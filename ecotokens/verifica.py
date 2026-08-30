"""Confronta le assunzioni del simulatore con il comportamento vero dell'API.

`ecotokens assunzioni` elenca cosa il progetto dà per vero. Questo modulo lo
**controlla**, una voce alla volta, chiamando l'API e guardando cosa risponde.

C'è una trappola da nominare subito, perché è la ragione per cui questo file
potrebbe fare più male che bene. Puntato al simulatore, ogni controllo passa —
e non significa niente: si starebbe verificando il simulatore contro se stesso,
cioè producendo una spunta verde che non porta nessuna informazione. È
esattamente la forma di misura che questo progetto ha già sbagliato tre volte:
uno strumento che dà una risposta plausibile a una domanda che non ha fatto.

Perciò il comando **si rifiuta di girare contro il simulatore**, a meno che non
gli si dica esplicitamente di farlo — e in quel caso ogni riga porta scritto
che il risultato è circolare. La spunta vale solo quando dall'altra parte c'è
`api.anthropic.com`.

Le chiamate costano. Sono poche e corte, e il comando dice quante ne farà prima
di farle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .assunzioni import ASSUNZIONI
from .pricing import MODELS

COMBACIA = "combacia"
DIVERGE = "diverge"
INDETERMINATO = "indeterminato"


@dataclass
class Controllo:
    assunzione: str
    atteso: str
    osservato: str
    esito: str
    nota: str = ""
    chiamate: int = 0


@dataclass
class Rapporto:
    controlli: list[Controllo] = field(default_factory=list)
    circolare: bool = False

    @property
    def chiamate(self) -> int:
        return sum(c.chiamate for c in self.controlli)

    @property
    def divergenze(self) -> list[Controllo]:
        return [c for c in self.controlli if c.esito == DIVERGE]

    def riepilogo(self) -> str:
        combaciano = sum(1 for c in self.controlli if c.esito == COMBACIA)
        testo = (
            f"{combaciano} su {len(self.controlli)} combaciano, "
            f"{len(self.divergenze)} divergono, {self.chiamate} chiamate."
        )
        if self.circolare:
            testo += (
                " ATTENZIONE: eseguito contro il simulatore. Ogni riga verifica "
                "il simulatore contro se stesso e non dice niente sull'API vera."
            )
        return testo


def _usage(messaggio: Any) -> dict[str, int]:
    u = getattr(messaggio, "usage", None)
    return {
        nome: int(getattr(u, nome, 0) or 0)
        for nome in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    }


def _riempi(token_circa: int) -> str:
    """Testo lungo circa `token_circa` token, stimati a 3,6 caratteri l'uno."""
    return "misura di riferimento " * max(1, int(token_circa * 3.6 / 22))


# --- i singoli controlli ---------------------------------------------------


async def _soglia_di_cache(client, modello: str) -> Controllo:
    """Sotto la soglia la cache non si forma, e l'API non lo dice.

    E' l'assunzione con la conseguenza piu' subdola di tutte: il gateway crede
    di aver messo in cache, paga la scrittura e non rilegge mai. Il controllo
    manda due prompt, uno sotto e uno sopra, e guarda
    `cache_creation_input_tokens`.
    """
    soglia = MODELS[modello].cache_min_tokens
    osservazioni = {}
    for etichetta, quanti in (("sotto", int(soglia * 0.6)), ("sopra", int(soglia * 1.8))):
        messaggio = await client.messages.create(
            model=modello,
            max_tokens=16,
            system=[
                {
                    "type": "text",
                    "text": _riempi(quanti),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": "ok"}],
        )
        osservazioni[etichetta] = _usage(messaggio)["cache_creation_input_tokens"]

    atteso = "0 sotto soglia, >0 sopra"
    osservato = f"{osservazioni['sotto']} sotto, {osservazioni['sopra']} sopra"
    combacia = osservazioni["sotto"] == 0 and osservazioni["sopra"] > 0
    return Controllo(
        assunzione="Soglie minime di cache",
        atteso=f"{atteso} (soglia dichiarata: {soglia} token per {modello})",
        osservato=osservato,
        esito=COMBACIA if combacia else DIVERGE,
        nota=(
            ""
            if combacia
            else "Se sopra soglia resta a zero, la soglia vera e' piu' alta di "
            "quella dichiarata e il pianificatore sta pagando scritture che non "
            "si formano."
        ),
        chiamate=2,
    )


async def _rilettura_di_cache(client, modello: str) -> Controllo:
    """Che una rilettura avvenga si osserva; che costi un decimo, no.

    L'API riporta quanti token sono stati riletti, non a quale prezzo. Il
    moltiplicatore 0,1x resta documentato e non verificabile da qui, e dirlo e'
    piu' utile che spuntarlo.
    """
    prefisso = [
        {
            "type": "text",
            "text": _riempi(MODELS[modello].cache_min_tokens * 2),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    letture = []
    for _ in range(2):
        messaggio = await client.messages.create(
            model=modello,
            max_tokens=16,
            system=prefisso,
            messages=[{"role": "user", "content": "ok"}],
        )
        letture.append(_usage(messaggio)["cache_read_input_tokens"])

    combacia = letture[1] > 0
    return Controllo(
        assunzione="Moltiplicatori della cache",
        atteso="la seconda richiesta rilegge il prefisso",
        osservato=f"riletture: {letture[0]} poi {letture[1]}",
        esito=COMBACIA if combacia else DIVERGE,
        nota=(
            "Verifica solo che la rilettura avvenga. Il moltiplicatore 0,1x non "
            "e' osservabile da `usage`: l'API riporta i token, non il prezzo."
        ),
        chiamate=2,
    )


async def _effetto_effort(client, modello: str) -> Controllo:
    """L'assunzione dichiarata piu' pesante: ci si appoggia tutto il risparmio
    del primo livello del router."""
    from .simulator import EFFORT_OUTPUT_MULTIPLIER

    domanda = "Spiega in modo completo perche' il cielo appare azzurro."
    osservati: dict[str, int] = {}
    troncati: list[str] = []
    for livello in ("low", "high", "max"):
        messaggio = await client.messages.create(
            # Il tetto era 4096, e con quello il controllo si e' smentito da
            # solo: `high` e `max` producevano **esattamente** 4096 token con
            # `stop_reason=max_tokens`, cioe' erano stati tagliati, e il
            # rapporto 1.00x fra i due misurava il tetto invece dell'effort.
            # Il controllo ha concluso "il verso non regge" da una misura
            # satura. Alzare il tetto non basta - puo' saturare comunque - e
            # per questo il taglio viene rilevato e reso INDETERMINATO.
            model=modello,
            max_tokens=16_000,
            thinking={"type": "adaptive"},
            output_config={"effort": livello},
            messages=[{"role": "user", "content": domanda}],
        )
        osservati[livello] = _usage(messaggio)["output_tokens"]
        if getattr(messaggio, "stop_reason", None) == "max_tokens":
            troncati.append(livello)

    if troncati:
        return Controllo(
            assunzione="Effetto dell'effort sui token generati",
            atteso="low < high < max in token generati",
            osservato=(
                ", ".join(f"{k} {v}" for k, v in osservati.items())
                + f" - troncati al tetto: {', '.join(troncati)}"
            ),
            esito=INDETERMINATO,
            nota=(
                "Una risposta tagliata a `max_tokens` non dice quanto avrebbe "
                "generato: confrontare due risposte entrambe al tetto misura il "
                "tetto. Serve rifare con un limite piu' alto."
            ),
            chiamate=3,
        )

    base = osservati.get("high") or 1
    rapporti = {k: v / base for k, v in osservati.items()}
    atteso = ", ".join(
        f"{k} {EFFORT_OUTPUT_MULTIPLIER[k]:.2f}x" for k in ("low", "high", "max")
    )
    osservato = ", ".join(f"{k} {v:.2f}x" for k, v in rapporti.items())
    # Il verso conta piu' del valore: e' quello su cui si regge lo stadio.
    verso = osservati["low"] < osservati["high"] < osservati["max"]
    return Controllo(
        assunzione="Effetto dell'effort sui token generati",
        atteso=f"{atteso} (rispetto a high)",
        osservato=osservato,
        esito=COMBACIA if verso else DIVERGE,
        nota=(
            "Il controllo verifica il **verso**, non i valori: il rapporto esatto "
            "dipende dal compito, e una sola domanda non lo stabilisce. Se il "
            "verso non regge, lo stadio dell'effort non risparmia."
            if verso
            else "Il verso non regge: abbassare l'effort non riduce i token "
            "generati su questo compito, e il risparmio attribuito allo stadio "
            "e' da rifare."
        ),
        chiamate=3,
    )


def _quattrocento_estraneo(errore: Exception, *parole: str) -> str:
    """Un 400 non conferma niente da solo: dipende da **quale** 400.

    Trovato al primo contatto con l'API vera. La chiave era legata a
    un'identita', e l'API rispondeva `400 anthropic-workspace-id is required` a
    qualunque richiesta - compresa quella che chiedeva se `temperature` fosse
    rifiutato, e quella che chiedeva se il quinto breakpoint fosse rifiutato.
    Tutti e due i controlli concludevano su `except BadRequestError`, e hanno
    dichiarato **confermate** due assunzioni che non avevano nemmeno sfiorato.

    Un controllo che passa per la ragione sbagliata e' peggio di uno che
    fallisce: quello rumoroso si corregge, questo si crede. Ed e' il difetto
    piu' caro che questo progetto possa avere, perche' sta nel modulo che
    esiste per dire quali assunzioni reggono.

    Restituisce il messaggio se il 400 non parla di cio' che e' stato chiesto,
    la stringa vuota se e' quello atteso.
    """
    testo = str(errore)
    minuscolo = testo.lower()
    if any(parola.lower() in minuscolo for parola in parole):
        return ""
    return testo


async def _parametri_rifiutati(client, modello: str) -> Controllo:
    """Se non fossero rifiutati, il gateway starebbe scartando parametri utili."""
    import anthropic

    try:
        # `extra_body`, non un parametro: l'SDK 1.x ha **tolto** `temperature`
        # dalla firma, quindi passarlo direttamente da un TypeError prima
        # ancora della richiesta. Che e' gia' una conferma - il parametro non
        # esiste piu' nemmeno per il client ufficiale - ma non e' la domanda:
        # qui si vuole sapere cosa risponde il server a chi glielo manda
        # comunque, che e' esattamente cio' che fa un client OpenAI.
        await client.messages.create(
            model=modello,
            max_tokens=16,
            messages=[{"role": "user", "content": "ok"}],
            extra_body={"temperature": 0.5},
        )
        rifiutato = False
        dettaglio = "accettato"
    except anthropic.BadRequestError as errore:
        estraneo = _quattrocento_estraneo(errore, "temperature")
        if estraneo:
            return Controllo(
                assunzione="I parametri rimossi danno 400",
                atteso="400 su `temperature`",
                osservato=f"400 di altra natura: {estraneo[:110]}",
                esito=INDETERMINATO,
                nota=(
                    "Il 400 c'e' ma non parla di `temperature`: non conferma "
                    "niente. Va tolta la causa vera e rifatto il controllo."
                ),
                chiamate=1,
            )
        rifiutato = True
        dettaglio = f"400: {str(errore)[:80]}"
    except Exception as errore:  # un altro errore non risponde alla domanda
        return Controllo(
            assunzione="I parametri rimossi danno 400",
            atteso="400 su `temperature`",
            osservato=f"{type(errore).__name__}",
            esito=INDETERMINATO,
            nota="Errore di altra natura: il controllo non ha potuto concludere.",
            chiamate=1,
        )

    return Controllo(
        assunzione="I parametri rimossi danno 400",
        atteso="400 su `temperature`",
        osservato=dettaglio,
        esito=COMBACIA if rifiutato else DIVERGE,
        nota=(
            ""
            if rifiutato
            else "Se e' accettato, il gateway sta scartando un parametro che il "
            "client poteva usare: sta cambiando la risposta senza motivo."
        ),
        chiamate=1,
    )


async def _troppi_breakpoint(client, modello: str) -> Controllo:
    import anthropic

    blocchi = [
        {
            "type": "text",
            "text": _riempi(600) + f" blocco {indice}",
            "cache_control": {"type": "ephemeral"},
        }
        for indice in range(5)
    ]
    try:
        await client.messages.create(
            model=modello,
            max_tokens=16,
            system=blocchi,
            messages=[{"role": "user", "content": "ok"}],
        )
        rifiutato = False
        dettaglio = "cinque breakpoint accettati"
    except anthropic.BadRequestError as errore:
        estraneo = _quattrocento_estraneo(errore, "cache_control", "cache control")
        if estraneo:
            return Controllo(
                assunzione="Quattro breakpoint al massimo",
                atteso="400 al quinto",
                osservato=f"400 di altra natura: {estraneo[:110]}",
                esito=INDETERMINATO,
                nota=(
                    "Il 400 c'e' ma non parla di `cache_control`: il quinto "
                    "breakpoint non e' stato messo alla prova."
                ),
                chiamate=1,
            )
        rifiutato = True
        dettaglio = "400 sul quinto breakpoint"
    except Exception as errore:
        return Controllo(
            assunzione="Quattro breakpoint al massimo",
            atteso="400 al quinto",
            osservato=type(errore).__name__,
            esito=INDETERMINATO,
            chiamate=1,
        )

    return Controllo(
        assunzione="Quattro breakpoint al massimo",
        atteso="400 al quinto",
        osservato=dettaglio,
        esito=COMBACIA if rifiutato else DIVERGE,
        nota=(
            ""
            if rifiutato
            else "Se ne accetta cinque, il limite del pianificatore e' piu' "
            "stretto del necessario e si sta rinunciando a un breakpoint."
        ),
        chiamate=1,
    )


async def _ciclo_agentico(client, modello: str) -> Controllo:
    """L'assunzione da cui dipende il numero piu' alto del progetto.

    Il +52% su un ciclo agentico si regge su una cosa sola: che marcare la
    **conversazione** - non solo il `system` - produca riletture che crescono
    turno dopo turno, man mano che i risultati dei tool si accumulano. Se il
    prefisso non reggesse fra un turno e il successivo, quel numero sarebbe
    solo un artefatto del simulatore.

    Tre turni bastano: la prima richiesta scrive, la seconda deve rileggere, la
    terza deve rileggere **di piu'**.
    """
    corpo = _riempi(4_000)
    storia: list[dict[str, Any]] = []
    letture: list[int] = []

    def blocco(testo: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": testo}]

    def con_marcatore(messaggio: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {**messaggio["content"][0], "cache_control": {"type": "ephemeral"}}
            ],
        }

    # Il testimone, e senza di lui questo controllo non vale niente.
    #
    # Misurato: c'e' stato un momento in cui la cache non rileggeva **nulla**,
    # nemmeno la stessa identica richiesta ripetuta - il caso che dieci minuti
    # prima funzionava. In quella finestra questo controllo avrebbe dichiarato
    # smentita l'assunzione su cui poggia il numero piu' alto del progetto,
    # e la smentita avrebbe descritto le condizioni del momento, non l'API.
    #
    # Quindi prima si chiede una cosa che **deve** funzionare: la stessa
    # richiesta due volte. Se nemmeno quella rilegge, non si conclude niente.
    apertura = [{"role": "user", "content": blocco(f"apertura: {corpo}")}]
    await client.messages.create(
        model=modello, max_tokens=16, messages=[con_marcatore(apertura[0])]
    )
    testimone = await client.messages.create(
        model=modello, max_tokens=16, messages=[con_marcatore(apertura[0])]
    )
    if _usage(testimone)["cache_read_input_tokens"] <= 0:
        return Controllo(
            assunzione="Il prefisso di conversazione regge fra i turni",
            atteso="le riletture crescono a ogni turno",
            osservato="la stessa richiesta ripetuta non rilegge: 0",
            esito=INDETERMINATO,
            nota=(
                "Non si sta misurando la tenuta del prefisso: in questo momento "
                "la cache non rilegge nemmeno una richiesta identica. Qualunque "
                "verdetto qui descriverebbe le condizioni del momento, non "
                "l'API. Rifare piu' tardi."
            ),
            chiamate=2,
        )

    for indice in range(3):
        # La storia resta **sempre** in forma a blocchi. Tenerla come stringa e
        # convertirla solo per l'ultimo messaggio cambierebbe la forma del
        # prefisso a ogni turno, e nessuna rilettura avverrebbe mai: e' il
        # primo modo in cui questo controllo e' stato scritto, e falliva.
        #
        # Il testo grosso sta **gia' nel primo turno**: con una prima domanda
        # corta il prefisso resterebbe sotto la soglia minima del modello, non
        # verrebbe scritta nessuna voce, e la prima rilettura slitterebbe al
        # terzo turno. Si misurerebbe la soglia invece della tenuta.
        storia = storia + [
            {"role": "user", "content": blocco(f"passo {indice}: {corpo}")}
        ]

        # Il breakpoint va in fondo alla conversazione, che e' precisamente
        # cio' che un client che marca solo il proprio system prompt non fa.
        messaggi = [dict(m) for m in storia]
        messaggi[-1] = {
            "role": "user",
            "content": [
                {**messaggi[-1]["content"][0], "cache_control": {"type": "ephemeral"}}
            ],
        }
        messaggio = await client.messages.create(
            model=modello, max_tokens=16, messages=messaggi
        )
        letture.append(_usage(messaggio)["cache_read_input_tokens"])
        storia = storia + [
            {"role": "assistant", "content": blocco("fatto")},
            {"role": "user", "content": blocco(f"risultato del tool: {corpo}")},
        ]

    # Due domande distinte, e tenerle separate cambia la conclusione. La prima
    # e' se il prefisso **regge**: al secondo e terzo turno si rilegge qualcosa
    # invece di riscrivere tutto. La seconda e' se cio' che si rilegge
    # **cresce** con la conversazione. Osservata una volta una rilettura ferma
    # sul primo messaggio (2809, poi ancora 2809): il prefisso reggeva e non
    # cresceva, e la vecchia condizione unica - `letture[2] > letture[1] > 0` -
    # avrebbe chiamato quel caso una smentita. Non lo e': e' una tenuta piu'
    # debole di quella assunta, che e' un'informazione diversa e va detta.
    regge = letture[1] > 0 and letture[2] > 0
    cresce = letture[2] > letture[1]
    return Controllo(
        assunzione="Il prefisso di conversazione regge fra i turni",
        atteso="riletture > 0 dal secondo turno, e in crescita",
        osservato=(
            f"riletture: {letture[0]}, {letture[1]}, {letture[2]}"
            + ("" if cresce else " (reggono ma non crescono)" if regge else "")
        ),
        esito=COMBACIA if regge else DIVERGE,
        nota=(
            "E' l'assunzione su cui poggia il +52% del carico agentico, il "
            "numero piu' alto che il progetto dichiara. Se diverge, quel "
            "numero va tolto dal README prima di ogni altra cosa."
            if not regge
            else ""
            if cresce
            else "Il prefisso regge ma la rilettura non cresce: si riusa il "
            "primo messaggio e si riscrive la coda a ogni turno. Il risparmio "
            "esiste ed e' piu' piccolo di quello assunto."
        ),
        chiamate=5,
    )


CONTROLLI = (
    _soglia_di_cache,
    _rilettura_di_cache,
    _parametri_rifiutati,
    _troppi_breakpoint,
    _effetto_effort,
    _ciclo_agentico,
)

# Quante chiamate costa l'intera verifica. Dichiarato prima di eseguirla:
# un comando che spende deve dire quanto prima di spendere.
CHIAMATE_PREVISTE = 14


async def verifica(client, modello: str, *, circolare: bool = False) -> Rapporto:
    """Esegue tutti i controlli. `circolare` marca l'esecuzione sul simulatore."""
    rapporto = Rapporto(circolare=circolare)
    for controllo in CONTROLLI:
        try:
            rapporto.controlli.append(await controllo(client, modello))
        except Exception as errore:
            rapporto.controlli.append(
                Controllo(
                    assunzione=controllo.__name__,
                    atteso="-",
                    osservato=f"{type(errore).__name__}: {errore}",
                    esito=INDETERMINATO,
                    nota="Il controllo stesso si e' rotto: non dice niente "
                    "sull'assunzione.",
                )
            )
    return rapporto


def nomi_coperti() -> set[str]:
    """Quali voci del registro delle assunzioni questo comando sa controllare."""
    return {
        "Soglie minime di cache",
        "Moltiplicatori della cache",
        "I parametri rimossi danno 400",
        "Quattro breakpoint al massimo",
        "Effetto dell'effort sui token generati",
        "Il prefisso di conversazione regge fra i turni",
    }


def nomi_scoperti() -> set[str]:
    """E quali no. Un elenco di cio' che si sa fare, senza quello di cio' che
    non si sa fare, si legge come se coprisse tutto."""
    return {a.nome for a in ASSUNZIONI} - nomi_coperti()
