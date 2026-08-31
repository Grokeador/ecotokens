"""Applicazione FastAPI e composizione della pipeline."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import anthropic
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .api.schemas import ChatCompletionRequest, error_payload
from .config import Settings, intestazioni_upstream, load_settings
from .pipeline.base import FORMAT_ANTHROPIC, Pipeline, RequestContext
from .pipeline.budget import BudgetStage
from .pipeline.cache_planner import CachePlannerStage
from .pipeline.context import ContextStage
from .pipeline.exact_cache import ExactCacheStage
from .pipeline.ledger import LedgerStage
from .pipeline.memory import MemoryStage
from .pipeline.prompt import PromptOptimizerStage
from .pipeline.router import RouterStage
from .pipeline.semantic_cache import SemanticCacheStage
from .pipeline.session import SessionStage
from .pricing import Usage, model_info, resolve_model
from .store.db import Database
from .store.repos import Store
from .tokens import TokenCounter
from .translate.from_anthropic import new_completion_id, to_openai_response, to_plain_dict
from .translate.to_anthropic import UNSUPPORTED_SAMPLING, build_anthropic_params

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
        intestazioni = intestazioni_upstream(settings.upstream)
        if intestazioni:
            kwargs["default_headers"] = intestazioni
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
                # Prima della memoria e della compattazione: quei due stadi
                # devono lavorare sul testo gia' riscritto, altrimenti il
                # riassunto verrebbe calcolato su un originale che poi non
                # viene piu' inviato.
                PromptOptimizerStage(settings),
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
            nome_richiesto_grezzo=request.model or "",
            params=translation.params,
            stream=request.stream,
            client_effort=request.reasoning_effort,
            headers=headers,
            notes=list(translation.notes),
            dropped=list(translation.dropped),
        )

    def make_native_context(
        self, body: dict[str, Any], headers: dict[str, str]
    ) -> RequestContext:
        """Contesto per una richiesta gia' in formato Anthropic.

        Non c'e' nessuna traduzione da fare: il corpo *e'* gia' cio' che la
        pipeline manipola. Restano tre cose da sistemare, e sono le stesse che
        la traduzione fa per le richieste OpenAI:

        * risolvere l'alias del modello (`claude-opus-5` e simili);
        * togliere i parametri di campionamento che i modelli attuali
          rifiutano con un 400, se un client li manda per abitudine;
        * applicare i valori predefiniti del gateway dove il client tace.
        """
        params = dict(body)
        params.pop("stream", None)

        scartati = [nome for nome in UNSUPPORTED_SAMPLING if nome in params]
        for nome in scartati:
            params.pop(nome)

        # `system` in forma di blocchi, anche quando arriva come stringa.
        #
        # L'API accetta le due forme come equivalenti, e la stringa e' quella
        # che scrive quasi chiunque. Il pianificatore pero' puo' attaccare un
        # `cache_control` **solo a un blocco** (`cache_planner.py`, la riga con
        # `isinstance(system, list)`): con una stringa non marcava niente e
        # taceva. Un guasto silenzioso della famiglia peggiore - il gateway
        # sembrava funzionare, il prefisso non andava mai in cache, e nessun
        # errore lo diceva.
        #
        # La conversione non cambia cosa legge il modello. Cambia solo che il
        # prefisso diventa marcabile, e che le due porte mandano la stessa
        # forma: due forme diverse sono due voci di cache invece di una.
        sistema = params.get("system")
        if isinstance(sistema, str) and sistema:
            params["system"] = [{"type": "text", "text": sistema}]

        model = resolve_model(params.get("model") or self.settings.upstream.default_model)
        params["model"] = model
        info = model_info(model)

        stream = bool(body.get("stream"))
        if not params.get("max_tokens"):
            params["max_tokens"] = min(
                self.settings.upstream.default_max_tokens_stream
                if stream
                else self.settings.upstream.default_max_tokens,
                info.max_output,
            )
        if self.settings.upstream.adaptive_thinking and "thinking" not in params:
            params["thinking"] = {"type": "adaptive"}

        # Gli stessi valori predefiniti dell'altra porta. Senza questo, la
        # stessa domanda posta nei due dialetti produrrebbe parametri diversi,
        # e quindi due voci di cache invece di una.
        effort = (params.get("output_config") or {}).get("effort")
        output_config = dict(params.get("output_config") or {})
        output_config.setdefault("effort", self.settings.upstream.default_effort)
        params["output_config"] = output_config

        note = ["richiesta nativa: nessuna traduzione applicata"]
        if scartati:
            note.append(
                "parametri di campionamento rimossi (i modelli attuali li rifiutano): "
                + ", ".join(scartati)
            )

        return RequestContext(
            request=None,
            settings=self.settings,
            store=self.store,
            client=self.client,
            counter=self.counter,
            completion_id=new_completion_id(),
            model=model,
            nome_richiesto_grezzo=str(body.get("model") or ""),
            params=params,
            stream=stream,
            client_format=FORMAT_ANTHROPIC,
            client_effort=effort,
            headers=headers,
            notes=note,
            dropped=scartati,
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
            ctx.client_response = ctx.short_circuit
            ctx.upstream_response = ctx.short_circuit
            await self.pipeline.after(ctx, None)
            ctx.short_circuit["ecotokens"] = ctx.meta()
            return ctx.short_circuit, ctx

        resource, params = self.messages_resource(ctx)
        message = await resource.create(**params)

        ctx.usage = Usage.from_api(getattr(message, "usage", None))
        response = to_openai_response(message, model=ctx.model, usage=ctx.usage)
        ctx.client_response = response
        ctx.upstream_response = to_plain_dict(message)

        await self.pipeline.after(ctx, message)
        # Il blocco diagnostico si allega alla fine: solo dopo la contabilita'
        # i valori di costo e risparmio sono definitivi.
        response["ecotokens"] = ctx.meta()
        return response, ctx

    def riconfigura(self, settings: Settings) -> None:
        """Sostituisce impostazioni e pipeline senza riavviare il processo.

        Il client e il database restano quelli: le credenziali e il percorso dei
        dati non passano dal pannello, e ricostruirli mentre delle richieste
        sono in volo significherebbe chiudere una connessione sotto i piedi di
        chi la sta usando.

        La pipeline nuova prende il posto della vecchia in un'assegnazione
        sola. Una richiesta gia' partita continua con la lista che aveva
        raccolto - `Pipeline.before` la scorre - quindi non esiste il caso di
        una richiesta servita per meta' con la vecchia configurazione e per
        meta' con la nuova.
        """
        self.settings = settings
        self.pipeline = self._build_pipeline(settings)

    async def startup(self) -> None:
        self.database.connect()
        logger.info(
            "EcoTokens avviato | modello di default %s | database %s | FTS5 %s",
            self.settings.upstream.default_model,
            self.settings.storage.path,
            "disponibile" if self.database.has_fts else "assente (ricerca lessicale)",
        )

    async def shutdown(self) -> None:
        """Chiude tutto, anche se qualcosa lungo la strada si rompe.

        I tre passi sono indipendenti e vanno tentati tutti. Nella versione
        precedente erano in fila senza protezione: una potatura della cache
        fallita - un database in sola lettura, un disco pieno - lasciava aperti
        sia la connessione al database sia il client HTTP, cioe' proprio le due
        cose che la chiusura esiste per chiudere. Un guasto durante la pulizia
        diventava una perdita di risorse.
        """
        for nome, passo in (
            ("potatura della cache", self._pota_cache()),
            ("chiusura del database", self._chiudi_database()),
            ("chiusura del client", self.client.close()),
        ):
            try:
                await passo
            except Exception as errore:  # la chiusura non ha nessuno a cui riferire
                logger.warning("errore durante la %s: %s", nome, errore)

    async def _pota_cache(self) -> None:
        await self.store.prune_cache(self.settings.exact_cache.max_entries)

    async def _chiudi_database(self) -> None:
        self.database.close()


def _dove_scrivere() -> Path:
    """Il file che il pannello riscrive.

    Lo stesso che `load_settings` legge all'avvio, cosi' cio' che si salva e'
    cio' che si ritrova. Se non esiste ancora, viene creato nella cartella di
    lavoro - dove il gateway cerchera' al riavvio.
    """
    from .config import DEFAULT_CONFIG_NAMES, _find_config

    trovato = _find_config(None)
    return Path(trovato) if trovato else Path(DEFAULT_CONFIG_NAMES[0])


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
        description=(
            "Gateway locale verso l'API Anthropic, con due porte in ingresso: "
            "dialetto OpenAI e dialetto nativo. Riduce la spesa in token."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.gateway = gateway

    @app.exception_handler(RequestValidationError)
    async def richiesta_malformata(request: Request, errore: RequestValidationError):
        """Errori di validazione nella busta che un client OpenAI sa aprire.

        FastAPI risponde `422` con un campo `detail`, che e' un ottimo formato
        - per FastAPI. Un client OpenAI cerca `error.message`, non lo trova, e
        fallisce **nel proprio parser**: a quel punto l'utente vede un errore
        della sua libreria al posto del nostro, e la causa vera sparisce.
        Vale la stessa regola gia' applicata agli errori dell'API a monte; qui
        mancava per gli errori generati dal gateway stesso.

        E `400`, non `422`: e' il codice che l'API OpenAI usa per una
        richiesta malformata, ed e' quello su cui i client decidono di non
        riprovare.
        """
        primo = (errore.errors() or [{}])[0]
        punto = ".".join(str(p) for p in primo.get("loc", ()) if p != "body")
        messaggio = primo.get("msg", "richiesta non valida")
        return JSONResponse(
            status_code=400,
            content=error_payload(
                f"{punto}: {messaggio}" if punto else messaggio,
                "invalid_request_error",
            ),
        )

    @app.middleware("http")
    async def check_api_key(request: Request, call_next):
        """Chiave del gateway, se configurata. Non e' la chiave Anthropic.

        Copre anche le pagine, non solo le API. Quando sono state aggiunte
        l'elenco diceva `/v1` e `/admin`, quindi console e quadro restavano
        aperte a chiave impostata: mostrano modelli, costi e frammenti dei
        prompt, cioe' esattamente il traffico che la chiave doveva proteggere.
        `/health` resta fuori apposta, perche' serve a sapere se il processo e'
        vivo e non dice niente di nessuno.
        """
        expected = settings.server.api_key or next(
            iter(settings.server.chiavi.values()), None
        )
        protetto = request.url.path in {"/", "/ui", "/quadro", "/impostazioni"} or request.url.path.startswith(
            ("/v1", "/admin")
        )
        if expected and protetto:
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else header
            valide = {expected, *settings.server.chiavi.values()}
            if token not in valide:
                return JSONResponse(
                    status_code=401,
                    content=error_payload(
                        "Chiave del gateway mancante o errata.", "authentication_error"
                    ),
                )
        return await call_next(request)

    from .api.routes_admin import router as admin_router
    from .api.routes_chat import router as chat_router
    from .api.routes_messages import router as messages_router
    from .api.routes_models import router as models_router

    app.include_router(chat_router, prefix="/v1")
    # Porta nativa: stessa pipeline, nessuna traduzione.
    app.include_router(messages_router, prefix="/v1")
    app.include_router(models_router, prefix="/v1")
    app.include_router(admin_router, prefix="/admin")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    # La console sta sulla radice, non sotto /admin: e' la pagina che si apre
    # dopo `ecotokens serve`, e un indirizzo che si ricorda vale quanto una
    # funzione in piu'.
    @app.get("/", response_class=HTMLResponse)
    @app.get("/ui", response_class=HTMLResponse)
    async def console() -> HTMLResponse:
        from .console import render_console

        return HTMLResponse(content=render_console())

    @app.get("/impostazioni", response_class=HTMLResponse)
    async def impostazioni(request: Request) -> HTMLResponse:
        from .pannello import render_pannello

        gateway = request.app.state.gateway
        return HTMLResponse(
            content=render_pannello(gateway.settings, percorso_config=_dove_scrivere())
        )

    @app.post("/impostazioni", response_class=HTMLResponse)
    async def salva_impostazioni(request: Request) -> HTMLResponse:
        """Valida, applica alla pipeline viva, poi scrive il file.

        In quest'ordine di proposito: se la validazione fallisce non si e'
        toccato niente, e se la scrittura fallisce - disco pieno, permessi - il
        gateway sta comunque girando con cio' che l'utente ha chiesto, e il
        messaggio lo dice. L'ordine opposto lascerebbe un file che promette una
        configurazione che il processo non ha.
        """
        from .pannello import ModificaRifiutata, prepara, render_pannello, scrivi_configurazione

        gateway = request.app.state.gateway
        # `request.form()` di Starlette pretende `python-multipart`, che
        # servirebbe per gli allegati. Questo modulo non ne ha e viaggia
        # urlencoded: la libreria standard basta, e una dipendenza in piu' per
        # una pagina sola non si giustifica.
        corpo = (await request.body()).decode("utf-8", errors="replace")
        campi = parse_qs(corpo, keep_blank_values=True)
        # Le caselle mandano due valori - il nascosto "false" e lo spuntato
        # "true" - e l'ultimo vince: e' quello che rappresenta lo stato voluto.
        # Senza il nascosto, spegnere qualcosa sarebbe indistinguibile dal non
        # averlo toccato, perche' una casella non spuntata non viene inviata.
        modifiche = {chiave: valori[-1] for chiave, valori in campi.items()}

        try:
            nuove, cambiati = prepara(gateway.settings, modifiche)
        except (ModificaRifiutata, ValueError) as errore:
            return HTMLResponse(
                content=render_pannello(
                    gateway.settings,
                    percorso_config=_dove_scrivere(),
                    esito={"errore": f"Niente e' stato cambiato. {errore}"},
                ),
                status_code=400,
            )

        gateway.riconfigura(nuove)
        esito: dict[str, Any] = {"cambiati": cambiati, "file": str(_dove_scrivere())}
        if cambiati:
            try:
                scrivi_configurazione(nuove, _dove_scrivere())
            except OSError as errore:
                esito = {
                    "errore": (
                        f"Applicato al gateway, ma non scritto su file ({errore}): "
                        "al prossimo riavvio tornera' come prima."
                    )
                }
        return HTMLResponse(
            content=render_pannello(nuove, percorso_config=_dove_scrivere(), esito=esito)
        )

    @app.get("/quadro", response_class=HTMLResponse)
    async def quadro(request: Request) -> HTMLResponse:
        """Cruscotto compatto: tutti i parametri, nessuna misura eseguita.

        Legge soltanto cio' che e' gia' registrato, quindi si apre subito. Una
        pagina di controllo che si fa aspettare non viene guardata.
        """
        from .quadro import build_quadro_data, render_quadro

        gateway = request.app.state.gateway
        dati = await build_quadro_data(gateway.settings, gateway.store)
        return HTMLResponse(content=render_quadro(dati))

    return app


def get_gateway(request: Request) -> Gateway:
    return request.app.state.gateway
