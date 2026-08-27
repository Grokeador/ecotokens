"""Tutto il testo che il gateway inserisce nei prompt, in un posto solo.

Il gateway non si limita a inoltrare: aggiunge testo suo. Delimitatori attorno
al riassunto della cronologia, un blocco per i fatti ricordati, un'istruzione
quando il client chiede JSON, le regole date al riassuntore. Sono token che
l'utente paga senza averli scritti, a ogni richiesta.

Sparsi per il codice erano invisibili: nessuno li contava e ognuno era scritto
con il tono del file in cui capitava. Raccolti qui diventano una voce di costo
come le altre, con tre conseguenze utili:

* si possono **contare** - ``ctx.overhead_tokens`` li somma richiesta per
  richiesta, e il banco di misura li riporta;
* si possono **riscrivere** senza rischio - a differenza del prompt
  dell'utente, questo testo e' nostro, e accorciarlo non cambia il
  comportamento di nessuna applicazione;
* si possono **confrontare** - ``LEGACY`` conserva la formulazione precedente,
  cosi' il guadagno e' verificabile invece che dichiarato.

Il criterio di riscrittura e' quello che si userebbe per un prompt qualsiasi:
niente cortesie, niente perifrasi, delimitatori corti ma ancora leggibili. Un
tag serve a separare, non a spiegare: ``<storico>`` delimita esattamente quanto
``<riassunto-conversazione-precedente>`` e costa un quarto.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tokens import estimate_tokens

# --- delimitatori ---------------------------------------------------------

# Riassunto della parte vecchia della conversazione, inserito dallo stadio di
# contesto al posto dei messaggi compattati.
SUMMARY_OPEN = "<storico>"
SUMMARY_CLOSE = "</storico>"

# Fatti ricordati, iniettati in coda dallo stadio di memoria.
MEMORY_OPEN = "<note>"
MEMORY_CLOSE = "</note>"

# Messaggio di sistema arrivato a meta' conversazione su un modello che non
# supporta il ruolo system nei messages: diventa testo in un turno utente.
OPERATOR_OPEN = "<sistema>"
OPERATOR_CLOSE = "</sistema>"

# Materiale passato al riassuntore quando il riassunto e' incrementale.
NOTES_OPEN = "<note>"
NOTES_CLOSE = "</note>"
NEW_OPEN = "<nuovi>"
NEW_CLOSE = "</nuovi>"


def wrap(open_tag: str, close_tag: str, body: str) -> str:
    return f"{open_tag}\n{body}\n{close_tag}"


# --- istruzioni -----------------------------------------------------------

# Il client ha chiesto response_format json_object, che l'API non ha: diventa
# un'istruzione in coda, dove non tocca il prefisso in cache.
JSON_OBJECT = "Rispondi solo con un oggetto JSON valido."

# Regole per il riassuntore. Chiedono appunti, non prosa: la prosa ricostruisce
# il contesto con frasi complete, e le frasi complete sono i token che stiamo
# cercando di togliere.
SUMMARY_RULES = (
    "Appunti telegrafici della conversazione.\n"
    "Tieni: decisioni, vincoli, dati concreti (nomi, numeri, percorsi, id), aperture.\n"
    "Togli: convenevoli, ripetizioni, cio' che si ricava da solo.\n"
    "Una riga per fatto, aperta da '- '. Nessun preambolo. Max {righe} righe."
)

MERGE_RULES = (
    "Aggiorna gli appunti con i messaggi nuovi. Un solo elenco.\n"
    "Fondi i ridondanti, sostituisci i superati, lascia invariati gli altri.\n"
    "Tieni: decisioni, vincoli, dati concreti, aperture.\n"
    "Una riga per fatto, aperta da '- '. Nessun preambolo. Max {righe} righe."
)

# Regole per l'estrattore di memoria.
EXTRACTION_RULES = (
    "Estrai i fatti stabili da ricordare nei prossimi scambi: preferenze, vincoli, "
    "decisioni, dati concreti (nomi, versioni, percorsi).\n"
    "Ignora cio' che vale solo per questo turno.\n"
    "Rispondi con un array JSON di stringhe, massimo 5. Se non c'e' nulla: []."
)

# Segnaposto nella trascrizione data al riassuntore. Al riassunto serve sapere
# che una chiamata c'e' stata, non cosa ha restituito.
TOOL_CALL = "[>{name}]"
TOOL_RESULT = "[esito]"


# --- contabilita' ---------------------------------------------------------


@dataclass(frozen=True)
class Wording:
    """Una voce di testo del gateway, con la sua formulazione precedente."""

    key: str
    purpose: str
    text: str
    legacy: str
    # Quante volte compare in una richiesta che la attiva. I delimitatori sono
    # due (apertura e chiusura), le istruzioni una sola.
    per_use: int = 1

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text) * self.per_use

    @property
    def legacy_tokens(self) -> int:
        return estimate_tokens(self.legacy) * self.per_use

    @property
    def saved(self) -> int:
        return self.legacy_tokens - self.tokens


# Formulazione precedente di ogni voce, per rendere il confronto verificabile.
# Non e' nostalgia: senza il termine di paragone "abbiamo accorciato" resta
# un'affermazione, e questo progetto non ne accetta.
CATALOG: tuple[Wording, ...] = (
    Wording(
        key="riassunto",
        purpose="delimita il riassunto della cronologia compattata",
        text=SUMMARY_OPEN,
        legacy="<riassunto-conversazione-precedente>",
        per_use=2,
    ),
    Wording(
        key="memoria",
        purpose="delimita i fatti ricordati iniettati in coda",
        text=MEMORY_OPEN,
        legacy="<memoria-rilevante>",
        per_use=2,
    ),
    Wording(
        key="sistema",
        purpose="delimita un messaggio di sistema arrivato a meta' conversazione",
        text=OPERATOR_OPEN,
        legacy="<operator-instruction>",
        per_use=2,
    ),
    Wording(
        key="json",
        purpose="istruzione quando il client chiede response_format json_object",
        text=JSON_OBJECT,
        legacy=(
            "Rispondi esclusivamente con un singolo oggetto JSON valido, "
            "senza testo attorno."
        ),
    ),
    Wording(
        key="regole-riassunto",
        purpose="istruzioni date al riassuntore di compattazione",
        text=SUMMARY_RULES,
        legacy=(
            "Riassumi la conversazione qui sotto in un massimo di 15 punti elenco. "
            "Conserva: decisioni prese, vincoli, dati concreti (nomi, numeri, percorsi "
            "di file), e le richieste ancora aperte. Ometti convenevoli e ripetizioni. "
            "Rispondi solo con il riassunto, senza introduzioni."
        ),
    ),
    Wording(
        key="regole-memoria",
        purpose="istruzioni date all'estrattore di memoria",
        text=EXTRACTION_RULES,
        legacy=(
            "Estrai dalla conversazione i fatti stabili che varra' la pena ricordare nei "
            "prossimi scambi: preferenze dell'utente, vincoli, decisioni prese, dati "
            "concreti (nomi, versioni, percorsi). Ignora tutto cio' che vale solo per "
            "questo turno. Rispondi con un array JSON di stringhe, al massimo 5 elementi. "
            "Se non c'e' nulla da ricordare rispondi []."
        ),
    ),
    Wording(
        key="esito-tool",
        purpose="segnaposto di un risultato di tool nella trascrizione",
        text=TOOL_RESULT,
        legacy="[risultato di tool]",
    ),
    Wording(
        key="chiamata-tool",
        purpose="segnaposto di una chiamata di tool nella trascrizione",
        text=TOOL_CALL.format(name="read_file"),
        legacy="[chiamata a read_file]",
    ),
)


def catalog_totals() -> dict[str, int]:
    """Somma delle voci: quanto costava prima, quanto costa adesso."""
    prima = sum(voce.legacy_tokens for voce in CATALOG)
    adesso = sum(voce.tokens for voce in CATALOG)
    return {"before": prima, "after": adesso, "saved": prima - adesso}
