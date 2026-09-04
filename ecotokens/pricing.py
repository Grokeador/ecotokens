"""Tariffe e catalogo modelli Claude, piu' il calcolo di costo e risparmio.

Questo modulo definisce cosa significa "risparmio" per EcoTokens: il costo
effettivo di una richiesta confrontato con la baseline, cioe' la stessa
richiesta senza nessuna ottimizzazione (nessuna cache, modello di default).
"""

from __future__ import annotations

from dataclasses import dataclass

# Moltiplicatori del prompt caching rispetto al prezzo base di input.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}


@dataclass(frozen=True)
class ModelInfo:
    """Un modello Claude e i parametri che servono al gateway."""

    id: str
    context_window: int
    max_output: int
    input_per_mtok: float
    output_per_mtok: float
    # Prefisso minimo perche' il prompt caching si attivi. NON e' monotono tra
    # le generazioni: sotto questa soglia la cache non viene creata e l'API non
    # segnala alcun errore.
    cache_min_tokens: int
    # I modelli che accettano {"role": "system"} dentro messages[].
    supports_mid_conversation_system: bool = False
    # Livello di capacita' per il router: piu' alto = piu' capace e costoso.
    tier: int = 0
    # Chi risponde. Serve al registro e al quadro per non mescolare provider
    # con regole di cache diverse dentro la stessa media.
    provider: str = "anthropic"
    # Falso quando le tariffe non le sappiamo. Un modello esterno di cui
    # nessuno ha dichiarato il prezzo si misura lo stesso - i token sono
    # token - ma la sua spesa non entra nei totali: un prezzo supposto
    # produce dollari che nessuno ha pagato e che nessuno vede arrivare.
    tariffa_dichiarata: bool = True


MODELS: dict[str, ModelInfo] = {
    "claude-opus-5": ModelInfo(
        id="claude-opus-5",
        context_window=1_000_000,
        max_output=128_000,
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_min_tokens=512,
        supports_mid_conversation_system=True,
        tier=30,
    ),
    "claude-opus-4-8": ModelInfo(
        id="claude-opus-4-8",
        context_window=1_000_000,
        max_output=128_000,
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_min_tokens=1024,
        supports_mid_conversation_system=True,
        tier=29,
    ),
    "claude-fable-5": ModelInfo(
        id="claude-fable-5",
        context_window=1_000_000,
        max_output=128_000,
        input_per_mtok=10.0,
        output_per_mtok=50.0,
        cache_min_tokens=512,
        supports_mid_conversation_system=True,
        tier=40,
    ),
    "claude-sonnet-5": ModelInfo(
        id="claude-sonnet-5",
        context_window=1_000_000,
        max_output=128_000,
        input_per_mtok=2.0,
        output_per_mtok=10.0,
        cache_min_tokens=1024,
        supports_mid_conversation_system=False,
        tier=20,
    ),
    "claude-sonnet-4-6": ModelInfo(
        id="claude-sonnet-4-6",
        context_window=1_000_000,
        max_output=128_000,
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_min_tokens=1024,
        supports_mid_conversation_system=False,
        tier=19,
    ),
    "claude-haiku-4-5": ModelInfo(
        id="claude-haiku-4-5",
        context_window=200_000,
        max_output=64_000,
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_min_tokens=4096,
        supports_mid_conversation_system=False,
        tier=10,
    ),
}

DEFAULT_MODEL = "claude-opus-5"

# Alias comodi per i client che mandano nomi in stile OpenAI o abbreviati.
ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
    "gpt-4": DEFAULT_MODEL,
    "gpt-4o": DEFAULT_MODEL,
    "gpt-4o-mini": "claude-haiku-4-5",
    "gpt-3.5-turbo": "claude-haiku-4-5",
    "default": DEFAULT_MODEL,
}


# Prefisso con cui si indirizza un modello che gira sulla macchina di chi lo
# usa. Il gateway non puo' dedurlo dal nome - "qwen2.5-coder" non dice dove
# stia girando - e indovinare qui costerebbe caro nelle due direzioni: un
# modello locale prezzato come remoto inventa spesa, uno remoto prezzato come
# locale la nasconde. Il prefisso e' l'unico modo di saperlo senza chiedere.
PREFISSO_LOCALE = "locale/"


# Tariffe dei modelli non-Anthropic, dichiarate in configurazione sotto
# [tariffe]. Vuoto di suo: nessun prezzo di terzi e' scritto nel codice,
# perche' i listini cambiano e un numero sbagliato qui non si vede - si
# presenta come una misura.
MODELLI_ESTERNI: dict[str, ModelInfo] = {}


def registra_modelli_esterni(tariffe: dict[str, dict]) -> None:
    """Carica le tariffe dichiarate dall'utente. Sostituisce le precedenti."""
    MODELLI_ESTERNI.clear()
    for nome, voce in (tariffe or {}).items():
        MODELLI_ESTERNI[nome.strip().lower()] = ModelInfo(
            id=nome,
            context_window=int(voce.get("context_window", 128_000)),
            max_output=int(voce.get("max_output", 16_000)),
            input_per_mtok=float(voce.get("input_per_mtok", 0.0)),
            output_per_mtok=float(voce.get("output_per_mtok", 0.0)),
            cache_min_tokens=int(voce.get("cache_min_tokens", 1024)),
            tier=int(voce.get("tier", 0)),
            provider=str(voce.get("provider", "esterno")),
            tariffa_dichiarata=True,
        )


def _info_locale(nome: str) -> ModelInfo:
    """Un modello che gira in casa: costo zero, e non e' una supposizione.

    La finestra di contesto invece lo e', ed e' l'unico numero qui che possa
    sbagliare per difetto: si dichiara in [tariffe] quando il modello ne ha
    una piu' grande.
    """
    return ModelInfo(
        id=nome,
        context_window=131_072,
        max_output=8_192,
        input_per_mtok=0.0,
        output_per_mtok=0.0,
        # Nessun provider locale offre prompt caching a prefisso: la soglia
        # sta a un valore irraggiungibile perche' il pianificatore non piazzi
        # breakpoint che nessuno legge.
        cache_min_tokens=10**9,
        provider="locale",
        tariffa_dichiarata=True,
    )


def e_locale(name: str | None) -> bool:
    return bool(name) and str(name).strip().lower().startswith(PREFISSO_LOCALE)


def _risolvi(name: str | None, fallback: str) -> tuple[str, bool]:
    """Risolve il nome e dice se e' stato **riconosciuto** o solo ripiegato.

    La distinzione conta piu' del nome: per un alias noto il ripiego e' una
    scelta, per un nome ignoto e' una supposizione, e prezzare una
    supposizione produce dollari inventati che nessuno vede (vedi
    ``tuning_log``, voce sul modello sconosciuto).
    """
    if not name:
        # Nessun nome: il default della configurazione e' una scelta esplicita.
        return fallback, True
    key = name.strip()
    if key in MODELS:
        return key, True
    lowered = key.lower()
    # Prima del catalogo Claude: un modello locale o dichiarato non deve mai
    # cadere nel ripiego, o verrebbe prezzato alle tariffe di Opus.
    if lowered.startswith(PREFISSO_LOCALE) or lowered in MODELLI_ESTERNI:
        return key, True
    if lowered in MODELS:
        return lowered, True
    if lowered in ALIASES:
        return ALIASES[lowered], True
    # Nomi con suffisso di data che non esistono piu' nell'API attuale.
    for model_id in MODELS:
        if lowered.startswith(model_id):
            return model_id, True
    return fallback, False


def resolve_model(name: str | None, fallback: str = DEFAULT_MODEL) -> str:
    """Normalizza il nome modello ricevuto da un client OpenAI."""
    return _risolvi(name, fallback)[0]


def modello_riconosciuto(name: str | None, fallback: str = DEFAULT_MODEL) -> bool:
    """Falso quando il nome non e' nel catalogo e si e' ripiegato al buio.

    Il gateway continua a funzionare lo stesso - un guasto degrada, non
    abbatte - ma il costo calcolato su queste richieste e' quello di un
    modello diverso, quindi non entra nel confronto.
    """
    return _risolvi(name, fallback)[1]


def model_info(name: str) -> ModelInfo:
    risolto = resolve_model(name)
    if e_locale(risolto):
        return _info_locale(risolto)
    esterno = MODELLI_ESTERNI.get(risolto.strip().lower())
    if esterno is not None:
        return esterno
    # Nome ignoto: resta il ripiego al default, che e' una decisione presa e
    # coperta da `test_modello_ignoto`. La spesa e' reale e va registrata; a
    # tenerla fuori dai confronti ci pensa gia' `modello_riconosciuto`.
    return MODELS.get(risolto, MODELS[DEFAULT_MODEL])


@dataclass(frozen=True)
class Usage:
    """Consumo di una singola chiamata, nei termini dell'API Anthropic.

    Attenzione: ``input_tokens`` e' soltanto il residuo NON servito da cache.
    La dimensione totale del prompt e' la somma dei tre campi di input.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens

    @classmethod
    def from_api(cls, usage: object) -> "Usage":
        """Costruisce da un oggetto ``response.usage`` dell'SDK."""

        def get(attr: str) -> int:
            value = getattr(usage, attr, None)
            return int(value) if value else 0

        return cls(
            input_tokens=get("input_tokens"),
            output_tokens=get("output_tokens"),
            cache_creation_tokens=get("cache_creation_input_tokens"),
            cache_read_tokens=get("cache_read_input_tokens"),
        )


def cost_usd(model: str, usage: Usage, cache_ttl: str = "5m") -> float:
    """Costo effettivo in dollari, moltiplicatori di cache inclusi."""
    info = model_info(model)
    write_mult = CACHE_WRITE_MULTIPLIER.get(cache_ttl, 1.25)
    input_rate = info.input_per_mtok / 1_000_000
    output_rate = info.output_per_mtok / 1_000_000
    return (
        usage.input_tokens * input_rate
        + usage.cache_creation_tokens * input_rate * write_mult
        + usage.cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
        + usage.output_tokens * output_rate
    )


def baseline_cost_usd(model: str, usage: Usage) -> float:
    """Costo che la stessa richiesta avrebbe avuto senza ottimizzazioni.

    Tutti i token di prompt pagati a prezzo pieno, nessuna cache.
    """
    info = model_info(model)
    return (
        usage.total_prompt_tokens * info.input_per_mtok / 1_000_000
        + usage.output_tokens * info.output_per_mtok / 1_000_000
    )


def baseline_ingenua_usd(
    model: str,
    usage: Usage,
    prefisso_stabile: int,
    *,
    prefisso_freddo: bool = False,
) -> float:
    """Costo della stessa richiesta per un client **senza gateway ma non ingenuo**.

    `baseline_cost_usd` prezza tutto a tariffa piena, cioe' un client che non
    usa affatto il prompt caching. Come confronto e' un fantoccio: chiunque
    integri l'API oggi mette un `cache_control` in cima al proprio system
    prompt, e Anthropic gli sconta il prefisso stabile senza chiedere niente in
    cambio. Attribuire quel risparmio al gateway vuol dire prendersi il merito
    di una funzione che c'e' comunque - ed e' la meta' piu' grossa del numero.

    Il modello dichiarato e' il piu' semplice che sia realistico: un solo
    breakpoint alla fine del blocco `system`, che nell'ordine di render cattura
    anche i tool. Il prefisso stabile si paga una scrittura la prima volta e
    letture dopo; tutto il resto - la conversazione, che cambia a ogni turno -
    a tariffa piena.

    `prefisso_freddo` va valorizzato con **lo stesso stato in cui eravamo noi**,
    e chi chiama lo ricava da `cache_read_tokens`: se abbiamo riletto, il
    prefisso era caldo per tutti e due. Confrontare un concorrente caldo con
    noi freddi - o viceversa - e' la trappola che questo progetto ha gia'
    calpestato due volte, e da' comodamente il risultato che si sperava.

    E' una stima, e sta fra le assunzioni dichiarate del progetto: usa il
    conteggio locale del prefisso, non quello dell'API.
    """
    info = model_info(model)
    stabile = max(0, min(prefisso_stabile, usage.total_prompt_tokens))
    # Sotto la soglia minima del modello nessuna cache si forma, nemmeno per
    # lui: allora la baseline realistica coincide con quella a prezzo pieno.
    if stabile < info.cache_min_tokens:
        return baseline_cost_usd(model, usage)

    resto = usage.total_prompt_tokens - stabile
    moltiplicatore = (
        CACHE_WRITE_MULTIPLIER["5m"] if prefisso_freddo else CACHE_READ_MULTIPLIER
    )
    return (
        stabile * info.input_per_mtok * moltiplicatore / 1_000_000
        + resto * info.input_per_mtok / 1_000_000
        + usage.output_tokens * info.output_per_mtok / 1_000_000
    )


def savings_usd(model: str, usage: Usage, cache_ttl: str = "5m") -> float:
    """Differenza tra baseline e costo effettivo. Puo' essere negativa.

    Un valore negativo significa che abbiamo pagato piu' del necessario: e'
    quasi sempre una scrittura in cache mai riletta, ed e' esattamente il caso
    che il cache planner deve evitare.
    """
    return baseline_cost_usd(model, usage) - cost_usd(model, usage, cache_ttl)


def cheaper_models(model: str) -> list[str]:
    """Modelli davvero piu' economici del dato, dal piu' capace al meno capace.

    Il confronto e' sul prezzo, non sul tier: due modelli possono avere
    capacita' diverse e identica tariffa, e scendere di tier senza scendere di
    prezzo non e' un risparmio, e' solo una perdita di qualita'.
    """
    info = model_info(model)
    candidates = [
        m
        for m in MODELS.values()
        if m.tier < info.tier and m.input_per_mtok < info.input_per_mtok
    ]
    return [m.id for m in sorted(candidates, key=lambda m: m.tier, reverse=True)]
