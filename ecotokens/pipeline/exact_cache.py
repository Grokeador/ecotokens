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
from ..translate.from_anthropic import cached_response_copy
from .base import SOURCE_EXACT_CACHE, BaseStage, RequestContext


def compute_cache_key(ctx: RequestContext) -> str:
    request = ctx.request
    payload = {
        "model": ctx.model,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "tool_calls": [call.model_dump() for call in message.tool_calls or []],
                "tool_call_id": message.tool_call_id,
            }
            for message in request.messages
        ],
        "tools": [tool.model_dump() for tool in request.tools or []],
        "functions": [fn.model_dump() for fn in request.functions or []],
        "tool_choice": request.tool_choice,
        "max_tokens": request.resolved_max_tokens(),
        "response_format": request.response_format.model_dump()
        if request.response_format
        else None,
        "reasoning_effort": request.reasoning_effort,
        "stop": request.stop,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ExactCacheStage(BaseStage):
    name = "exact_cache"

    def __init__(self, settings: Any) -> None:
        self.config = settings.exact_cache
        self.enabled = self.config.enabled

    def _skip(self, ctx: RequestContext) -> bool:
        # Con i tool in gioco la risposta dipende da uno stato esterno che la
        # cache non conosce: servirla dal disco significherebbe rispondere con
        # dati potenzialmente vecchi.
        return self.config.skip_when_tools and ctx.request.has_tools()

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
        ctx.short_circuit = cached_response_copy(entry.response, ecotokens_meta=ctx.meta())

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        if message is None or ctx.cache_key is None or ctx.source != "api":
            return
        if self._skip(ctx):
            return
        response = ctx.openai_response
        if not response:
            return
        await ctx.store.put_cached(
            ctx.cache_key,
            ctx.model,
            response,
            ctx.usage,
            self.config.ttl_seconds,
        )
