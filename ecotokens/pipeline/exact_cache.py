"""Cache esatta delle risposte.

Una richiesta identica a una gia' vista viene servita dal disco: zero token,
zero latenza di rete, zero rischio, perche' l'input e' lo stesso byte per byte.

La chiave si calcola sulla richiesta OpenAI normalizzata, non sui parametri
Anthropic gia' riscritti: cosi' resta stabile anche se memoria, compattazione o
router cambiano il prompt effettivo, e due richieste identiche del client
finiscono sulla stessa voce.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..pricing import Usage, baseline_cost_usd
from ..prompt_opt import normalize
from ..translate.from_anthropic import native_response_copy
from .base import SOURCE_EXACT_CACHE, BaseStage, RequestContext


def compute_cache_key(ctx: RequestContext) -> str:
    """Identita' di una richiesta ai fini della cache.

    Si calcola sui parametri Anthropic, non sulla richiesta cosi' com'e'
    arrivata, per due motivi. Il primo: sono gli stessi da entrambe le porte
    del gateway, quindi la stessa domanda posta in dialetto OpenAI o in
    dialetto nativo finisce sulla stessa voce, invece di pagarsi due volte. Il
    secondo: questo stadio gira prima di memoria, compattazione e router,
    quindi qui i parametri sono ancora quelli di partenza.

    Il testo viene normalizzato: due richieste che differiscono per uno spazio
    doppio o una virgoletta tipografica sono la stessa domanda.
    """
    ripulisci = normalize if ctx.settings.exact_cache.normalize_key else (lambda value: value)
    params = ctx.params
    payload = {
        "model": ctx.model,
        "system": _clean_content(params.get("system"), ripulisci),
        "messages": [
            {
                "role": message.get("role"),
                "content": _clean_content(message.get("content"), ripulisci),
            }
            for message in params.get("messages") or []
        ],
        "tools": params.get("tools"),
        "tool_choice": params.get("tool_choice"),
        "max_tokens": params.get("max_tokens"),
        "output_config": params.get("output_config"),
        "stop_sequences": params.get("stop_sequences"),
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _clean_content(content: Any, ripulisci: Any) -> Any:
    """Riduce un contenuto alla sua forma canonica, per il solo calcolo della chiave.

    Due normalizzazioni, con lo stesso scopo: far combaciare richieste che
    dicono la stessa cosa scrivendola diversamente.

    La prima e' sulla *spaziatura*. Due richieste che differiscono per uno
    spazio doppio o una virgoletta tipografica sono la stessa domanda: tenerle
    su voci diverse significa pagare due volte la stessa risposta.

    La seconda e' sulla *forma*. Un contenuto puo' essere una stringa o una
    lista con un solo blocco di testo: per l'API sono la stessa cosa, e le due
    porte del gateway ne producono una ciascuna. Senza ridurle a una forma
    comune, la stessa domanda posta in dialetto OpenAI e in dialetto nativo
    finirebbe su due voci di cache diverse.

    Vale solo per la chiave: la richiesta inviata all'API resta quella che il
    client ha scritto.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return [{"type": "text", "text": ripulisci(content)}]
    if isinstance(content, list):
        return [
            {"type": "text", "text": ripulisci(str(parte.get("text", "")))}
            if isinstance(parte, dict) and parte.get("type") == "text"
            else parte
            for parte in content
        ]
    return content


class ExactCacheStage(BaseStage):
    name = "exact_cache"

    def __init__(self, settings: Any) -> None:
        self.config = settings.exact_cache
        self.enabled = self.config.enabled

    def _skip(self, ctx: RequestContext) -> bool:
        # Con i tool in gioco la risposta dipende da uno stato esterno che la
        # cache non conosce: servirla dal disco significherebbe rispondere con
        # dati potenzialmente vecchi.
        return self.config.skip_when_tools and ctx.has_tools()

    async def before(self, ctx: RequestContext) -> None:
        if self._skip(ctx):
            return
        ctx.cache_key = compute_cache_key(ctx)
        entry = await ctx.store.get_cached(ctx.cache_key)
        if entry is None:
            return

        # Il risparmio e' l'intero costo che la chiamata avrebbe avuto.
        avoided = baseline_cost_usd(entry.model, entry.usage)
        ctx.source = SOURCE_EXACT_CACHE
        ctx.usage = Usage()
        ctx.cost_usd = 0.0
        ctx.saved_usd = avoided
        ctx.note(
            f"risposta servita dalla cache esatta (hit #{entry.hits}), "
            f"{entry.usage.total_prompt_tokens + entry.usage.output_tokens} token non spesi"
        )
        # Si restituisce il formato canonico: e' la rotta a sapere in che
        # dialetto risponde il client che ha chiesto.
        ctx.short_circuit = native_response_copy(entry.response, ecotokens_meta=ctx.meta())

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        if message is None or ctx.cache_key is None or ctx.source != "api":
            return
        if self._skip(ctx):
            return
        response = ctx.upstream_response
        if not response:
            return
        await ctx.store.put_cached(
            ctx.cache_key,
            ctx.model,
            response,
            ctx.usage,
            self.config.ttl_seconds,
        )
