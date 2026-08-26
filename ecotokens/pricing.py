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


def resolve_model(name: str | None, fallback: str = DEFAULT_MODEL) -> str:
    """Normalizza il nome modello ricevuto da un client OpenAI."""
    if not name:
        return fallback
    key = name.strip()
    if key in MODELS:
        return key
    lowered = key.lower()
    if lowered in MODELS:
        return lowered
    if lowered in ALIASES:
        return ALIASES[lowered]
    # Nomi con suffisso di data che non esistono piu' nell'API attuale.
    for model_id in MODELS:
        if lowered.startswith(model_id):
            return model_id
    return fallback


def model_info(name: str) -> ModelInfo:
    return MODELS.get(resolve_model(name), MODELS[DEFAULT_MODEL])


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
