"""Ossatura della pipeline: contesto condiviso e protocollo degli stadi.

Ogni stadio vede lo stesso ``RequestContext`` e puo':

* riscrivere ``ctx.params`` prima dell'invio (compattazione, memoria, cache planner);
* interrompere la catena valorizzando ``ctx.short_circuit`` (hit di cache);
* rifiutare la richiesta sollevando ``PipelineAbort`` (budget esaurito).

``before`` gira nell'ordine dichiarato, ``after`` in ordine inverso: uno stadio
che ha modificato la richiesta e' quindi il primo a poter osservare l'esito.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..api.schemas import ChatCompletionRequest
from ..config import Settings
from ..pricing import Usage
from ..store.repos import Session, Store
from ..tokens import TokenCounter

# Fonti possibili di una risposta, per la contabilita'.
SOURCE_API = "api"
SOURCE_EXACT_CACHE = "exact_cache"
SOURCE_SEMANTIC_CACHE = "semantic_cache"

# Forma della porta da cui la richiesta e' entrata. Il gateway parla con un
# solo provider - Anthropic - ma accetta due dialetti: quello OpenAI, per le
# applicazioni che gia' esistono, e quello nativo di Claude, per i client che
# lo parlano gia' e non hanno bisogno di nessuna traduzione.
FORMAT_OPENAI = "openai"
FORMAT_ANTHROPIC = "anthropic"


class PipelineAbort(Exception):
    """Interrompe la richiesta prima di spendere token."""

    def __init__(self, message: str, *, status_code: int = 429, error_type: str = "budget_exceeded") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


@dataclass
class RequestContext:
    # Assente per le richieste native: quelle non passano da nessuna
    # traduzione, quindi non esiste una versione OpenAI da conservare.
    request: ChatCompletionRequest | None
    settings: Settings
    store: Store
    client: Any
    counter: TokenCounter
    completion_id: str

    model: str
    params: dict[str, Any]
    stream: bool
    # Dialetto della richiesta in arrivo. Gli stadi non devono guardarlo:
    # lavorano tutti sui parametri Anthropic, che sono gli stessi in
    # entrambi i casi. Serve alle rotte, per sapere come rispondere.
    client_format: str = FORMAT_OPENAI
    # Effort chiesto esplicitamente dal client, se l'ha chiesto. Il router
    # non lo tocca: e' una scelta di chi ha scritto l'applicazione.
    client_effort: str | None = None
    # Header HTTP della richiesta, in minuscolo.
    headers: dict[str, str] = field(default_factory=dict)

    notes: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    betas: list[str] = field(default_factory=list)

    session: Session | None = None
    session_is_new: bool = True
    fingerprint: str | None = None
    # Turni assistant gia' presenti nella cronologia in arrivo. E' il segnale
    # piu' affidabile per sapere se esiste gia' un prefisso in cache: non
    # dipende da cosa il gateway ha registrato in passato.
    history_turns: int = 0
    # Firma normalizzata dei messaggi cosi' come sono arrivati dal client,
    # prima di ogni riscrittura interna: e' il metro del riconoscimento di
    # sessione, e deve restare la vista del client, non la nostra.
    incoming_signature: list[str] = field(default_factory=list)

    cache_key: str | None = None
    short_circuit: dict[str, Any] | None = None
    source: str = SOURCE_API
    cache_ttl: str = "5m"

    estimated_prompt_tokens: int = 0
    # Token tolti dalla riscrittura del prompt, e quanti di quelli stavano
    # fuori dal prefisso servito da cache. La distinzione conta: un token tolto
    # al prefisso in cache vale un decimo di uno tolto alla coda.
    prompt_tokens_removed: int = 0
    prompt_tokens_removed_uncached: int = 0
    # Token che il gateway aggiunge di suo al prompt: delimitatori attorno al
    # riassunto, blocco della memoria, istruzioni di formato. L'utente li paga
    # senza averli scritti, quindi vanno contati come tutto il resto.
    overhead_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    # Compilati dallo stadio di contabilita' dopo la risposta.
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    # Spesa delle chiamate che il gateway fa per conto proprio: riassunti di
    # compattazione, estrazione dei fatti da ricordare. Non compare in
    # `response.usage` perche' non appartiene alla risposta dell'utente, ma
    # l'utente la paga. Tenerla fuori dal conto farebbe sembrare gratuito ogni
    # stadio che chiama un modello.
    aux_usage: Usage = field(default_factory=Usage)
    aux_cost_usd: float = 0.0
    # Risposta finale nel formato atteso dal client, valorizzata dalla rotta
    # prima di eseguire gli stadi `after`: gli stadi di cache la salvano da
    # qui. Il formato e' quello della porta d'ingresso, non quello interno.
    client_response: dict[str, Any] | None = None
    # La stessa risposta in formato Anthropic: e' questa che finisce in
    # cache. Il formato interno del gateway e' uno solo, e deve esserlo
    # anche quello salvato, altrimenti un hit servito a un client dell'altro
    # dialetto restituirebbe una risposta della forma sbagliata.
    upstream_response: dict[str, Any] | None = None

    @property
    def session_id(self) -> str | None:
        return self.session.id if self.session else None

    @property
    def total_cost_usd(self) -> float:
        """Quanto e' costata davvero la richiesta, chiamate interne comprese."""
        return self.cost_usd + self.aux_cost_usd

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000

    def has_tools(self) -> bool:
        """Vero se la richiesta dichiara dei tool.

        Si legge dai parametri, non dalla richiesta in arrivo: cosi' la
        risposta e' la stessa da qualunque porta sia entrata.
        """
        return bool(self.params.get("tools"))

    def note(self, message: str) -> None:
        self.notes.append(message)

    def use_beta(self, flag: str) -> None:
        """Segna un beta header necessario: la chiamata passera' da client.beta."""
        if flag not in self.betas:
            self.betas.append(flag)

    def meta(self) -> dict[str, Any]:
        """Blocco diagnostico allegato alla risposta come campo ``ecotokens``."""
        blocco = {
            "source": self.source,
            "model": self.model,
            "session_id": self.session_id,
            "cost_usd": round(self.cost_usd, 6),
            "saved_usd": round(self.saved_usd, 6),
            "cached_prompt_tokens": self.usage.cache_read_tokens,
            "notes": list(self.notes),
        }
        if self.aux_cost_usd:
            blocco["aux_cost_usd"] = round(self.aux_cost_usd, 6)
        return blocco


class Stage(Protocol):
    """Uno stadio della pipeline."""

    name: str

    async def before(self, ctx: RequestContext) -> None: ...

    async def after(self, ctx: RequestContext, message: Any | None) -> None: ...


class BaseStage:
    """Implementazione vuota: gli stadi ridefiniscono solo cio' che serve."""

    name = "stage"
    enabled = True

    async def before(self, ctx: RequestContext) -> None:  # pragma: no cover - default
        return None

    async def after(self, ctx: RequestContext, message: Any | None) -> None:  # pragma: no cover
        return None


class Pipeline:
    def __init__(self, stages: list[BaseStage]) -> None:
        self.stages = stages

    async def before(self, ctx: RequestContext) -> None:
        for stage in self.stages:
            if not getattr(stage, "enabled", True):
                continue
            await stage.before(ctx)
            if ctx.short_circuit is not None:
                # Un hit di cache rende inutile tutto il resto della catena.
                return

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        for stage in reversed(self.stages):
            if not getattr(stage, "enabled", True):
                continue
            await stage.after(ctx, message)
