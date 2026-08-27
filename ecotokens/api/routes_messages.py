"""Rotta nativa: ``POST /v1/messages``.

E' la stessa forma dell'endpoint Anthropic. Serve ai client che parlano gia'
il dialetto di Claude e che, passando dalla porta OpenAI, dovrebbero farsi
tradurre due volte per tornare al punto di partenza.

Qui il lavoro e' *meno* che sull'altra rotta, non di piu': la pipeline lavora
gia' in formato Anthropic, quindi per una richiesta nativa non c'e' nessuna
traduzione da fare - ne' all'andata ne' al ritorno. Il corpo entra come e'
arrivato, la risposta esce come l'ha prodotta l'API.

Il corpo non viene validato campo per campo di proposito. Riprodurre lo schema
Anthropic qui dentro significherebbe mantenerne una copia che invecchia, e
rifiutare per conto dell'API parametri che l'API magari accetta gia'. Si
controlla il minimo indispensabile per costruire il contesto; del resto
risponde l'API, con i suoi messaggi di errore che sono migliori dei nostri.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..pipeline.base import PipelineAbort, RequestContext
from ..pricing import Usage
from ..tokens import estimate_prompt_tokens, strip_cache_control
from ..translate.from_anthropic import to_plain_dict as _as_dict
from .errors import error_response
from .schemas import error_payload

logger = logging.getLogger("ecotokens.messages")
router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _gateway(request: Request):
    return request.app.state.gateway


@router.post("/messages")
async def messages(request: Request, gateway=Depends(_gateway)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=error_payload("Corpo della richiesta non e' JSON valido.", "invalid_request_error"),
        )
    if not isinstance(body, dict) or not body.get("messages"):
        return JSONResponse(
            status_code=400,
            content=error_payload(
                "La richiesta deve contenere il campo 'messages'.", "invalid_request_error"
            ),
        )

    ctx = gateway.make_native_context(
        body, {k.lower(): v for k, v in request.headers.items()}
    )

    try:
        await gateway.pipeline.before(ctx)
    except PipelineAbort as abort:
        return JSONResponse(
            status_code=abort.status_code,
            content=error_payload(abort.message, abort.error_type),
        )
    except Exception as error:
        return error_response(error)

    if ctx.short_circuit is not None:
        ctx.client_response = ctx.short_circuit
        ctx.upstream_response = ctx.short_circuit
        await gateway.pipeline.after(ctx, None)
        ctx.short_circuit["ecotokens"] = ctx.meta()
        if ctx.stream:
            return StreamingResponse(
                _replay_as_stream(ctx.short_circuit),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        return JSONResponse(content=ctx.short_circuit)

    if ctx.stream:
        return StreamingResponse(
            _stream_upstream(gateway, ctx),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    return await _call_upstream(gateway, ctx)


# Parametri che ``count_tokens`` accetta. Non ne accetta altri: `max_tokens`
# per esempio non ha senso per un conteggio dell'input, e passarlo e' un 400.
COUNT_TOKENS_PARAMS = (
    "messages",
    "model",
    "system",
    "thinking",
    "tool_choice",
    "tools",
    "output_config",
)


@router.post("/messages/count_tokens")
async def count_tokens(request: Request, gateway=Depends(_gateway)):
    """Conta i token di input di una richiesta, senza generarne la risposta.

    Un client nativo che vuole preventivare una spesa chiama questo endpoint.
    Senza, prende un 404 e il gateway risulta incompleto proprio per i client
    che l'hanno cercato.

    **Cosa viene contato.** La richiesta cosi' come e' arrivata, dopo la sola
    sanificazione: alias del modello risolto, parametri di campionamento tolti.
    Non dopo gli stadi che riscrivono il prompt - memoria, compattazione,
    riscrittura - perche' quelli hanno effetti collaterali che un conteggio non
    deve produrre: creerebbero sessioni, scriverebbero riassunti, chiamerebbero
    modelli. Il numero e' quindi il costo del prompt *che hai scritto*: quello
    che il gateway manda davvero e' di solito piu' corto, ed e' il punto.

    La risposta porta anche la stima locale del gateway accanto al conteggio
    vero. Non serve al chiamante: serve a noi. Il progetto si regge su uno
    stimatore euristico mai tarato contro il tokenizer reale, e ogni chiamata a
    questo endpoint e' un punto di taratura gratuito.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=error_payload(
                "Corpo della richiesta non e' JSON valido.", "invalid_request_error"
            ),
        )
    if not isinstance(body, dict) or not body.get("messages"):
        return JSONResponse(
            status_code=400,
            content=error_payload(
                "La richiesta deve contenere il campo 'messages'.", "invalid_request_error"
            ),
        )

    ctx = gateway.make_native_context(
        body, {k.lower(): v for k, v in request.headers.items()}
    )
    # I marker di cache non vanno contati: sono una direttiva, non contenuto.
    params = {
        nome: strip_cache_control(ctx.params[nome])
        for nome in COUNT_TOKENS_PARAMS
        if ctx.params.get(nome) is not None
    }

    try:
        risposta = await gateway.client.messages.count_tokens(**params)
    except Exception as error:
        return error_response(error)

    payload = _as_dict(risposta)
    esatto = payload.get("input_tokens")
    stimato = estimate_prompt_tokens(ctx.params)
    payload["ecotokens"] = {
        "model": ctx.model,
        "estimated_input_tokens": stimato,
        "estimate_error_ratio": round((stimato - esatto) / esatto, 4)
        if isinstance(esatto, int) and esatto
        else None,
        "notes": list(ctx.notes),
    }
    if isinstance(esatto, int) and esatto:
        logger.info(
            "count_tokens | %s | esatto=%d stimato=%d scarto=%+.1f%%",
            ctx.model,
            esatto,
            stimato,
            (stimato - esatto) / esatto * 100,
        )
    return JSONResponse(content=payload)

async def _call_upstream(gateway, ctx: RequestContext) -> JSONResponse:
    resource, params = gateway.messages_resource(ctx)
    params.pop("stream", None)
    try:
        message = await resource.create(**params)
    except Exception as error:
        return error_response(error)

    ctx.usage = Usage.from_api(getattr(message, "usage", None))
    payload = _as_dict(message)
    ctx.client_response = payload
    ctx.upstream_response = payload
    await gateway.pipeline.after(ctx, message)
    payload["ecotokens"] = ctx.meta()
    return JSONResponse(content=payload)


async def _stream_upstream(gateway, ctx: RequestContext) -> AsyncIterator[str]:
    """Ritrasmette gli eventi dell'API cosi' come arrivano.

    Nessuna traduzione: il client nativo si aspetta esattamente questi eventi.
    L'unica cosa che facciamo e' guardarli passare per estrarre i consumi, che
    servono alla contabilita' e che arrivano divisi fra il primo evento e
    l'ultimo.
    """
    resource, params = gateway.messages_resource(ctx)
    params.pop("stream", None)
    finale: Any = None
    try:
        async with resource.stream(**params) as flusso:
            async for evento in flusso:
                yield _sse(evento)
            finale = await flusso.get_final_message()
    except Exception as error:
        logger.warning("errore durante lo streaming nativo: %s", error)
        yield _sse_errore(error)
        return

    ctx.usage = Usage.from_api(getattr(finale, "usage", None))
    payload = _as_dict(finale) if finale is not None else None
    ctx.client_response = payload
    ctx.upstream_response = payload
    await gateway.pipeline.after(ctx, finale)


async def _replay_as_stream(payload: dict[str, Any]) -> AsyncIterator[str]:
    """Chi chiede uno stream deve riceverne uno anche su un hit di cache.

    La risposta e' gia' completa, quindi si ricostruisce la sequenza minima di
    eventi che un client nativo si aspetta invece di inventarne una piu' ricca:
    meno cose da sbagliare.
    """
    testa = {k: v for k, v in payload.items() if k not in ("content", "ecotokens")}
    testa["content"] = []
    yield _evento("message_start", {"type": "message_start", "message": testa})

    for indice, blocco in enumerate(payload.get("content") or []):
        yield _evento(
            "content_block_start",
            {"type": "content_block_start", "index": indice, "content_block": blocco},
        )
        yield _evento(
            "content_block_stop", {"type": "content_block_stop", "index": indice}
        )

    yield _evento(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": payload.get("stop_reason"),
                "stop_sequence": payload.get("stop_sequence"),
            },
            "usage": payload.get("usage") or {},
        },
    )
    yield _evento("message_stop", {"type": "message_stop"})


def _sse(evento: Any) -> str:
    tipo = getattr(evento, "type", "message_delta")
    return _evento(tipo, _as_dict(evento))


def _sse_errore(error: Exception) -> str:
    return _evento(
        "error",
        {"type": "error", "error": {"type": "api_error", "message": str(error)}},
    )


def _evento(tipo: str, dati: dict[str, Any]) -> str:
    return f"event: {tipo}\ndata: {json.dumps(dati, ensure_ascii=False, default=str)}\n\n"
