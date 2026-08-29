"""Il carico su cui il gateway vale di piu', dalla porta che dovrebbe servirlo.

Contro uno sviluppatore che marca il proprio system prompt, un ciclo agentico e'
il caso migliore misurato: **+52%** su venti turni. La ragione e' strutturale -
i risultati dei tool pesano molto piu' del `system`, quindi chi marca solo
quello cattura il 3% - e vale piu' del numero.

Ma quel numero veniva da un carico sintetico passato dalla porta OpenAI, e il
README ammetteva che un client nativo via ``ANTHROPIC_BASE_URL`` era **non
provato**. Il gateway era migliore esattamente dove non aveva mai provato ad
arrivare.

Questi test non misurano il risparmio - lo fa il banco. Verificano la cosa che
un ciclo agentico puo' rompere e una chat no: il **protocollo**. Una catena
``tool_use``/``tool_result`` spezzata fa fallire la richiesta con un 400, e la
potatura del contesto e' precisamente uno stadio che taglia messaggi.
"""

from __future__ import annotations

from typing import Any

from ecotokens.pipeline.context import _has_orphan_tool_result

RISULTATO = "riga di codice\n" * 200  # ~3.000 caratteri per risultato


def _turno(indice: int, chiamate: int = 3) -> list[dict[str, Any]]:
    """Un turno completo: pensiero, chiamate a tool, risultati."""
    blocchi: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": f"devo esaminare il modulo {indice}"},
        {"type": "text", "text": f"faccio il passo {indice}"},
    ]
    risultati: list[dict[str, Any]] = []
    for chiamata in range(chiamate):
        ident = f"toolu_{indice}_{chiamata}"
        blocchi.append(
            {
                "type": "tool_use",
                "id": ident,
                "name": "leggi_file",
                "input": {"path": f"src/modulo_{chiamata}.py"},
            }
        )
        risultati.append(
            {
                "type": "tool_result",
                "tool_use_id": ident,
                "content": [{"type": "text", "text": RISULTATO}],
            }
        )
    return [
        {"role": "assistant", "content": blocchi},
        {"role": "user", "content": risultati},
    ]


def traccia(turni: int, chiamate: int = 3) -> list[list[dict[str, Any]]]:
    """Le richieste successive di un ciclo agentico, ognuna con la storia intera."""
    richieste, storia = [], []
    for indice in range(turni):
        storia = storia + [{"role": "user", "content": f"passo {indice}: sistema il modulo"}]
        richieste.append(list(storia))
        storia = storia + _turno(indice, chiamate)
    return richieste


def corpo(messaggi: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": "claude-opus-5",
        "max_tokens": 1024,
        "system": "Assistente di sviluppo. Usa i tool quando servono. " * 40,
        "tools": [
            {
                "name": "leggi_file",
                "description": "Legge un file dal disco. " * 20,
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        "messages": messaggi,
    }


# --- le proprieta' del protocollo -----------------------------------------


def _coppie(messaggi: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Gli id delle chiamate e quelli dei risultati, nell'ordine in cui stanno."""
    chiamate: set[str] = set()
    risultati: set[str] = set()
    for messaggio in messaggi:
        contenuto = messaggio.get("content")
        if not isinstance(contenuto, list):
            continue
        for blocco in contenuto:
            if not isinstance(blocco, dict):
                continue
            if blocco.get("type") == "tool_use":
                chiamate.add(blocco["id"])
            elif blocco.get("type") == "tool_result":
                risultati.add(blocco["tool_use_id"])
    return chiamate, risultati


def _breakpoint(params: dict[str, Any]) -> int:
    """Quanti `cache_control` ci sono in tutta la richiesta. L'API ne accetta 4."""
    totale = 0
    for chiave in ("tools", "system"):
        valore = params.get(chiave)
        if isinstance(valore, list):
            totale += sum(
                1 for blocco in valore
                if isinstance(blocco, dict) and blocco.get("cache_control")
            )
    for messaggio in params.get("messages") or []:
        contenuto = messaggio.get("content")
        if isinstance(contenuto, list):
            totale += sum(
                1 for blocco in contenuto
                if isinstance(blocco, dict) and blocco.get("cache_control")
            )
    return totale


def test_venti_turni_con_tool_arrivano_tutti_a_destinazione(client):
    """La proprieta' minima: il ciclo non si rompe a meta'."""
    for messaggi in traccia(20):
        risposta = client.post("/v1/messages", json=corpo(messaggi))
        assert risposta.status_code == 200, risposta.text


def _stadio(client, nome: str):
    """Lo stadio come gira davvero, non come e' scritto in configurazione.

    `enabled` viene copiato alla costruzione, quindi mutarlo su `settings` dopo
    che l'app esiste non accende niente: un test che lo facesse passerebbe a
    vuoto senza dirlo. Gli altri campi di configurazione si leggono invece a
    ogni richiesta, e quelli si possono cambiare.
    """
    for stadio in client.gateway.pipeline.stages:
        if stadio.name == nome:
            return stadio
    raise AssertionError(f"stadio {nome} non e' nella catena")


def test_nessun_tool_result_resta_orfano(client):
    """Un `tool_result` la cui chiamata non e' piu' nel prompt fa fallire la
    richiesta con un 400. E' il modo piu' probabile in cui uno stadio che taglia
    messaggi puo' rompere un ciclo agentico.

    Il taglio va **forzato**: con la finestra da un milione di token di Opus 5 e
    una traccia da ~45.000, la compattazione locale non scatterebbe mai e questo
    test passerebbe senza aver esercitato niente.
    """
    stadio = _stadio(client, "context")
    assert stadio.enabled, "senza lo stadio acceso questo test non prova nulla"
    stadio.config.hard_ratio = 0.0  # forza la compattazione locale a ogni turno
    assert stadio.config.local_compaction

    for messaggi in traccia(20):
        client.post("/v1/messages", json=corpo(messaggi))
        inviati = client.stub.last["messages"]

        chiamate, risultati = _coppie(inviati)
        orfani = risultati - chiamate
        assert not orfani, f"risultati senza la loro chiamata: {sorted(orfani)}"

        # E il controllo che il gateway usa gia' per scegliere dove tagliare.
        assert not _has_orphan_tool_result(inviati)


def test_gli_id_delle_chiamate_non_vengono_riscritti(client):
    """Il client li ha generati e li usa per appaiare i risultati: se il gateway
    ne cambiasse uno, la conversazione successiva non tornerebbe piu'."""
    richieste = traccia(8)
    client.post("/v1/messages", json=corpo(richieste[-1]))

    attesi, _ = _coppie(richieste[-1])
    inviati, _ = _coppie(client.stub.last["messages"])
    assert attesi <= inviati, f"id spariti: {sorted(attesi - inviati)}"


def test_i_blocchi_di_pensiero_non_vengono_alterati(client):
    """Il pensiero fa parte della firma del turno: modificarlo o toglierlo puo'
    invalidare il prefisso e, con i modelli che lo richiedono, la richiesta."""
    richieste = traccia(6)
    client.post("/v1/messages", json=corpo(richieste[-1]))

    def pensieri(messaggi):
        return [
            blocco["thinking"]
            for messaggio in messaggi
            for blocco in (messaggio.get("content") or [])
            if isinstance(blocco, dict) and blocco.get("type") == "thinking"
        ]

    assert pensieri(client.stub.last["messages"]) == pensieri(richieste[-1])


def test_i_breakpoint_restano_entro_i_quattro(client):
    """L'API ne accetta quattro. Il quinto non e' un'ottimizzazione in piu': e'
    un 400. Un ciclo agentico e' il carico che ne chiede di piu', perche' la
    conversazione cresce a ogni turno."""
    assert _stadio(client, "cache_planner").enabled, (
        "senza il pianificatore acceso non c'e' nessun breakpoint da contare"
    )

    for messaggi in traccia(20, chiamate=8):
        client.post("/v1/messages", json=corpo(messaggi))
        quanti = _breakpoint(client.stub.last)
        assert quanti <= 4, f"{quanti} breakpoint: l'API ne accetta 4"


def test_la_conversazione_non_perde_l_ultimo_turno(client):
    """La potatura puo' togliere il vecchio, mai la domanda a cui rispondere."""
    stadio = _stadio(client, "context")
    assert stadio.enabled
    stadio.config.hard_ratio = 0.0  # il taglio deve avvenire davvero
    richieste = traccia(20)
    client.post("/v1/messages", json=corpo(richieste[-1]))

    inviati = client.stub.last["messages"]
    assert inviati, "nessun messaggio e' arrivato"
    assert inviati[-1] == richieste[-1][-1], "l'ultimo turno e' stato riscritto"
