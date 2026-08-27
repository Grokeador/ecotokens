"""Test della potatura del contesto.

Lo stadio e' rimasto a lungo a zero nell'ablazione, e la ragione non era che
potare fosse inutile: era che potare come lo faceva prima *costava*. Con il
confine mobile - il valore predefinito del server - l'insieme dei blocchi
svuotati cambia a ogni turno, quindi il prefisso cambia a ogni turno e la cache
non trova mai niente.

La proprieta' verificata qui e' quella: mentre la conversazione cresce, i
blocchi svuotati devono restare gli stessi.
"""

from __future__ import annotations

import json

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.context import ContextStage
from ecotokens.simulator import _apply_context_edits
from ecotokens.translate.to_anthropic import build_anthropic_params

RIMOSSO = "rimosso dal contesto"


@pytest.fixture
def settings() -> Settings:
    config = Settings()
    # La guardia contro l'overflow non c'entra: qui si prova la convenienza.
    config.context.trigger_ratio = 0.99
    config.context.local_compaction = False
    config.context.prune_min_prunable_tokens = 1_000
    return config


def conversazione(turni: int, per_turno: int = 1, parole: int = 200):
    """Cronologia agentica: ogni turno chiama dei tool e ne riceve i risultati."""
    messaggi = [{"role": "user", "content": "Analizza il progetto."}]
    for turno in range(turni):
        chiamate = [
            {
                "id": f"c{turno}_{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
            for i in range(per_turno)
        ]
        messaggi.append({"role": "assistant", "tool_calls": chiamate})
        for i in range(per_turno):
            messaggi.append(
                {
                    "role": "tool",
                    "tool_call_id": f"c{turno}_{i}",
                    "content": f"contenuto {turno}_{i} " * parole,
                }
            )
        messaggi.append({"role": "user", "content": "Continua."})
    return messaggi


def make_context(settings, messaggi):
    request = ChatCompletionRequest.model_validate(
        {"model": "claude-opus-5", "messages": messaggi}
    )
    traduzione = build_anthropic_params(request, settings)
    ctx = RequestContext(
        request=request,
        settings=settings,
        store=None,
        client=None,
        counter=None,
        completion_id="test",
        model=traduzione.model,
        params=traduzione.params,
        stream=False,
    )
    ctx.history_turns = sum(1 for m in messaggi if m.get("role") == "assistant")
    return ctx


def svuotati(ctx) -> list[str]:
    """Identificatori dei blocchi che il server svuoterebbe."""
    effettivo = _apply_context_edits(ctx.params)
    fuori = []
    for messaggio in effettivo.get("messages") or []:
        for blocco in messaggio.get("content") or []:
            if not isinstance(blocco, dict) or blocco.get("type") != "tool_result":
                continue
            if RIMOSSO in json.dumps(blocco.get("content"), default=str):
                fuori.append(blocco.get("tool_use_id"))
    return fuori


# --- la regressione -------------------------------------------------------


async def test_i_blocchi_svuotati_restano_gli_stessi_mentre_la_chat_cresce(settings):
    """Il punto di tutto lo stadio.

    Con il confine mobile ogni turno svuota un insieme diverso, quindi il
    prefisso e' nuovo a ogni richiesta. A scatti resta fermo, e la cache regge.
    """
    settings.context.prune_step_turns = 4
    stadio = ContextStage(settings)

    insiemi = []
    for turni in (8, 9, 10):
        ctx = make_context(settings, conversazione(turni))
        await stadio.before(ctx)
        insiemi.append(tuple(svuotati(ctx)))

    assert insiemi[0], "qualcosa deve essere stato potato"
    assert len(set(insiemi)) == 1, "l'insieme svuotato deve restare identico"


async def test_il_confine_mobile_cambia_a_ogni_turno(settings):
    """Il comportamento di prima, tenuto come termine di paragone."""
    settings.context.prune_step_turns = 0  # scatto ridotto al minimo: insegue
    stadio = ContextStage(settings)

    insiemi = []
    for turni in (8, 9, 10):
        ctx = make_context(settings, conversazione(turni))
        await stadio.before(ctx)
        insiemi.append(tuple(svuotati(ctx)))

    assert len(set(insiemi)) == 3, "senza scatto ogni turno pota un insieme diverso"


async def test_lo_scatto_si_misura_in_turni_non_in_risultati(settings):
    """La proprieta' che giustifica l'unita' di misura.

    Un ciclo con sei chiamate per turno consuma i risultati sei volte piu' in
    fretta di uno che ne fa una. Se lo scatto fosse contato in risultati, lo
    stesso valore darebbe molti turni di stabilita' su un carico e nessuno
    sull'altro - il confine tornerebbe a inseguire, che e' il difetto da cui si
    e' partiti.

    Contato in turni, il confine si sposta con la stessa frequenza su entrambi:
    circa una volta ogni ``prune_step_turns``, qualunque sia il ritmo dei tool.
    """
    settings.context.prune_step_turns = 3
    stadio = ContextStage(settings)
    turni_osservati = range(6, 18)
    atteso = len(turni_osservati) // settings.context.prune_step_turns

    for per_turno in (1, 6):
        precedente, cambi = None, 0
        for turni in turni_osservati:
            ctx = make_context(settings, conversazione(turni, per_turno=per_turno))
            await stadio.before(ctx)
            corrente = tuple(svuotati(ctx))
            if precedente is not None and corrente != precedente:
                cambi += 1
            precedente = corrente

        assert cambi <= atteso + 1, (
            f"con {per_turno} risultati per turno il confine si muove troppo spesso: "
            f"{cambi} volte su {len(turni_osservati)} turni"
        )
        assert cambi < len(turni_osservati) - 1, (
            "un confine che cambia a ogni turno e' quello che si voleva evitare"
        )


# --- condizioni di attivazione --------------------------------------------


async def test_si_pota_quando_conviene_anche_senza_pressione_sulla_finestra(settings):
    """Due domande diverse - sono in pericolo, conviene - due condizioni."""
    settings.context.trigger_ratio = 0.99
    ctx = make_context(settings, conversazione(10))
    await ContextStage(settings).before(ctx)
    assert "context_management" in ctx.params


async def test_non_si_pota_quando_non_c_e_materiale(settings):
    settings.context.prune_min_prunable_tokens = 10**9
    ctx = make_context(settings, conversazione(10))
    await ContextStage(settings).before(ctx)
    assert "context_management" not in ctx.params


async def test_potatura_rinviata_sotto_uno_scatto(settings):
    """Meglio non potare che potare una sfoglia e buttare via la cache."""
    settings.context.prune_step_turns = 50
    ctx = make_context(settings, conversazione(6))
    await ContextStage(settings).before(ctx)

    assert svuotati(ctx) == []
    assert any("rinviata" in nota for nota in ctx.notes)


async def test_i_risultati_recenti_non_si_toccano_mai(settings):
    settings.context.prune_keep_tool_uses = 3
    settings.context.prune_step_turns = 2
    ctx = make_context(settings, conversazione(12))
    await ContextStage(settings).before(ctx)

    effettivo = _apply_context_edits(ctx.params)
    risultati = [
        blocco
        for messaggio in effettivo["messages"]
        for blocco in (messaggio.get("content") or [])
        if isinstance(blocco, dict) and blocco.get("type") == "tool_result"
    ]
    assert all(
        RIMOSSO not in json.dumps(blocco.get("content"), default=str)
        for blocco in risultati[-3:]
    )


# --- il modello del server ------------------------------------------------


def _payload(edit: dict, risultati: int = 10, contenuto: str = "x" * 400) -> dict:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"c{i}", "content": contenuto}
                    for i in range(risultati)
                ],
            }
        ],
        "context_management": {"edits": [edit]},
    }


def test_il_simulatore_rispetta_keep():
    payload = _payload(
        {"type": "clear_tool_uses_20250919", "keep": {"type": "tool_uses", "value": 4}}
    )
    assert json.dumps(_apply_context_edits(payload), default=str).count(RIMOSSO) == 6


def test_il_simulatore_rispetta_clear_at_least():
    """Sotto la soglia il contesto non viene toccato affatto."""
    payload = _payload(
        {
            "type": "clear_tool_uses_20250919",
            "keep": {"type": "tool_uses", "value": 0},
            "clear_at_least": {"type": "input_tokens", "value": 100_000},
        },
        risultati=1,
        contenuto="corto",
    )
    assert RIMOSSO not in json.dumps(_apply_context_edits(payload), default=str)


def test_il_simulatore_rispetta_il_trigger():
    payload = _payload(
        {
            "type": "clear_tool_uses_20250919",
            "keep": {"type": "tool_uses", "value": 0},
            "trigger": {"type": "tool_uses", "value": 50},
        }
    )
    assert RIMOSSO not in json.dumps(_apply_context_edits(payload), default=str)


def test_il_simulatore_rispetta_exclude_tools():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "name": "protetto",
                     "content": "x" * 400},
                    {"type": "tool_result", "tool_use_id": "b", "name": "normale",
                     "content": "x" * 400},
                ],
            }
        ],
        "context_management": {
            "edits": [
                {
                    "type": "clear_tool_uses_20250919",
                    "keep": {"type": "tool_uses", "value": 0},
                    "exclude_tools": ["protetto"],
                }
            ]
        },
    }
    assert json.dumps(_apply_context_edits(payload), default=str).count(RIMOSSO) == 1
