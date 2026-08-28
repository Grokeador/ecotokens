"""Infrastruttura comune ai test: gateway collegato allo stub, senza rete."""

from __future__ import annotations

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from ecotokens.config import Settings
from ecotokens.server import create_app

from ecotokens.simulator import StubState, create_stub


@pytest.fixture
def settings() -> Settings:
    """Configurazione di test: database in memoria, stadi rumorosi spenti.

    Profilo **prudente** di proposito. Il profilo predefinito declassa il
    modello, e un test che verifica dove finisce un breakpoint di cache o come
    viene tradotto un parametro non deve dipendere da quella scelta: se
    cambiasse, fallirebbe una dozzina di test che non c'entrano niente, e il
    guasto sembrerebbe stare dove non sta. Il profilo aggressivo ha i suoi
    test, in `test_profilo.py`.
    """
    config = Settings(profilo="prudente")
    config.storage.path = ":memory:"
    config.memory.enabled = False
    config.semantic_cache.enabled = False
    config.budget.enabled = False
    return config


@pytest.fixture
def stub() -> tuple[object, StubState]:
    return create_stub()


@pytest.fixture
def client(settings, stub):
    """TestClient del gateway, con l'SDK puntato allo stub via ASGI.

    Il client Anthropic viene sostituito dopo la costruzione dell'app, cosi'
    tutto il resto (pipeline, rotte, storage) resta quello di produzione.
    """
    stub_app, state = stub
    app = create_app(settings)

    gateway = app.state.gateway
    gateway.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url="http://stub",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )
    # Gli stadi hanno gia' catturato il client precedente solo tramite il
    # contesto, che viene costruito a ogni richiesta: basta aggiornare qui.
    gateway.counter._client = gateway.client

    with TestClient(app) as test_client:
        test_client.stub = state
        # Comodo per i test che devono guardare il registro dopo una richiesta
        # invece che la risposta: il gateway e' lo stesso oggetto che ha
        # servito la chiamata, non una copia costruita a parte.
        test_client.gateway = gateway
        yield test_client


def chat_payload(**overrides):
    """Richiesta OpenAI minima, con un system abbastanza lungo da essere cacheable."""
    payload = {
        "model": "claude-opus-5",
        "messages": [
            {"role": "system", "content": "Sei un assistente. " * 200},
            {"role": "user", "content": "Ciao, come stai?"},
        ],
    }
    payload.update(overrides)
    return payload
