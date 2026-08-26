"""Test del cache planner.

Le regole verificate qui sono tutte economiche: un breakpoint piazzato male non
produce errori, produce fatture piu' alte.
"""

from __future__ import annotations

import json

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.cache_planner import LOOKBACK_BLOCKS, CachePlannerStage
from ecotokens.store.repos import Session
from ecotokens.translate.to_anthropic import build_anthropic_params


def make_context(settings: Settings, messages, history_turns=1, session=None, model="claude-opus-5"):
    request = ChatCompletionRequest.model_validate({"model": model, "messages": messages})
    translation = build_anthropic_params(request, settings)
    return RequestContext(
        request=request,
        settings=settings,
        store=None,
        client=None,
        counter=None,
        completion_id="test",
        model=translation.model,
        params=translation.params,
        stream=False,
        history_turns=history_turns,
        session=session,
    )


def markers(params) -> int:
    return json.dumps(params, default=str).count('"cache_control"')


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def planner(settings) -> CachePlannerStage:
    return CachePlannerStage(settings)


def lungo(parole: int = 400) -> str:
    return "parola " * parole


async def test_il_primo_turno_scrive_in_cache(planner, settings):
    """Contro l'intuizione, ma misurato.

    Sembrerebbe uno spreco: il pareggio della cache e' a due richieste, quindi
    la scrittura del primo turno pare non ripagarsi. Il ragionamento pero'
    guarda solo ai turni della stessa conversazione e dimentica che il pezzo
    piu' grosso del prefisso - prompt di sistema e definizioni dei tool - e'
    condiviso anche fra conversazioni diverse: quella scrittura la rilegge la
    richiesta successiva, di chiunque sia.
    """
    ctx = make_context(
        settings,
        [{"role": "system", "content": lungo()}, {"role": "user", "content": "ciao"}],
        history_turns=0,
    )
    await planner.before(ctx)
    assert markers(ctx.params) > 0


async def test_il_primo_turno_si_puo_saltare(planner, settings):
    """L'opzione resta, per chi ha ogni richiesta con un prefisso unico."""
    settings.cache_planner.skip_first_turn = True
    planner = CachePlannerStage(settings)
    ctx = make_context(
        settings,
        [{"role": "system", "content": lungo()}, {"role": "user", "content": "ciao"}],
        history_turns=0,
    )
    await planner.before(ctx)
    assert markers(ctx.params) == 0
    assert any("primo turno" in nota for nota in ctx.notes)


async def test_nessun_marker_sotto_la_soglia_del_modello(planner, settings):
    """Sotto la soglia la cache non si crea e l'API non lo segnala."""
    ctx = make_context(
        settings,
        [{"role": "user", "content": "ciao"}, {"role": "assistant", "content": "ciao"},
         {"role": "user", "content": "come va?"}],
        history_turns=1,
    )
    await planner.before(ctx)
    assert markers(ctx.params) == 0
    assert any("sotto la soglia" in nota for nota in ctx.notes)


async def test_soglia_dipende_dal_modello(planner, settings):
    """Opus 5 chiede 512 token di prefisso, Haiku 4.5 ne chiede 4096."""
    messaggi = [
        {"role": "system", "content": "istruzione " * 300},
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "ciao"},
        {"role": "user", "content": "ancora"},
    ]
    opus = make_context(settings, messaggi, history_turns=1, model="claude-opus-5")
    await planner.before(opus)

    haiku = make_context(settings, messaggi, history_turns=1, model="claude-haiku-4-5")
    await planner.before(haiku)

    assert markers(opus.params) > 0
    assert markers(haiku.params) == 0


async def test_marker_su_system_e_ultimo_turno(planner, settings):
    ctx = make_context(
        settings,
        [
            {"role": "system", "content": lungo()},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "ciao"},
            {"role": "user", "content": "ancora"},
        ],
        history_turns=1,
    )
    await planner.before(ctx)

    assert ctx.params["system"][-1].get("cache_control") == {"type": "ephemeral"}
    assert ctx.params["messages"][-1]["content"][-1].get("cache_control") == {
        "type": "ephemeral"
    }


async def test_mai_oltre_quattro_breakpoint(planner, settings):
    """Il limite dell'API e' 4: superarlo e' un errore di richiesta."""
    tool_calls = [
        {"id": f"c{i}", "type": "function", "function": {"name": f"f{i}", "arguments": "{}"}}
        for i in range(30)
    ]
    messaggi = [
        {"role": "system", "content": lungo()},
        {"role": "user", "content": "fai molte cose"},
        {"role": "assistant", "tool_calls": tool_calls},
        *[{"role": "tool", "tool_call_id": f"c{i}", "content": "esito " * 20} for i in range(30)],
        {"role": "user", "content": "continua"},
    ]
    ctx = make_context(settings, messaggi, history_turns=1)
    await planner.before(ctx)

    assert 0 < markers(ctx.params) <= 4


async def test_marker_intermedi_solo_sui_turni_lunghi(planner, settings):
    """Servono quando un turno supera i 20 blocchi di lookback, non prima."""
    corta = make_context(
        settings,
        [
            {"role": "system", "content": lungo()},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "ciao"},
            {"role": "user", "content": "ancora"},
        ],
        history_turns=1,
    )
    await planner.before(corta)
    assert not any("intermedi" in nota for nota in corta.notes)

    tool_calls = [
        {"id": f"c{i}", "type": "function", "function": {"name": f"f{i}", "arguments": "{}"}}
        for i in range(LOOKBACK_BLOCKS + 6)
    ]
    lunga = make_context(
        settings,
        [
            {"role": "system", "content": lungo()},
            {"role": "user", "content": "molte cose"},
            {"role": "assistant", "tool_calls": tool_calls},
            *[
                {"role": "tool", "tool_call_id": f"c{i}", "content": "esito"}
                for i in range(LOOKBACK_BLOCKS + 6)
            ],
        ],
        history_turns=1,
    )
    await planner.before(lunga)
    assert any("intermedi" in nota for nota in lunga.notes)


async def test_ttl_lungo_solo_per_sessioni_lunghe_e_intermittenti(planner, settings):
    """Il TTL di un'ora costa il doppio in scrittura: serve traffico a intervalli."""
    import time

    messaggi = [
        {"role": "system", "content": lungo()},
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "ciao"},
        {"role": "user", "content": "ancora"},
    ]

    recente = Session(
        id="s1", fingerprint="f", model="claude-opus-5", created_at=time.time(),
        updated_at=time.time(), turn_count=5, message_count=4, locked_model=None,
    )
    ctx = make_context(settings, messaggi, history_turns=1, session=recente)
    await planner.before(ctx)
    assert ctx.cache_ttl == "5m", "traffico continuo: il TTL breve basta"

    intermittente = Session(
        id="s2", fingerprint="f", model="claude-opus-5", created_at=0,
        updated_at=time.time() - 1800, turn_count=5, message_count=4, locked_model=None,
    )
    ctx = make_context(settings, messaggi, history_turns=1, session=intermittente)
    await planner.before(ctx)
    assert ctx.cache_ttl == "1h"
    assert ctx.params["system"][-1]["cache_control"]["ttl"] == "1h"
