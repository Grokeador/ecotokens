"""Compatibilita' con l'SDK OpenAI vero.

E' il criterio di accettazione principale del progetto: un'applicazione
esistente deve funzionare cambiando soltanto ``base_url``. Verificarlo con
asserzioni sul JSON non basta, perche' il vero giudice e' il parser del client:
un campo mancante o di tipo sbagliato fallisce li', non qui.

Il client OpenAI parla con il gateway via ASGI e il gateway parla con lo stub
via ASGI: due livelli, nessuna porta aperta, nessun token speso. Il transport
ASGI e' asincrono, quindi si usa ``AsyncOpenAI``: la versione sincrona non puo'
pilotare un'app ASGI senza un server vero in mezzo.
"""

from __future__ import annotations

import anthropic
import httpx
import httpx2
import pytest_asyncio
from openai import AsyncOpenAI

from ecotokens.config import Settings
from ecotokens.server import create_app

from ecotokens.simulator import create_stub


@pytest_asyncio.fixture
async def openai_client():
    settings = Settings()
    settings.storage.path = ":memory:"
    settings.memory.enabled = False
    settings.semantic_cache.enabled = False

    stub_app, state = create_stub()
    app = create_app(settings)
    gateway = app.state.gateway
    gateway.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url="http://stub",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )

    # ASGITransport non esegue il lifespan dell'app: lo si avvia a mano.
    await gateway.startup()

    client = AsyncOpenAI(
        base_url="http://gateway/v1",
        api_key="non-serve",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)),
    )
    client._ecotokens_stub = state
    try:
        yield client
    finally:
        await client.close()
        await gateway.shutdown()


async def test_chat_completion_con_sdk_openai(openai_client):
    completion = await openai_client.chat.completions.create(
        model="claude-opus-5",
        messages=[
            {"role": "system", "content": "Sei un assistente."},
            {"role": "user", "content": "Ciao"},
        ],
        temperature=0.7,  # il gateway lo scarta: l'API Claude lo rifiuterebbe
    )

    assert completion.choices[0].message.content == "Risposta di prova."
    assert completion.choices[0].message.role == "assistant"
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage.prompt_tokens > 0
    assert completion.usage.completion_tokens > 0
    assert completion.model == "claude-opus-5"


async def test_streaming_con_sdk_openai(openai_client):
    stream = await openai_client.chat.completions.create(
        model="claude-opus-5",
        messages=[{"role": "user", "content": "Ciao"}],
        stream=True,
    )
    pezzi = []
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta:
            pezzi.append(chunk.choices[0].delta.content or "")
    assert "".join(pezzi) == "Risposta di prova."


async def test_tool_calling_con_sdk_openai(openai_client):
    openai_client._ecotokens_stub.reply_text = ""
    openai_client._ecotokens_stub.tool_calls = [
        {"id": "toolu_1", "name": "get_weather", "input": {"city": "Roma"}}
    ]

    completion = await openai_client.chat.completions.create(
        model="claude-opus-5",
        messages=[{"role": "user", "content": "Che tempo fa a Roma?"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Meteo di una citta'",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
    )

    assert completion.choices[0].finish_reason == "tool_calls"
    call = completion.choices[0].message.tool_calls[0]
    assert call.function.name == "get_weather"
    assert call.function.arguments == '{"city": "Roma"}'


async def test_elenco_modelli_con_sdk_openai(openai_client):
    modelli = await openai_client.models.list()
    identificativi = [modello.id for modello in modelli.data]
    assert "claude-opus-5" in identificativi
    assert "claude-haiku-4-5" in identificativi
