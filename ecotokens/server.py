"""Applicazione FastAPI e composizione della pipeline."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.schemas import ChatCompletionRequest, error_payload
from .config import Settings, load_settings
from .pipeline.base import Pipeline, RequestContext
from .pipeline.budget import BudgetStage
from .pipeline.cache_planner import CachePlannerStage
from .pipeline.context import ContextStage
from .pipeline.exact_cache import ExactCacheStage
from .pipeline.ledger import LedgerStage
from .pipeline.memory import MemoryStage
from .pipeline.router import RouterStage
from .pipeline.semantic_cache import SemanticCacheStage
from .pipeline.session import SessionStage
from .pricing import Usage
from .store.db import Database
from .store.repos import Store
from .tokens import TokenCounter
from .translate.from_anthropic import new_completion_id, to_openai_response
from .translate.to_anthropic import build_anthropic_params

logger = logging.getLogger("ecotokens")


class Gateway:
    """Stato condiviso del gateway: client, storage, pipeline."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.storage.path)
        self.store = Store(self.database)
        self.client = self._build_client(settings)
        self.counter = TokenCounter(self.client)
        self.pipeline = self._build_pipeline(settings)

    @staticmethod
    def _build_client(settings: Settings) -> anthropic.AsyncAnthropic:
        kwargs: dict[str, Any] = {
            "timeout": settings.upstream.timeout_seconds,
            "max_retries": settings.upstream.max_retries,
        }
        # Se la chiave non e' in configurazione, l'SDK la risolve da solo:
        # ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN o un profilo `ant auth login`.
        if settings.upstream.api_key:
            kwargs["api_key"] = settings.upstream.api_key
        if settings.upstream.base_url:
            kwargs["base_url"] = settings.upstream.base_url
        return anthropic.AsyncAnthropic(**kwargs)

    @staticmethod
    def _build_pipeline(settings: Settings) -> Pipeline:
        """L'ordine e' parte del progetto, non una preferenza.

        La sessione viene per prima perche' quasi ogni stadio successivo ha
        bisogno di sapere in che conversazione si trova. Poi le cache, prima
        degli stadi che riscrivono il prompt, cosi' un hit non paga lavoro
        inutile. Il budget sta subito dopo le cache e non prima: una risposta
        servita dalla cache non spende nulla e non ha senso rifiutarla per
        superamento del tetto, mentre qualsiasi chiamata vera all'API e' ancora
        a valle. Il cache planner per ultimo, quando il prompt e' ormai
        definitivo: piazzare i breakpoint prima significherebbe marcarli su un
        testo che poi cambia.
        """
        return Pipeline(
            [
                SessionStage(
                    settings.session.enabled,
                    settings.session.fingerprint_depth,
                    settings.session.ttl_hours,
                ),
                ExactCacheStage(settings),
                SemanticCacheStage(settings),
                BudgetStage(settings),
                MemoryStage(settings),
                ContextStage(settings),
                RouterStage(settings),
                CachePlannerStage(settings),
                LedgerStage(),
            ]
        )

    def make_context(
        self, request: ChatCompletionRequest, headers: dict[str, str]
    ) -> RequestContext:
        translation = build_anthropic_params(request, self.settings)
        return RequestContext(
            request=request,
            settings=self.settings,
            store=self.store,
            client=self.client,
            counter=self.counter,
            completion_id=new_completion_id(),
            model=translation.model,
            params=translation.params,
            stream=request.stream,
            headers=headers,
            notes=list(translation.notes),
            dropped=list(translation.dropped),
        )

    def messages_resource(self, ctx: RequestContext) -> tuple[Any, dict[str, Any]]:
        """Sceglie l'endpoint giusto e completa i parametri.

        Gli stadi che usano funzionalita' beta (potatura del contesto) lo
        dichiarano con ``ctx.use_beta``: solo in quel caso si passa da
        ``client.beta.messages``, che e' l'unico endpoint ad accettare
        ``context_management`` e i flag beta.
        """
        params = dict(ctx.params)
        if ctx.betas:
            params["betas"] = list(ctx.betas)
            return self.client.beta.messages, params
        params.pop("context_management", None)
        return self.client.messages, params

    async def complete(
        self, request: ChatCompletionRequest, headers: dict[str, str] | None = None
    ) -> tuple[dict[str, Any], RequestContext]:
        """Esegue una richiesta non in streaming e restituisce (risposta, contesto).

        E' il percorso unico usato sia dalla rotta HTTP sia dal banco di misura:
        misurare una strada diversa da quella che percorrono le richieste vere
        darebbe numeri che non valgono per nessuno.
        """
        ctx = self.make_context(request, headers or {})
        await self.pipeline.before(ctx)

        if ctx.short_circuit is not None:
            ctx.openai_response = ctx.short_circuit
            await self.pipeline.after(ctx, None)
            ctx.short_circuit["ecotokens"] = ctx.meta()
            return ctx.short_circuit, ctx

        resource, params = self.messages_resource(ctx)
        message = await resource.create(**params)

        ctx.usage = Usage.from_api(getattr(message, "usage", None))
        response = to_openai_response(message, model=ctx.model, usage=ctx.usage)
        ctx.openai_response = response

        await self.pipeline.after(ctx, message)
        # Il blocco diagnostico si allega alla fine: solo dopo la contabilita'
        # i valori di costo e risparmio sono definitivi.
        response["ecotokens"] = ctx.meta()
        return response, ctx

    async def startup(self) -> None:
        self.database.connect()
        logger.info(
            "EcoTokens avviato | modello di default %s | database %s | FTS5 %s",
            self.settings.upstream.default_model,
            self.settings.storage.path,
            "disponibile" if self.database.has_fts else "assente (ricerca lessicale)",
        )

    async def shutdown(self) -> None:
        await self.store.prune_cache(self.settings.exact_cache.max_entries)
        self.database.close()
        await self.client.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    gateway = Gateway(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await gateway.startup()
        try:
            yield
        finally:
            await gateway.shutdown()

    app = FastAPI(
        title="EcoTokens",
        description="Gateway locale OpenAI-compatibile per Claude, con economia di token",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.gateway = gateway

    @app.middleware("http")
    async def check_api_key(request: Request, call_next):
        """Chiave del gateway, se configurata. Non e' la chiave Anthropic."""
        expected = settings.server.api_key
        if expected and request.url.path.startswith(("/v1", "/admin")):
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else header
            if token != expected:
                return JSONResponse(
                    status_code=401,
                    content=error_payload(
                        "Chiave del gateway mancante o errata.", "authentication_error"
                    ),
                )
        return await call_next(request)

    from .api.routes_admin import router as admin_router
    from .api.routes_chat import router as chat_router
    from .api.routes_models import router as models_router

    app.include_router(chat_router, prefix="/v1")
    app.include_router(models_router, prefix="/v1")
    app.include_router(admin_router, prefix="/admin")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": "0.1.0"}

    return app


def get_gateway(request: Request) -> Gateway:
    return request.app.state.gateway
