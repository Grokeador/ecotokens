"""Configurazione: file TOML + variabili d'ambiente.

Ogni stadio della pipeline si accende e si spegne da qui. I default sono
scelti per essere sicuri: le ottimizzazioni che possono cambiare il contenuto
di una risposta (cache semantica, cambio di modello) sono disattivate.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .pricing import DEFAULT_MODEL

CONFIG_ENV_VAR = "ECOTOKENS_CONFIG"
DEFAULT_CONFIG_NAMES = ("ecotokens.toml",)


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    # Se valorizzata, i client devono presentarla in Authorization: Bearer.
    # Non e' la chiave Anthropic: e' la chiave del gateway.
    api_key: str | None = None
    log_level: str = "info"


class UpstreamSettings(BaseModel):
    """Come il gateway parla con l'API Anthropic."""

    # Lasciare vuoto: l'SDK risolve da solo ANTHROPIC_API_KEY,
    # ANTHROPIC_AUTH_TOKEN o un profilo creato con `ant auth login`.
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 600.0
    max_retries: int = 2
    default_model: str = DEFAULT_MODEL
    # Applicati quando il client non li specifica.
    default_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    default_max_tokens: int = 16_000
    default_max_tokens_stream: int = 64_000
    adaptive_thinking: bool = True
    thinking_display: Literal["omitted", "summarized"] = "omitted"


class SessionSettings(BaseModel):
    enabled: bool = True
    # Quanti messaggi iniziali normalizzati entrano nel fingerprint.
    fingerprint_depth: int = 3
    # Dopo quanto una sessione inattiva non viene piu' riconosciuta.
    ttl_hours: int = 72


class CachePlannerSettings(BaseModel):
    """Piazzamento automatico dei breakpoint cache_control."""

    enabled: bool = True
    # L'API ne accetta al massimo 4 per richiesta.
    max_breakpoints: int = 4
    # La finestra di lookback e' di 20 blocchi: oltre quella distanza il
    # breakpoint non trova la voce precedente e la cache manca in silenzio.
    intermediate_every_blocks: int = 15
    # Saltare il primo turno sembra prudente e invece costa: il pareggio e' a 2
    # richieste, e il prefisso piu' grosso - prompt di sistema e definizioni dei
    # tool - e' condiviso fra richieste *diverse*, non solo fra i turni della
    # stessa conversazione. La scrittura del primo turno viene quindi riletta
    # dalla richiesta successiva di chiunque abbia lo stesso system prompt.
    # Misurato con `ecotokens optimize`: attivarlo costa il 6% in piu' sul mix
    # standard, e fino al 155% su venti richieste isolate che condividono il
    # system prompt. Da accendere solo se ogni richiesta ha un prefisso unico.
    skip_first_turn: bool = False
    # TTL 1h solo per sessioni lunghe con pause: la scrittura costa 2x e
    # servono almeno 3 richieste per rientrare.
    long_ttl_min_turns: int = 3
    long_ttl_min_gap_seconds: int = 300


class ContextSettings(BaseModel):
    """Gestione della finestra di contesto."""

    enabled: bool = True
    # Rimozione dei vecchi tool result / blocchi di pensiero (beta API).
    # Attenzione: **non e' un'ottimizzazione di costo, e' una difesa contro
    # l'overflow di contesto.** Misurato con `ecotokens ablate`: potare sposta
    # il confine di taglio a ogni turno, quindi cambia il prefisso e distrugge
    # il prompt caching. Sul carico di costruzione di questo progetto la quota
    # di prompt servita da cache crolla dall'89% al 27% e il costo sale del 35%;
    # su un ciclo agentico con risultati molto grossi invece conviene (+10%).
    # Per questo la soglia resta alta: si pota solo quando l'alternativa e'
    # sforare la finestra.
    clear_tool_uses: bool = True
    clear_thinking: bool = False
    # Frazione della finestra del modello oltre la quale si pota.
    trigger_ratio: float = 0.6
    # Oltre questa frazione la potatura non basta e si riassume la parte
    # vecchia della conversazione.
    hard_ratio: float = 0.85
    # Modello usato per riassumere. Il riassunto viene calcolato una volta e
    # poi riusato alla lettera: se cambiasse a ogni turno cambierebbe il
    # prefisso del prompt e la cache mancherebbe sempre.
    summary_model: str = "claude-haiku-4-5"
    local_compaction: bool = True
    # Messaggi recenti che restano sempre integrali, mai riassunti.
    keep_recent_messages: int = 8


class MemorySettings(BaseModel):
    enabled: bool = False
    # Modello economico usato per estrarre i fatti da ricordare.
    extraction_model: str = "claude-haiku-4-5"
    max_facts_injected: int = 8
    max_fact_chars: int = 400


class ExactCacheSettings(BaseModel):
    enabled: bool = True
    ttl_seconds: int = 86_400
    # Non servire da cache le richieste con tool: il risultato dipende da uno
    # stato esterno che la cache non conosce.
    skip_when_tools: bool = True
    max_entries: int = 5_000


class SemanticCacheSettings(BaseModel):
    """Disattivata di default, e per una ragione.

    Servire una risposta "abbastanza simile" e' un rischio di correttezza, non
    un'ottimizzazione neutra: due domande vicine nello spazio degli embedding
    possono avere risposte giuste diverse.
    """

    enabled: bool = False
    model_name: str = "BAAI/bge-small-en-v1.5"
    similarity_threshold: float = 0.97
    ttl_seconds: int = 3_600
    max_candidates: int = 200


class RouterSettings(BaseModel):
    enabled: bool = True
    # Livello 1: abbassa l'effort sulle richieste semplici. Non invalida la
    # cache e non cambia modello: e' il risparmio sicuro.
    effort_downshift: bool = True
    simple_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Si misura la domanda, non l'intero prompt. La difficolta' sta in cio' che
    # viene chiesto: un system prompt da 5000 token non rende difficile un
    # "che ore sono". Misurare il prompt intero significava non abbassare mai
    # l'effort, perche' qualsiasi system prompt reale supera la soglia.
    simple_max_question_tokens: int = 120
    # Livello 2: cambio di modello. Le cache sono legate al modello, quindi
    # cambiarlo a meta' conversazione azzera il prompt caching.
    model_downgrade: bool = False
    # Se attivo, il modello si sceglie una volta per sessione e non cambia.
    model_locked_per_session: bool = True
    downgrade_target: str = "claude-haiku-4-5"


class BudgetSettings(BaseModel):
    enabled: bool = False
    daily_usd: float = 5.0
    monthly_usd: float = 100.0
    # Preventivo esatto con count_tokens prima di inviare. Costa una chiamata
    # in piu' (non fatturata) ma evita sforamenti.
    precount: bool = False


class StorageSettings(BaseModel):
    path: str = "ecotokens.db"
    # Conserva il testo dei messaggi. Disattivare per non tenere a disco il
    # contenuto delle conversazioni: le statistiche continuano a funzionare.
    store_message_content: bool = True


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    cache_planner: CachePlannerSettings = Field(default_factory=CachePlannerSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    exact_cache: ExactCacheSettings = Field(default_factory=ExactCacheSettings)
    semantic_cache: SemanticCacheSettings = Field(default_factory=SemanticCacheSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)


def _find_config(explicit: str | Path | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    return None


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Applica le variabili ECOTOKENS_<SEZIONE>__<CAMPO>."""
    for raw_key, value in os.environ.items():
        if not raw_key.startswith("ECOTOKENS_") or "__" not in raw_key:
            continue
        remainder = raw_key[len("ECOTOKENS_") :]
        section, _, field = remainder.partition("__")
        section, field = section.lower(), field.lower()
        if section not in Settings.model_fields:
            continue
        data.setdefault(section, {})[field] = value
    return data


def load_settings(path: str | Path | None = None) -> Settings:
    """Carica la configurazione da TOML, poi applica gli override d'ambiente."""
    data: dict[str, Any] = {}
    config_path = _find_config(path)
    if config_path is not None:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    data = _apply_env_overrides(data)
    return Settings.model_validate(data)
