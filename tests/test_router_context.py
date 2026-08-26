"""Test del router e della gestione del contesto."""

from __future__ import annotations

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.context import CONTEXT_MANAGEMENT_BETA, ContextStage
from ecotokens.pipeline.router import RouterStage
from ecotokens.store.db import Database
from ecotokens.store.repos import Store
from ecotokens.translate.to_anthropic import build_anthropic_params


class FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [FakeBlock(text)]


class FakeMessages:
    """Client finto per il riassunto: conta quante volte viene chiamato."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return FakeMessage(f"riassunto numero {self.calls}")


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


def make_context(settings, messages, *, store=None, client=None, session=None, **overrides):
    payload = {"model": "claude-opus-5", "messages": messages}
    payload.update(overrides)
    request = ChatCompletionRequest.model_validate(payload)
    translation = build_anthropic_params(request, settings)
    return RequestContext(
        request=request,
        settings=settings,
        store=store,
        client=client,
        counter=None,
        completion_id="test",
        model=translation.model,
        params=translation.params,
        stream=False,
        session=session,
    )


# --- router --------------------------------------------------------------


async def test_effort_abbassato_su_richiesta_semplice(settings):
    ctx = make_context(settings, [{"role": "user", "content": "Che ore sono a Roma?"}])
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "low"


async def test_effort_invariato_con_i_tool(settings):
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Che ore sono?"}],
        tools=[{"type": "function", "function": {"name": "ora", "parameters": {"type": "object"}}}],
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "high"


async def test_effort_invariato_se_lo_chiede_il_client(settings):
    ctx = make_context(
        settings, [{"role": "user", "content": "Che ore sono?"}], reasoning_effort="max"
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "max"


async def test_effort_invariato_su_richiesta_complessa(settings):
    ctx = make_context(
        settings, [{"role": "user", "content": "Analizza questo problema e spiega perche'"}]
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "high"


async def test_modello_non_cambia_di_default(settings):
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-opus-5"


async def test_modello_non_cambia_a_meta_conversazione(settings):
    """Cambiarlo azzererebbe la cache accumulata, che costa piu' del risparmio."""
    settings.router.model_downgrade = True
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    ctx.history_turns = 3
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-opus-5"
    assert any("cache e' legata al modello" in nota for nota in ctx.notes)


async def test_modello_declassato_a_inizio_sessione(settings):
    settings.router.model_downgrade = True
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    ctx.history_turns = 0
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-haiku-4-5"
    assert ctx.params["model"] == "claude-haiku-4-5"
    assert any("soglia" in nota or "richiede almeno" in nota for nota in ctx.notes)


# --- contesto ------------------------------------------------------------


async def test_potatura_non_attiva_sotto_la_soglia(settings):
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    await ContextStage(settings).before(ctx)
    assert "context_management" not in ctx.params
    assert ctx.betas == []


async def test_potatura_attiva_con_prompt_grande(settings):
    # Finestra ridotta artificialmente per non dover generare un milione di token.
    settings.context.trigger_ratio = 0.0001
    ctx = make_context(settings, [{"role": "user", "content": "testo " * 500}])
    await ContextStage(settings).before(ctx)

    assert ctx.params["context_management"]["edits"] == [{"type": "clear_tool_uses_20250919"}]
    assert CONTEXT_MANAGEMENT_BETA in ctx.betas


async def test_riassunto_calcolato_una_volta_e_riusato(settings, store):
    """Il punto cruciale: il riassunto deve restare identico tra i turni.

    Se cambiasse a ogni richiesta cambierebbe il prefisso del prompt, e la
    cache mancherebbe sempre: la compattazione costerebbe piu' di quanto fa
    risparmiare.
    """
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2

    messaggi = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]
    sessione = await store.create_session("fp", "claude-opus-5")
    client = FakeClient()

    primo = make_context(settings, messaggi, store=store, client=client, session=sessione)
    await ContextStage(settings).before(primo)
    testo_primo = primo.params["messages"][0]["content"][0]["text"]

    secondo = make_context(settings, messaggi, store=store, client=client, session=sessione)
    await ContextStage(settings).before(secondo)
    testo_secondo = secondo.params["messages"][0]["content"][0]["text"]

    assert "riassunto numero 1" in testo_primo
    assert testo_primo == testo_secondo, "il prefisso deve restare identico byte per byte"
    assert client.messages.calls == 1, "il riassunto non va ricalcolato a ogni turno"
    assert any("riusato il riassunto" in nota for nota in secondo.notes)


async def test_riassunto_fallito_non_rompe_la_richiesta(settings, store):
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2

    class ClientRotto:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("API non raggiungibile")

    messaggi = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]
    sessione = await store.create_session("fp2", "claude-opus-5")
    ctx = make_context(settings, messaggi, store=store, client=ClientRotto(), session=sessione)
    prima = len(ctx.params["messages"])

    await ContextStage(settings).before(ctx)

    assert len(ctx.params["messages"]) == prima, "la conversazione resta integrale"
    assert any("non riuscito" in nota for nota in ctx.notes)
