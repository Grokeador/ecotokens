"""Riscrittura del prompt per spendere meno token.

Tre livelli, in ordine di rischio crescente. Nessuno e' acceso perche' sembra
una buona idea: ognuno ha un vincolo che lo governa.

**1. Normalizzazione** (senza perdita, attiva di default). Spazi ripetuti, righe
vuote in eccesso, spazi in coda, spazi unificatori, caratteri a larghezza zero,
virgolette tipografiche. Non cambia una parola del testo, quindi non cambia cosa
il modello capisce. E' l'unico livello che si puo' accendere senza pensarci.

**2. Sostituzioni lessicali** (spenta di default). Sinonimi piu' corti: "al fine
di" per "per", "utilizzare" per "usare". Qui c'e' un problema di metodo che va
detto invece che nascosto: **piu' corto in caratteri non significa piu' corto in
token**. Il tokenizer di Claude non e' pubblico e non e' ricostruibile a mano;
l'unica autorita' e' ``messages.count_tokens``. Percio' la tabella qui sotto e'
un elenco di *candidati*, non di risparmi accertati, e con
``verify_with_api`` attivo una sostituzione viene applicata solo dopo che il
conteggio vero l'ha confermata. Le altre restano inerti.

**3. Riscrittura con un modello** (spenta di default). Un modello economico
riformula il prompt di sistema in forma telegrafica. E' il livello piu' potente
e il piu' pericoloso, perche' cambia le parole che governano il comportamento
dell'applicazione.

Un vincolo attraversa tutti e tre e non e' negoziabile: **le trasformazioni
devono essere deterministiche e idempotenti**. Un client OpenAI rispedisce
l'intera cronologia a ogni turno, quindi lo stesso testo passa di qui molte
volte. Se il risultato cambiasse fra un passaggio e l'altro, cambierebbe il
prefisso del prompt e salterebbe la cache: si risparmierebbero token pagandoli
dieci volte tanto. E' lo stesso errore gia' trovato nella compattazione.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# --- livello 1: normalizzazione senza perdita ----------------------------

# Caratteri invisibili che occupano token senza dire nulla. Arrivano da copia e
# incolla dal web o da editor che li inseriscono da soli.
_INVISIBILI = dict.fromkeys(
    map(ord, "​‌‍⁠﻿­"), None
)

# Sostituzioni carattere per carattere che non alterano il significato.
_EQUIVALENTI = {
    " ": " ",  # spazio unificatore
    " ": " ",
    " ": " ",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "′": "'",
    "″": '"',
}

_SPAZI_RIPETUTI = re.compile(r"[ \t]{2,}")
_SPAZI_IN_CODA = re.compile(r"[ \t]+$", re.MULTILINE)
_RIGHE_VUOTE = re.compile(r"\n{3,}")
# Blocchi di codice recintati: dentro, gli spazi contano.
_RECINTO = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)


def normalize(text: str) -> str:
    """Ripulisce il testo senza toccarne una parola.

    I blocchi di codice recintati vengono lasciati intatti: li' l'indentazione
    e' significato, non spreco.
    """
    if not text:
        return text
    pezzi = _RECINTO.split(text)
    for indice, pezzo in enumerate(pezzi):
        if indice % 2 == 1:  # dentro un recinto
            continue
        pezzi[indice] = _normalizza_fuori_dal_codice(pezzo)
    return "".join(pezzi).strip()


def _normalizza_fuori_dal_codice(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_INVISIBILI)
    for prima, dopo in _EQUIVALENTI.items():
        text = text.replace(prima, dopo)
    text = _SPAZI_RIPETUTI.sub(" ", text)
    text = _SPAZI_IN_CODA.sub("", text)
    text = _RIGHE_VUOTE.sub("\n\n", text)
    return text


# --- livello 2: riempitivi e sostituzioni lessicali ------------------------

# Formule che introducono un'istruzione senza aggiungerle nulla. Un modello
# esegue "Rispondi in italiano" esattamente come "E' importante notare che devi
# rispondere in italiano".
FILLER = (
    "e' importante notare che",
    "e' importante sottolineare che",
    "tieni presente che",
    "ti prego di",
    "per favore",
    "vorrei che tu",
    "il tuo compito e' quello di",
    "il tuo compito e' di",
    "sarebbe utile se",
    "mi piacerebbe che",
    "it is important to note that",
    "it is important to remember that",
    "please note that",
    "i would like you to",
    "your task is to",
    "it would be great if you could",
    "please kindly",
    "as an ai assistant,",
    "as an ai language model,",
)


@dataclass(frozen=True)
class Substitution:
    """Un candidato alla sostituzione.

    ``verified`` resta ``None`` finche' ``messages.count_tokens`` non ha detto
    la sua: e' l'unica autorita' sul numero di token, e senza credenziali non
    la si puo' interpellare.
    """

    source: str
    target: str
    note: str = ""


# Candidati, non verita' acquisite. Ogni voce accorcia il testo in caratteri;
# se accorci anche in token lo dice `ecotokens substitutions --live`.
SUBSTITUTIONS: tuple[Substitution, ...] = (
    # italiano
    Substitution("al fine di", "per"),
    Substitution("allo scopo di", "per"),
    Substitution("in modo tale da", "per"),
    Substitution("nel caso in cui", "se"),
    Substitution("qualora si verifichi che", "se"),
    Substitution("per quanto riguarda", "su"),
    Substitution("in relazione a", "su"),
    Substitution("a causa del fatto che", "perche'"),
    Substitution("dal momento che", "poiche'"),
    Substitution("e' necessario che", "deve"),
    Substitution("e' possibile che", "puo'"),
    Substitution("hai la possibilita' di", "puoi"),
    Substitution("effettuare", "fare"),
    Substitution("utilizzare", "usare"),
    Substitution("utilizza", "usa"),
    Substitution("successivamente", "poi"),
    Substitution("precedentemente", "prima"),
    Substitution("attualmente", "ora"),
    Substitution("nella maggior parte dei casi", "di solito"),
    Substitution("un numero elevato di", "molti"),
    # inglese
    Substitution("in order to", "to"),
    Substitution("so as to", "to"),
    Substitution("in the event that", "if"),
    Substitution("due to the fact that", "because"),
    Substitution("for the reason that", "because"),
    Substitution("at this point in time", "now"),
    Substitution("in the near future", "soon"),
    Substitution("make use of", "use"),
    Substitution("utilize", "use"),
    Substitution("is able to", "can"),
    Substitution("has the ability to", "can"),
    Substitution("a large number of", "many"),
    Substitution("the majority of", "most"),
    Substitution("with regard to", "about"),
    Substitution("in spite of the fact that", "although"),
    Substitution("subsequent to", "after"),
    Substitution("prior to", "before"),
)


def _catena_chiusa(sostituzioni: tuple[Substitution, ...]) -> list[str]:
    """Trova le sostituzioni che si innescano a vicenda.

    Se ``a -> b`` e ``b -> c`` convivono, applicare due volte da' risultati
    diversi: il testo non e' piu' stabile fra i turni e la cache salta. Questa
    verifica gira nei test, non a ogni richiesta.
    """
    bersagli = {s.target.lower() for s in sostituzioni}
    return [s.source for s in sostituzioni if s.source.lower() in bersagli]


@dataclass
class OptimizerConfig:
    """Cosa applicare. Rispecchia la configurazione dello stadio."""

    normalize_text: bool = True
    strip_filler: bool = False
    substitute: bool = False
    # Sostituzioni che il conteggio vero ha confermato. Vuoto = nessuna
    # confermata, e con `only_verified` attivo non se ne applica nessuna.
    verified: frozenset[str] = frozenset()
    only_verified: bool = True


@dataclass
class OptimizationResult:
    text: str
    applied: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def optimize_text(text: str, config: OptimizerConfig) -> OptimizationResult:
    """Applica i livelli attivi. Deterministica e idempotente."""
    if not text or not text.strip():
        return OptimizationResult(text=text)

    originale = text
    applicati: list[str] = []

    if config.normalize_text:
        ripulito = normalize(text)
        if ripulito != text:
            applicati.append("normalizzazione")
            text = ripulito

    if config.strip_filler:
        senza, quanti = _togli_filler(text)
        if quanti:
            applicati.append(f"{quanti} formule di riempimento")
            text = senza

    if config.substitute:
        sostituito, quante = _sostituisci(text, config)
        if quante:
            applicati.append(f"{quante} sostituzioni lessicali")
            text = sostituito

    if text == originale:
        return OptimizationResult(text=originale)
    return OptimizationResult(text=text, applied=applicati)


def _togli_filler(text: str) -> tuple[str, int]:
    quanti = 0
    for formula in FILLER:
        schema = re.compile(rf"\b{re.escape(formula)}\s*", re.IGNORECASE)
        text, sostituzioni = schema.subn("", text)
        quanti += sostituzioni
    if quanti:
        # Togliere "Per favore," lascia una virgola all'inizio della frase:
        # va tolta, altrimenti si e' risparmiato un token e peggiorato il testo.
        text = _PUNTEGGIATURA_ORFANA.sub(r"\1", text)
        # Togliere una formula in testa lascia una minuscola dove serve una
        # maiuscola: si rimette, altrimenti il testo si legge peggio senza
        # risparmiare nulla in piu'.
        text = _rimaiuscola(text)
        text = _SPAZI_RIPETUTI.sub(" ", text).strip()
    return text, quanti


# Virgola o punto e virgola rimasti in testa a una frase dopo la potatura.
_PUNTEGGIATURA_ORFANA = re.compile(r"(^|[.!?]\s+|\n)\s*[,;]\s*")


# Inizio di frase vero: apertura del testo, dopo una punteggiatura che chiude,
# dopo un elenco puntato o dopo una riga vuota. Non dopo un semplice a capo: li'
# la frase continua, e maiuscolarla peggiora il testo senza risparmiare nulla.
_INIZIO_FRASE = re.compile(r"(^|[.!?:]\s+|\n\s*[-*]\s+|\n\s*\n\s*)([a-zàèéìòù])")


def _rimaiuscola(text: str) -> str:
    return _INIZIO_FRASE.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def _sostituisci(text: str, config: OptimizerConfig) -> tuple[str, int]:
    quante = 0
    for voce in SUBSTITUTIONS:
        if config.only_verified and voce.source not in config.verified:
            continue
        schema = re.compile(rf"\b{re.escape(voce.source)}\b", re.IGNORECASE)
        text, fatte = schema.subn(lambda m: _come_originale(m.group(0), voce.target), text)
        quante += fatte
    return text, quante


def _come_originale(originale: str, sostituto: str) -> str:
    """Conserva la maiuscola iniziale dell'originale."""
    if originale[:1].isupper():
        return sostituto[:1].upper() + sostituto[1:]
    return sostituto
