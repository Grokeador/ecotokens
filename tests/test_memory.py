"""Test dello stadio di memoria."""

from __future__ import annotations

import asyncio
import json

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.memory import MemoryStage
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
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return FakeMessage(self.payload)


class FakeClient:
    def __init__(self, payload: str) -> None:
        self.messages = FakeMessages(payload)


@pytest.fixture
def settings() -> Settings:
    config = Settings()
    config.memory.enabled = True
    return config


@pytest.fixture
def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


def make_context(settings, messages, *, store=None, client=None, session=None, model="claude-opus-5"):
    request = ChatCompletionRequest.model_validate({"model": model, "messages": messages})
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


async def test_fatti_iniettati_in_coda(settings, store):
    """La memoria va dopo il prefisso in cache, mai nel system.

    Metterla in testa cambierebbe il prefisso a ogni turno e farebbe mancare
    la cache dell'intera conversazione.
    """
    sessione = await store.create_session("fp", "claude-opus-5")
    await store.add_facts(
        sessione.id,
        ["L'utente preferisce risposte in italiano", "Il progetto usa Python 3.13"],
    )

    ctx = make_context(
        settings,
        [
            {"role": "system", "content": "Istruzioni stabili."},
            {"role": "user", "content": "Che versione di Python usiamo nel progetto?"},
        ],
        store=store,
        session=sessione,
    )
    await MemoryStage(settings).before(ctx)

    # Il system top-level non e' stato toccato.
    assert ctx.params["system"] == [{"type": "text", "text": "Istruzioni stabili."}]

    ultimo = ctx.params["messages"][-1]
    assert ultimo["role"] == "system", "su Opus 5 la memoria usa il canale operatore"
    assert "memoria-rilevante" in ultimo["content"]
    assert "Python 3.13" in ultimo["content"]


async def test_memoria_degrada_sui_modelli_senza_system_a_meta(settings, store):
    sessione = await store.create_session("fp2", "claude-sonnet-5")
    await store.add_facts(sessione.id, ["Il progetto usa Python 3.13"])

    ctx = make_context(
        settings,
        [{"role": "user", "content": "Che versione di Python?"}],
        store=store,
        session=sessione,
        model="claude-sonnet-5",
    )
    await MemoryStage(settings).before(ctx)

    ultimo = ctx.params["messages"][-1]
    assert ultimo["role"] == "user"
    assert "memoria-rilevante" in ultimo["content"][-1]["text"]


async def test_nessuna_iniezione_senza_fatti_pertinenti(settings, store):
    sessione = await store.create_session("fp3", "claude-opus-5")
    await store.add_facts(sessione.id, ["Il gatto si chiama Fufi"])

    ctx = make_context(
        settings,
        [{"role": "user", "content": "Spiegami i tensori in algebra lineare"}],
        store=store,
        session=sessione,
    )
    prima = len(ctx.params["messages"])
    await MemoryStage(settings).before(ctx)

    assert len(ctx.params["messages"]) == prima


async def test_estrazione_dei_fatti_dopo_la_risposta(settings, store):
    """L'estrazione avviene in background: non deve pesare sulla latenza."""
    sessione = await store.create_session("fp4", "claude-opus-5")
    payload = json.dumps({"facts": ["L'utente si chiama Jorge", "Preferisce il TOML"]})
    client = FakeClient(payload)

    ctx = make_context(
        settings,
        [{"role": "user", "content": "Mi chiamo Jorge e preferisco il TOML"}],
        store=store,
        client=client,
        session=sessione,
    )
    stage = MemoryStage(settings)
    await stage.after(ctx, FakeMessage("Piacere, Jorge."))

    await asyncio.gather(*stage._tasks)

    fatti = await store.existing_facts(sessione.id)
    assert "L'utente si chiama Jorge" in fatti
    assert "Preferisce il TOML" in fatti


async def test_fatti_duplicati_non_si_accumulano(settings, store):
    sessione = await store.create_session("fp5", "claude-opus-5")
    await store.add_facts(sessione.id, ["L'utente si chiama Jorge"])

    client = FakeClient(json.dumps({"facts": ["L'utente si chiama Jorge"]}))
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Mi chiamo Jorge"}],
        store=store,
        client=client,
        session=sessione,
    )
    stage = MemoryStage(settings)
    await stage.after(ctx, FakeMessage("Ciao Jorge."))
    await asyncio.gather(*stage._tasks)

    righe = await store.db.query("SELECT COUNT(*) AS n FROM memory_facts", ())
    assert righe[0]["n"] == 1


async def test_estrazione_fallita_non_propaga(settings, store):
    """La risposta e' gia' partita: un errore qui non deve avere effetti."""
    sessione = await store.create_session("fp6", "claude-opus-5")

    class ClientRotto:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("API non raggiungibile")

    ctx = make_context(
        settings,
        [{"role": "user", "content": "qualcosa"}],
        store=store,
        client=ClientRotto(),
        session=sessione,
    )
    stage = MemoryStage(settings)
    await stage.after(ctx, FakeMessage("ok"))
    await asyncio.gather(*stage._tasks)

    assert await store.existing_facts(sessione.id) == set()
