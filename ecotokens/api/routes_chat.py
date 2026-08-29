"""Rotta principale: ``POST /v1/chat/completions``."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..pipeline.base import PipelineAbort, RequestContext
from ..pricing import Usage
from ..translate.from_anthropic import (
    openai_response_from_dict,
    to_openai_response,
    to_plain_dict,
)
from ..translate.stream import DONE, StreamTranslator, replay_response_as_stream, sse
from .errors import error_response
from .schemas import ChatCompletionRequest, error_payload

logger = logging.getLogger("ecotokens.chat")
router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _gateway(request: Request):
    return request.app.state.gateway


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    gateway=Depends(_gateway),
):
    ctx = gateway.make_context(body, {k.lower(): v for k, v in request.headers.items()})

    try:
        await gateway.pipeline.before(ctx)
    except PipelineAbort as abort:
        return JSONResponse(
            status_code=abort.status_code,
            content=error_payload(abort.message, abort.error_type),
        )
    except Exception as error:
        return error_response(error)

    # Hit di cache: nessuna chiamata all'API, ma la contabilita' va comunque
    # aggiornata, altrimenti il risparmio non comparirebbe da nessuna parte.
    if ctx.short_circuit is not None:
        # In cache c'e' il formato canonico: qui va tradotto, perche' chi ha
        # chiesto parla il dialetto OpenAI.
        ctx.upstream_response = ctx.short_circuit
        risposta = openai_response_from_dict(
            ctx.short_circuit, model=ctx.model, usage=ctx.usage
        )
        ctx.client_response = risposta
        await gateway.pipeline.after(ctx, None)
        risposta["ecotokens"] = ctx.meta()
        if body.stream:
            return StreamingResponse(
                replay_response_as_stream(
                    risposta, include_usage=body.wants_usage_in_stream()
                ),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        return JSONResponse(content=risposta)

    if body.stream:
        return StreamingResponse(
            _stream_upstream(gateway, ctx, body),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return await _call_upstream(gateway, ctx)


async def _call_upstream(gateway, ctx: RequestContext) -> JSONResponse:
    """Chiama l'API riusando il contesto gia' passato per la pipeline."""
    resource, params = gateway.messages_resource(ctx)
    try:
        message = await resource.create(**params)
    except Exception as error:
        return error_response(error)

    ctx.usage = Usage.from_api(getattr(message, "usage", None))
    response = to_openai_response(message, model=ctx.model, usage=ctx.usage)
    ctx.client_response = response
    ctx.upstream_response = to_plain_dict(message)

    await gateway.pipeline.after(ctx, message)
    # Il blocco diagnostico si allega alla fine: solo dopo la contabilita'
    # i valori di costo e risparmio sono definitivi.
    response["ecotokens"] = ctx.meta()
    return JSONResponse(content=response)


async def _stream_upstream(
    gateway, ctx: RequestContext, body: ChatCompletionRequest
) -> AsyncIterator[str]:
    """Streaming vero verso l'API, tradotto in chunk OpenAI.

    Gli errori vanno gestiti dentro il generatore: una volta iniziata la
    risposta HTTP non e' piu' possibile cambiare lo stato, quindi un problema
    a meta' stream viene comunicato come chunk di errore seguito da [DONE],
    che i client sanno interpretare.
    """
    resource, params = gateway.messages_resource(ctx)
    translator = StreamTranslator(
        completion_id=ctx.completion_id,
        model=ctx.model,
        include_usage=body.wants_usage_in_stream(),
    )

    final_message: Any = None
    try:
        async with resource.stream(**params) as stream:
            yield translator.open_chunk()
            async for event in stream:
                for chunk in translator.handle(event):
                    yield chunk
            final_message = await stream.get_final_message()
    except Exception as error:
        logger.warning("stream interrotto: %s", error)
        yield translator.chunk_interrotto(str(error))
        # **Il prompt e' gia' stato pagato.** Anthropic lo ha letto per
        # intero - input, letture e scritture di cache - e ha generato i
        # token consegnati fin qui. Uscire senza passare dalla contabilita'
        # rendeva quella spesa invisibile: `stats` la sottostimava e il tetto
        # di spesa non la contava, quindi si poteva sforare un budget a furia
        # di stream che cadono senza che nessun contatore se ne accorgesse.
        await _conta_interrotto(gateway, ctx, translator)
        if body.wants_usage_in_stream():
            yield translator.usage_chunk(ctx.meta())
        yield DONE
        return

    if translator.interrotto:
        # Lo stream si e' chiuso **senza errori** ma anche senza dire di
        # essere finito: e' cosi' che si presenta un proxy che taglia la
        # connessione. Prima di questa riga il gateway consegnava la risposta
        # tagliata con `finish_reason: "stop"`, cioe' certificava come
        # completa una risposta a meta'.
        logger.warning("stream chiuso senza stop_reason: risposta incompleta")
        ctx.risposta_incompleta = True
        ctx.note("risposta incompleta: lo stream si e' chiuso a meta'")
        yield translator.chunk_interrotto(
            "lo stream si e' chiuso prima della fine della risposta"
        )
    else:
        yield translator.final_chunk()

    ctx.usage = translator.usage
    response = to_openai_response(final_message, model=ctx.model, usage=ctx.usage)
    ctx.client_response = response
    ctx.upstream_response = to_plain_dict(final_message)
    await gateway.pipeline.after(ctx, final_message)

    if body.wants_usage_in_stream():
        yield translator.usage_chunk(ctx.meta())
    yield DONE


async def _conta_interrotto(gateway, ctx: RequestContext, translator) -> None:
    """Contabilita' di uno stream morto per strada.

    Non c'e' nessun messaggio finale da passare agli stadi, ma i consumi si
    conoscono lo stesso: il traduttore li ha raccolti da `message_start` e
    dai `message_delta` arrivati prima della caduta.
    """
    ctx.usage = translator.usage
    ctx.risposta_incompleta = True
    if not ctx.usage.total_prompt_tokens:
        # Caduta prima ancora di sapere quanto e' costata: non c'e' niente da
        # registrare, e inventare uno zero sarebbe una riga falsa in piu'.
        return
    await gateway.pipeline.after(ctx, None)
