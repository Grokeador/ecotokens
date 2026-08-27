"""Cache semantica: risposte riusate per domande diverse ma equivalenti.

Disattivata di default, e la ragione va detta chiaramente: servire una risposta
"abbastanza simile" non e' un'ottimizzazione neutra come le altre, e' una
scelta che puo' restituire una risposta sbagliata. "Quanto fa 2+2" e "quanto fa
2+3" sono vicinissime nello spazio degli embedding e hanno risposte diverse.

Per questo, quando e' attiva, lavora con vincoli stretti: soglia di similarita'
alta, mai con i tool in gioco, TTL breve, e confronto solo tra richieste che
condividono lo stesso contesto precedente.

Le dipendenze (``fastembed``, ``numpy``) sono opzionali: si installano con
``pip install ecotokens[semantic]``. Senza di esse lo stadio si spegne da solo
invece di far fallire il gateway.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from ..pricing import Usage, baseline_cost_usd
from ..prompt_opt import normalize
from ..translate.from_anthropic import native_response_copy
from .base import SOURCE_SEMANTIC_CACHE, BaseStage, RequestContext

logger = logging.getLogger("ecotokens.semantic")


class SemanticCacheStage(BaseStage):
    name = "semantic_cache"

    def __init__(self, settings: Any) -> None:
        self.config = settings.semantic_cache
        self.enabled = self.config.enabled
        self._embedder: Any = None
        self._numpy: Any = None
        if self.enabled:
            self._load_backend()

    def _load_backend(self) -> None:
        try:
            import numpy
            from fastembed import TextEmbedding
        except ImportError:
            self.enabled = False
            logger.warning(
                "cache semantica richiesta ma fastembed/numpy non sono installati: "
                "stadio disattivato. Installare con: pip install ecotokens[semantic]"
            )
            return
        try:
            self._embedder = TextEmbedding(model_name=self.config.model_name)
            self._numpy = numpy
        except Exception as error:
            self.enabled = False
            logger.warning("modello di embedding non caricabile (%s): stadio disattivato", error)

    # -- ciclo della richiesta --------------------------------------------

    async def before(self, ctx: RequestContext) -> None:
        if ctx.has_tools() or ctx.cache_key is None:
            return
        query = _last_user_text(ctx.params)
        if not query.strip():
            return

        vector = self._embed(query)
        if vector is None:
            return

        candidates = await ctx.store.semantic_candidates(
            ctx.model, _prefix_hash(ctx.params), self.config.max_candidates
        )
        if not candidates:
            return

        best_key, best_score = self._best_match(vector, candidates)
        if best_key is None or best_score < self.config.similarity_threshold:
            return

        entry = await ctx.store.get_cached(best_key)
        if entry is None:
            return

        avoided = baseline_cost_usd(entry.model, entry.usage)
        ctx.source = SOURCE_SEMANTIC_CACHE
        ctx.usage = Usage()
        ctx.cost_usd = 0.0
        ctx.saved_usd = avoided
        ctx.note(f"risposta servita dalla cache semantica (similarita' {best_score:.4f})")
        ctx.short_circuit = native_response_copy(entry.response, ecotokens_meta=ctx.meta())

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        if message is None or ctx.source != "api" or ctx.cache_key is None:
            return
        if ctx.has_tools() or ctx.upstream_response is None:
            return
        query = _last_user_text(ctx.params)
        vector = self._embed(query)
        if vector is None:
            return
        await ctx.store.add_semantic(
            cache_key=ctx.cache_key,
            model=ctx.model,
            prefix_hash=_prefix_hash(ctx.params),
            prompt=query[:2000],
            embedding=vector.astype("float32").tobytes(),
            ttl_seconds=self.config.ttl_seconds,
        )

    # -- utilita' ----------------------------------------------------------

    def _embed(self, text: str) -> Any:
        if self._embedder is None or self._numpy is None:
            return None
        # Stessa normalizzazione della cache esatta. Gli embedding sono
        # abbastanza robusti da assorbire uno spazio doppio, ma non del tutto:
        # normalizzare prima toglie una fonte di rumore che sposta il coseno
        # senza che nessuna parola sia cambiata.
        text = normalize(text)
        try:
            vectors = list(self._embedder.embed([text]))
        except Exception as error:
            logger.warning("embedding non riuscito: %s", error)
            return None
        if not vectors:
            return None
        vector = self._numpy.asarray(vectors[0], dtype="float32")
        norm = float(self._numpy.linalg.norm(vector))
        return vector / norm if norm else vector

    def _best_match(self, vector: Any, candidates: list[dict[str, Any]]) -> tuple[str | None, float]:
        numpy = self._numpy
        best_key: str | None = None
        best_score = -1.0
        for candidate in candidates:
            stored = numpy.frombuffer(candidate["embedding"], dtype="float32")
            if stored.shape != vector.shape:
                continue
            norm = float(numpy.linalg.norm(stored))
            if not norm:
                continue
            score = float(numpy.dot(vector, stored / norm))
            if score > best_score:
                best_score, best_key = score, candidate["cache_key"]
        return best_key, best_score


def _prefix_hash(params: dict[str, Any]) -> str:
    """Impronta di tutto cio' che precede l'ultima domanda.

    Confrontare due domande simili ha senso solo se arrivano dopo lo stesso
    contesto: la stessa frase dopo conversazioni diverse merita risposte diverse.
    """
    messages = params.get("messages") or []
    payload = {
        "system": params.get("system"),
        "messages": messages[:-1],
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]


def _last_user_text(params: dict[str, Any]) -> str:
    for message in reversed(params.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return ""
