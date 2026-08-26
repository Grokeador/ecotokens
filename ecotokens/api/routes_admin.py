"""Rotte di servizio: statistiche, sessioni, gestione delle cache."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


def _gateway(request: Request):
    return request.app.state.gateway


@router.get("/stats")
async def stats(gateway=Depends(_gateway)) -> dict[str, Any]:
    """Quanto e' stato speso, quanto risparmiato, e quanto lavora la cache."""
    data = await gateway.store.stats()
    today, this_month = await gateway.store.current_spend()
    data["spend"] = {"today_usd": today, "month_usd": this_month}

    baseline = float(data.get("baseline_cost_usd") or 0)
    cost = float(data.get("cost_usd") or 0)
    data["saved_ratio"] = (baseline - cost) / baseline if baseline else 0.0
    data["stages_enabled"] = {
        stage.name: bool(getattr(stage, "enabled", True))
        for stage in gateway.pipeline.stages
    }
    return data


@router.get("/sessions")
async def sessions(limit: int = 50, gateway=Depends(_gateway)) -> dict[str, Any]:
    return {"sessions": await gateway.store.list_sessions(limit)}


@router.post("/cache/prune")
async def prune_cache(gateway=Depends(_gateway)) -> dict[str, Any]:
    total = await gateway.store.prune_cache(gateway.settings.exact_cache.max_entries)
    return {"entries_before_prune": total}


@router.post("/cache/clear")
async def clear_cache(gateway=Depends(_gateway)) -> dict[str, Any]:
    await gateway.store.clear_caches()
    return {"status": "cache svuotate"}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(measure: bool = False, gateway=Depends(_gateway)) -> HTMLResponse:
    """Dashboard delle misure.

    Di default mostra solo cio' che e' gia' registrato: eseguire il banco a ogni
    apertura della pagina la renderebbe lenta e imprevedibile. Con
    ``?measure=true`` rifa' le misure sul momento.
    """
    from ..dashboard import build_dashboard_data, render_dashboard

    dati = await build_dashboard_data(gateway.settings, measure=measure)
    return HTMLResponse(content=render_dashboard(dati))
