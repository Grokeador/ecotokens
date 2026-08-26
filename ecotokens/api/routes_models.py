"""``GET /v1/models``: il catalogo che i client usano per popolare le tendine."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..pricing import MODELS
from .schemas import error_payload

router = APIRouter()


def _gateway(request: Request):
    return request.app.state.gateway


def _model_entry(model_id: str) -> dict[str, Any]:
    info = MODELS[model_id]
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "anthropic",
        # Campi fuori standard, utili a chi guarda: i client li ignorano.
        "context_window": info.context_window,
        "max_output_tokens": info.max_output,
        "pricing_usd_per_mtok": {
            "input": info.input_per_mtok,
            "output": info.output_per_mtok,
        },
        "cache_min_prompt_tokens": info.cache_min_tokens,
    }


@router.get("/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [_model_entry(model_id) for model_id in MODELS],
        "created": int(time.time()),
    }


@router.get("/models/{model_id:path}")
async def retrieve_model(model_id: str, gateway=Depends(_gateway)):
    if model_id not in MODELS:
        return JSONResponse(
            status_code=404,
            content=error_payload(f"Modello sconosciuto: {model_id}", "not_found_error"),
        )
    return JSONResponse(content=_model_entry(model_id))
