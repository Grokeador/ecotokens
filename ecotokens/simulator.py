"""Simulatore dell'API Anthropic.

Serve a due cose diverse ma imparentate: far girare i test senza rete e far
girare il banco di misura (``ecotokens bench``) senza credenziali e senza
spendere un centesimo.

E' un'app ASGI collegata al client tramite ``ASGITransport``: nessuna porta,
nessun thread, ma il percorso attraversato e' quello vero, serializzazione e
parsing SSE inclusi. Nei test non si usano ``respx`` o ``pytest-httpx`` per un
motivo preciso: l'SDK ``anthropic`` 1.x gira su ``httpx2``, non su ``httpx``,
quindi quelle librerie non intercettano nulla e i test passerebbero contro il
vuoto.

**Il modello di caching e' fedele nella meccanica, non nei numeri.** Riproduce
le regole che contano - match di prefisso nell'ordine tools, system, messages;
i breakpoint dei turni precedenti restano punti di lettura validi - ma i
conteggi di token sono proporzionali alla dimensione del testo, non prodotti
dal tokenizer vero. Le percentuali di risparmio misurate qui sono quindi
indicative: per numeri reali serve ``--live``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .pricing import model_info


# Lunghezza di una risposta tipica, prima dell'effetto dell'effort. **E' un
# modello dichiarato**, e sposta il peso relativo di prompt e output in ogni
# misura: con risposte piu' lunghe il risparmio percentuale del gateway scende,
# perche' tutte le sue leve agiscono sul prompt e nessuna sull'output.
#
# Sta qui, a livello di modulo, invece che dentro `__init__`: e' un'assunzione
# sul comportamento dell'API, e le assunzioni devono stare dove un test possa
# accorgersi che esistono (vedi `ecotokens/assunzioni.py`).
OUTPUT_TIPICO = 600


class StubState:
    """Memoria dello stub: richieste ricevute e prefissi in cache."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        # Tenute separate: `requests` significa "chiamate che generano", ed e'
        # su quella che i test della cache contano le chiamate risparmiate.
        self.count_requests: list[dict[str, Any]] = []
        self.cached_prefixes: dict[str, int] = {}
        self.reply_text = "Risposta di prova."
        self.tool_calls: list[dict[str, Any]] = []
        self.stop_reason = "end_turn"
        # Token di output di un turno tipico, prima dell'effetto dell'effort.
        self.base_output_tokens = OUTPUT_TIPICO
        # Guasti a comando. Il gateway sta **in mezzo**: cosa fa quando cio'
        # che ha a monte si rompe conta quanto cosa fa quando funziona, e
        # finora lo stub sapeva solo funzionare.
        #
        # `guasti` e' una coda di codici di stato consumata una richiesta alla
        # volta: cosi' si prova anche il caso che conta di piu', cioe' il
        # guasto **transitorio**, dove il secondo tentativo va a buon fine.
        self.guasti: list[int] = []
        # Chiude lo stream dopo N eventi, senza `message_stop`. E' la rottura
        # a meta' risposta: il client ha gia' ricevuto del testo e il prompt
        # e' gia' stato pagato per intero.
        self.interrompi_stream_dopo: int | None = None

    def reset(self) -> None:
        self.requests.clear()
        self.count_requests.clear()
        self.cached_prefixes.clear()
        self.tool_calls.clear()
        self.guasti.clear()
        self.interrompi_stream_dopo = None
        self.reply_text = "Risposta di prova."
        self.stop_reason = "end_turn"

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    @property
    def last_count(self) -> dict[str, Any]:
        return self.count_requests[-1]


# Distanza massima, in blocchi di contenuto, entro cui un breakpoint riesce a
# trovare una voce di cache scritta in precedenza.
LOOKBACK_BLOCKS = 20

# Quanti blocchi possono portare `cache_control` in una richiesta. Il quinto
# viene rifiutato con un 400.
#
# Il simulatore li accettava tutti. Era una **falla di fedelta'**, non una
# semplificazione innocua: un pianificatore che ne emettesse cinque avrebbe
# superato ogni test e fallito solo in produzione, e il test sarebbe stato
# verde proprio sul caso che doveva cogliere. L'ha trovata `ecotokens verifica
# --anche-simulato`, cioe' il giro che era stato dichiarato incapace di dire
# qualcosa sull'API: non dice niente sull'API, ma dice molto sul simulatore.
MAX_BREAKPOINTS = 4

# Parametri che i modelli Claude attuali **rifiutano con un 400**. I client
# OpenAI li mandano di routine, e toglierli e' il mestiere di
# `translate/to_anthropic.py` - il file piu' delicato del progetto.
#
# Il simulatore li accettava in silenzio, e quella tolleranza toglieva ogni
# valore ai test che coprono la sanificazione: se domani qualcuno smettesse di
# rimuovere `temperature`, tutti resterebbero verdi e il gateway darebbe 400
# su ogni richiesta di un client OpenAI. Un simulatore piu' permissivo
# dell'originale non semplifica: nasconde.
PARAMETRI_RIFIUTATI = (
    "temperature",
    "top_p",
    "top_k",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "logprobs",
    "n",
)


# Valore predefinito di ``keep`` quando la richiesta non lo specifica. **E' un
# modello dichiarato**: la strategia vera ``clear_tool_uses_20250919`` conserva
# un certo numero di risultati recenti, e il valore esatto va verificato con
# `--live`. Serve un default perche' senza di esso l'edit non potato sarebbe
# indistinguibile da un edit assente.
KEPT_TOOL_RESULTS = 3

CLEARED_PLACEHOLDER = {"type": "text", "text": "[risultato rimosso dal contesto]"}


def _valore(parametro: Any) -> int | None:
    if isinstance(parametro, dict) and isinstance(parametro.get("value"), int):
        return parametro["value"]
    return None


def _conta_tool_result(messaggi: list[dict[str, Any]]) -> int:
    return sum(
        1
        for messaggio in messaggi
        for blocco in (messaggio.get("content") or [])
        if isinstance(blocco, dict) and blocco.get("type") == "tool_result"
    )


def _peso(oggetto: Any) -> int:
    return max(0, len(json.dumps(oggetto, default=str)) // 4)


def _clear_tool_uses(
    messaggi: list[dict[str, Any]], edit: dict[str, Any], token_totali: int
) -> list[dict[str, Any]]:
    """Modello dichiarato di ``clear_tool_uses_20250919``.

    I tre parametri sono quelli dello schema ufficiale dell'SDK, non inventati
    qui: ``trigger`` decide quando la strategia entra in gioco, ``keep`` quanti
    risultati recenti restano interi, ``clear_at_least`` impone un guadagno
    minimo sotto il quale non si tocca nulla.

    Il comportamento e' ricostruito dalla documentazione, non osservato: va
    confermato con `--live` prima di dedurne qualcosa di definitivo.
    """
    totale_risultati = _conta_tool_result(messaggi)

    trigger = edit.get("trigger")
    if isinstance(trigger, dict):
        soglia = _valore(trigger)
        if soglia is not None:
            misura = token_totali if trigger.get("type") == "input_tokens" else totale_risultati
            if misura < soglia:
                return messaggi

    keep = _valore(edit.get("keep"))
    if keep is None:
        keep = KEPT_TOOL_RESULTS
    keep = max(0, keep)

    esclusi = set(edit.get("exclude_tools") or [])

    # Prima passata a vuoto: quanto si guadagnerebbe davvero.
    visti = 0
    risparmio = 0
    for messaggio in reversed(messaggi):
        for blocco in reversed(messaggio.get("content") or []):
            if isinstance(blocco, dict) and blocco.get("type") == "tool_result":
                visti += 1
                if visti > keep and blocco.get("name") not in esclusi:
                    risparmio += _peso(blocco.get("content")) - _peso(
                        [dict(CLEARED_PLACEHOLDER)]
                    )

    minimo = _valore(edit.get("clear_at_least"))
    if minimo is not None and risparmio < minimo:
        # "Context will only be modified if at least this many tokens can be
        # removed": sotto la soglia la richiesta resta integrale.
        return messaggi

    visti = 0
    potati = []
    for messaggio in reversed(messaggi):
        contenuto = messaggio.get("content")
        if not isinstance(contenuto, list):
            potati.append(messaggio)
            continue
        nuovo = []
        for blocco in reversed(contenuto):
            if isinstance(blocco, dict) and blocco.get("type") == "tool_result":
                visti += 1
                if visti > keep and blocco.get("name") not in esclusi:
                    blocco = {**blocco, "content": [dict(CLEARED_PLACEHOLDER)]}
            nuovo.append(blocco)
        messaggio["content"] = list(reversed(nuovo))
        potati.append(messaggio)
    return list(reversed(potati))


def _apply_context_edits(payload: dict[str, Any]) -> dict[str, Any]:
    """Applica ``context_management`` come farebbe il server, prima di contare.

    Senza questo passaggio la potatura del contesto risulterebbe inefficace per
    costruzione: il simulatore conterebbe comunque i token dei risultati che il
    server avrebbe gia' scartato, e lo stadio verrebbe giudicato inutile per un
    difetto del banco di misura, non per un suo difetto.
    """
    edits = (payload.get("context_management") or {}).get("edits") or []
    edits = [edit for edit in edits if isinstance(edit, dict)]
    if not edits:
        return payload

    messaggi = [dict(messaggio) for messaggio in payload.get("messages") or []]
    token_totali = _peso(payload)

    for edit in edits:
        if edit.get("type") == "clear_tool_uses_20250919":
            messaggi = _clear_tool_uses(messaggi, edit, token_totali)
        elif edit.get("type") == "clear_thinking_20251015":
            for messaggio in messaggi:
                contenuto = messaggio.get("content")
                if isinstance(contenuto, list):
                    messaggio["content"] = [
                        blocco
                        for blocco in contenuto
                        if not (isinstance(blocco, dict) and blocco.get("type") == "thinking")
                    ]

    return {**payload, "messages": messaggi}


def _render(payload: dict[str, Any]) -> tuple[list[str], list[int]]:
    """Serializza la richiesta nell'ordine di render e localizza i marker.

    L'ordine e' quello vero - ``tools``, poi ``system``, poi ``messages`` - ed
    e' cio' che rende sensato parlare di "prefisso": un tool che cambia
    posizione invalida tutto quello che viene dopo.
    """
    items: list[str] = []
    markers: list[int] = []

    def aggiungi(valore: Any, marcato: bool) -> None:
        # Il marker non entra nell'impronta: ``cache_control`` e' una direttiva,
        # non contenuto. Includerlo sarebbe un errore sottile e costoso: il
        # blocco marcato a un turno non combacerebbe piu' con se stesso al turno
        # dopo, quando il marker si e' spostato in avanti, e la cache non
        # verrebbe mai riletta.
        items.append(
            json.dumps(_senza_marker(valore), sort_keys=True, ensure_ascii=False, default=str)
        )
        if marcato:
            markers.append(len(items))

    for tool in payload.get("tools") or []:
        aggiungi(tool, isinstance(tool, dict) and bool(tool.get("cache_control")))

    system = payload.get("system")
    if isinstance(system, list):
        for block in system:
            aggiungi(block, isinstance(block, dict) and bool(block.get("cache_control")))
    elif system:
        aggiungi(system, False)

    for message in payload.get("messages") or []:
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            aggiungi(
                {"role": message.get("role"), "block": block},
                isinstance(block, dict) and bool(block.get("cache_control")),
            )

    # Caching automatico: un solo `cache_control` in cima alla richiesta, senza
    # marker sui blocchi. Il server piazza il breakpoint sull'ultimo blocco
    # memorizzabile, e a ogni turno quello si sposta in avanti da solo.
    #
    # E' la funzione che ha cambiato la domanda a cui questo banco risponde.
    # Finche' non esisteva, "senza gateway" voleva dire "nessuna cache" e il
    # confronto era onesto. Adesso chiunque ottiene un breakpoint con una riga,
    # quindi il riferimento giusto per il pianificatore di EcoTokens non e' piu'
    # il nulla: e' questo.
    if payload.get("cache_control") and items:
        if len(items) not in markers:
            markers.append(len(items))

    return items, markers


def _senza_marker(valore: Any) -> Any:
    """Copia della struttura senza le chiavi ``cache_control``."""
    if isinstance(valore, dict):
        return {
            chiave: _senza_marker(elemento)
            for chiave, elemento in valore.items()
            if chiave != "cache_control"
        }
    if isinstance(valore, list):
        return [_senza_marker(elemento) for elemento in valore]
    return valore


def _prefix_table(items: list[str]) -> tuple[list[str], list[int]]:
    """Impronta e dimensione di ogni prefisso, calcolate una volta sola.

    L'impronta e' incrementale: quella del prefisso lungo N deriva da quella
    lunga N-1. Serve perche' la ricerca all'indietro interroga fino a venti
    posizioni per marker, e ricalcolare ogni volta l'intero prefisso renderebbe
    il simulatore quadratico sulle conversazioni lunghe.
    """
    digests: list[str] = []
    lunghezze: list[int] = []
    corrente = hashlib.sha256()
    totale = 0
    for item in items:
        corrente = corrente.copy()
        corrente.update(item.encode("utf-8"))
        digests.append(corrente.hexdigest())
        totale += len(item)
        lunghezze.append(totale)
    return digests, lunghezze


# Effetto dell'effort sui token generati. **E' un modello dichiarato, non una
# misura**: l'effort governa la profondita' del ragionamento, che viene
# fatturato come output, ma il rapporto esatto fra i livelli dipende dal
# compito e va verificato con `--live`. Serve perche' senza di esso il
# simulatore restituirebbe sempre la stessa lunghezza e l'effort adattivo
# risulterebbe inutile per costruzione.
EFFORT_OUTPUT_MULTIPLIER = {
    "low": 0.4,
    "medium": 0.7,
    "high": 1.0,
    "xhigh": 1.6,
    "max": 2.6,
}


def _output_tokens(state: StubState, payload: dict[str, Any]) -> int:
    effort = (payload.get("output_config") or {}).get("effort", "high")
    moltiplicatore = EFFORT_OUTPUT_MULTIPLIER.get(effort, 1.0)
    if (payload.get("thinking") or {}).get("type") != "adaptive":
        # Senza ragionamento adattivo resta solo la risposta visibile.
        moltiplicatore *= 0.5
    generati = max(1, int(state.base_output_tokens * moltiplicatore))
    # Il server smette di generare a `max_tokens` e fattura solo quello che ha
    # generato. Senza questo taglio ogni tetto imposto dal gateway - per
    # esempio quello sul riassunto di compattazione - resterebbe invisibile
    # alla misura, e sembrerebbe inutile per costruzione.
    tetto = payload.get("max_tokens")
    if isinstance(tetto, int) and tetto > 0:
        generati = min(generati, tetto)
    return generati


def _sopra_la_soglia(payload: dict[str, Any], items: list[str], markers: list[int]) -> list[int]:
    """Scarta i breakpoint su prefissi troppo corti per essere memorizzati.

    Sotto il minimo del modello la cache non si crea e **l'API non lo segnala**:
    ``cache_creation_input_tokens`` torna zero e la richiesta va a buon fine.
    E' la perdita che si nota solo mesi dopo, sulla fattura.

    Il simulatore lo ignorava, e finora non era grave: il pianificatore del
    gateway controlla la soglia prima di piazzare un marker, quindi non gliene
    passava mai uno sotto misura. Diventa invece decisivo appena si misura una
    configurazione in cui il pianificatore e' spento - il caching automatico -
    perche' li' il marker lo mette il server e il controllo tocca a lui.

    Il minimo non e' monotono col prezzo: Opus 5 ne vuole 512, Sonnet 5 mille,
    Haiku 4.5 quattromila. Passare a un modello piu' economico puo' quindi
    spegnere la cache in silenzio.
    """
    if not markers:
        return markers
    minimo = model_info(payload.get("model") or "").cache_min_tokens
    lunghezze = [len(voce) for voce in items]
    sopravvissuti = []
    for marker in markers:
        token = max(1, sum(lunghezze[:marker]) // 4)
        if token >= minimo:
            sopravvissuti.append(marker)
    return sopravvissuti

def _usage_for(state: StubState, payload: dict[str, Any]) -> dict[str, int]:
    # Il server applica gli edit e poi conta: contare prima significherebbe
    # fatturare contenuto che non e' mai arrivato al modello.
    effettivo = _apply_context_edits(payload)
    total = max(1, len(json.dumps(effettivo, default=str)) // 4)
    output = _output_tokens(state, effettivo)
    items, markers = _render(effettivo)
    markers = _sopra_la_soglia(effettivo, items, markers)

    if not markers:
        return {
            "input_tokens": total,
            "output_tokens": output,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    digests, lunghezze = _prefix_table(items)

    def token_del_prefisso(posizione: int) -> int:
        return max(1, lunghezze[posizione - 1] // 4)

    # Lettura: ogni breakpoint cammina all'indietro fino a LOOKBACK_BLOCKS
    # blocchi in cerca di una voce gia' scritta. Non serve che il marker sia
    # nella stessa posizione di prima: e' questo che permette alla cache di
    # accumularsi turno dopo turno mentre la conversazione cresce.
    read_tokens = 0
    for marker in markers:
        for indietro in range(LOOKBACK_BLOCKS + 1):
            posizione = marker - indietro
            if posizione <= 0:
                break
            if digests[posizione - 1] in state.cached_prefixes:
                read_tokens = max(read_tokens, token_del_prefisso(posizione))
                break

    # Scrittura: si paga la parte nuova, cioe' quanto il breakpoint piu' lungo
    # supera cio' che si e' potuto rileggere.
    piu_lungo = token_del_prefisso(max(markers))
    nuovi = any(digests[marker - 1] not in state.cached_prefixes for marker in markers)
    written = max(0, piu_lungo - read_tokens) if nuovi else 0

    for marker in markers:
        state.cached_prefixes.setdefault(digests[marker - 1], token_del_prefisso(marker))

    read_tokens = min(read_tokens, total)
    written = min(written, max(0, total - read_tokens))
    return {
        "input_tokens": max(0, total - read_tokens - written),
        "output_tokens": output,
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": read_tokens,
    }


def _content_blocks(state: StubState) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if state.reply_text:
        blocks.append({"type": "text", "text": state.reply_text})
    for index, call in enumerate(state.tool_calls):
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", f"toolu_{index}"),
                "name": call["name"],
                "input": call.get("input", {}),
            }
        )
    return blocks or [{"type": "text", "text": ""}]


def create_stub(state: StubState | None = None) -> tuple[FastAPI, StubState]:
    state = state or StubState()
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(request: Request):
        payload = await request.json()
        state.requests.append(payload)
        if state.guasti:
            # La richiesta e' partita davvero - resta contata - ma non scrive
            # nessun prefisso in cache: una chiamata fallita non lascia niente
            # da rileggere, ed e' proprio il caso in cui un gateway distratto
            # crede di aver messo qualcosa in cache e paga la scrittura due
            # volte.
            stato = state.guasti.pop(0)
            return JSONResponse(
                status_code=stato,
                content={
                    "type": "error",
                    "error": {"type": _tipo_errore(stato), "message": f"guasto simulato {stato}"},
                },
            )
        vietati = [nome for nome in PARAMETRI_RIFIUTATI if nome in payload]
        if vietati:
            return JSONResponse(
                status_code=400,
                content={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": (
                            f"Unexpected parameter(s): {', '.join(vietati)}. "
                            "These are not supported on this model."
                        ),
                    },
                },
            )
        marcati = _conta_breakpoint(payload)
        if marcati > MAX_BREAKPOINTS:
            return JSONResponse(
                status_code=400,
                content={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": (
                            f"A maximum of {MAX_BREAKPOINTS} blocks with "
                            f"cache_control may be provided, got {marcati}"
                        ),
                    },
                },
            )
        usage = _usage_for(state, payload)
        blocks = _content_blocks(state)
        stop_reason = "tool_use" if state.tool_calls else state.stop_reason

        body = {
            "id": f"msg_{len(state.requests):04d}",
            "type": "message",
            "role": "assistant",
            "model": payload.get("model", "claude-opus-5"),
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        }

        if not payload.get("stream"):
            return JSONResponse(content=body)

        async def events():
            start = dict(body)
            start["content"] = []
            yield _event("message_start", {"type": "message_start", "message": start})

            for index, block in enumerate(blocks):
                if block["type"] == "text":
                    yield _event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    for piece in _split(block["text"]):
                        yield _event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "text_delta", "text": piece},
                            },
                        )
                else:
                    yield _event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {
                                "type": "tool_use",
                                "id": block["id"],
                                "name": block["name"],
                                "input": {},
                            },
                        },
                    )
                    yield _event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(block["input"]),
                            },
                        },
                    )
                yield _event(
                    "content_block_stop", {"type": "content_block_stop", "index": index}
                )

            yield _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage["output_tokens"]},
                },
            )
            yield _event("message_stop", {"type": "message_stop"})

        async def troncati():
            """Gli stessi eventi, ma tagliati dove chiede lo stato.

            Contare da fuori invece di instrumentare ogni `yield` tiene la
            simulazione del guasto separata dalla simulazione del protocollo:
            se domani cambiano gli eventi, il troncamento continua a valere.
            """
            limite = state.interrompi_stream_dopo
            emessi = 0
            async for evento in events():
                if limite is not None and emessi >= limite:
                    # Nessun `message_stop`: e' cosi' che si presenta una
                    # connessione caduta, non con un evento di errore.
                    return
                yield evento
                emessi += 1

        return StreamingResponse(troncati(), media_type="text/event-stream")

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        payload = await request.json()
        state.count_requests.append(payload)
        total = max(1, len(json.dumps(payload, default=str)) // 4)
        return JSONResponse(content={"input_tokens": total})

    return app, state


def _conta_breakpoint(payload: dict[str, Any]) -> int:
    """Blocchi marcati con `cache_control`, ovunque si trovino.

    Il conto e' su tutta la richiesta - tool, system e messaggi - perche' e'
    cosi' che lo fa l'API: i quattro sono un budget della richiesta, non di
    ciascuna delle sue parti.
    """
    quanti = 0
    da_guardare: list[Any] = [payload.get("system"), payload.get("tools")]
    for messaggio in payload.get("messages") or []:
        if isinstance(messaggio, dict):
            da_guardare.append(messaggio.get("content"))
    for gruppo in da_guardare:
        if isinstance(gruppo, list):
            quanti += sum(
                1
                for blocco in gruppo
                if isinstance(blocco, dict) and blocco.get("cache_control")
            )
    return quanti


def _tipo_errore(stato: int) -> str:
    """Il `type` che l'API mette nel corpo, per codice di stato."""
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
        500: "api_error",
        529: "overloaded_error",
    }.get(stato, "api_error")


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _split(text: str, size: int = 8) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]
