"""Test end-to-end del gateway attraverso lo stub dell'API."""

from __future__ import annotations

import json

from .conftest import chat_payload


def test_richiesta_semplice(client):
    response = client.post("/v1/chat/completions", json=chat_payload())
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "Risposta di prova."
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] > 0
    assert body["ecotokens"]["source"] == "api"


def test_parametri_non_supportati_non_arrivano_all_api(client):
    """temperature e top_p farebbero fallire la richiesta con un 400."""
    client.post(
        "/v1/chat/completions",
        json=chat_payload(temperature=0.7, top_p=0.5, frequency_penalty=1.0),
    )
    inviata = client.stub.last
    assert "temperature" not in inviata
    assert "top_p" not in inviata
    assert "frequency_penalty" not in inviata


def test_prefill_finale_rimosso(client):
    """Un assistant in coda e' un prefill: sui modelli attuali e' un 400."""
    payload = chat_payload()
    payload["messages"].append({"role": "assistant", "content": "Sto per dire"})
    client.post("/v1/chat/completions", json=payload)
    assert client.stub.last["messages"][-1]["role"] != "assistant"


def test_cache_letta_dal_secondo_turno(client):
    """Il criterio di accettazione centrale del progetto.

    Il primo turno scrive in cache, il secondo la rilegge. La scrittura iniziale
    non e' sprecata: prompt di sistema e definizioni dei tool sono condivisi
    anche fra conversazioni diverse.
    """
    first = chat_payload()
    client.post("/v1/chat/completions", json=first)
    assert _ha_marker(client.stub.last), "il primo turno deve scrivere in cache"

    second = chat_payload()
    second["messages"].append({"role": "assistant", "content": "Risposta di prova."})
    second["messages"].append({"role": "user", "content": "E adesso?"})
    client.post("/v1/chat/completions", json=second)
    assert _ha_marker(client.stub.last), "il secondo turno deve piazzare i breakpoint"

    third = chat_payload()
    third["messages"].append({"role": "assistant", "content": "Risposta di prova."})
    third["messages"].append({"role": "user", "content": "E adesso?"})
    third["messages"].append({"role": "assistant", "content": "Risposta di prova."})
    third["messages"].append({"role": "user", "content": "Un'altra cosa"})
    body = client.post("/v1/chat/completions", json=third).json()

    assert body["usage"]["prompt_tokens_details"]["cached_tokens"] > 0
    assert body["ecotokens"]["cached_prompt_tokens"] > 0


def test_cache_esatta_serve_senza_spendere(client):
    payload = chat_payload()
    client.post("/v1/chat/completions", json=payload)
    chiamate = len(client.stub.requests)

    body = client.post("/v1/chat/completions", json=payload).json()

    assert len(client.stub.requests) == chiamate, "la seconda richiesta non deve uscire"
    assert body["ecotokens"]["source"] == "exact_cache"
    assert body["ecotokens"]["cost_usd"] == 0
    assert body["ecotokens"]["saved_usd"] > 0
    assert body["choices"][0]["message"]["content"] == "Risposta di prova."


def test_streaming(client):
    payload = chat_payload(stream=True, stream_options={"include_usage": True})
    with client.stream("POST", "/v1/chat/completions", json=payload) as response:
        assert response.status_code == 200
        righe = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert righe[-1] == "data: [DONE]"
    chunk = [json.loads(line[6:]) for line in righe[:-1]]

    assert chunk[0]["choices"][0]["delta"]["role"] == "assistant"
    testo = "".join(
        item["choices"][0]["delta"].get("content", "")
        for item in chunk
        if item["choices"] and "content" in item["choices"][0]["delta"]
    )
    assert testo == "Risposta di prova."

    finali = [item for item in chunk if item["choices"] and item["choices"][0]["finish_reason"]]
    assert finali[-1]["choices"][0]["finish_reason"] == "stop"

    con_usage = [item for item in chunk if item.get("usage")]
    assert con_usage and con_usage[-1]["usage"]["completion_tokens"] > 0


def test_streaming_da_cache(client):
    """Chi chiede stream deve ricevere uno stream anche su un hit di cache."""
    payload = chat_payload()
    client.post("/v1/chat/completions", json=payload)

    payload["stream"] = True
    with client.stream("POST", "/v1/chat/completions", json=payload) as response:
        righe = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert righe[-1] == "data: [DONE]"
    chunk = [json.loads(line[6:]) for line in righe[:-1]]
    testo = "".join(
        item["choices"][0]["delta"].get("content", "")
        for item in chunk
        if item["choices"] and "content" in item["choices"][0]["delta"]
    )
    assert testo == "Risposta di prova."


def test_tool_call_completo(client):
    client.stub.reply_text = ""
    client.stub.tool_calls = [{"id": "toolu_1", "name": "get_weather", "input": {"city": "Roma"}}]

    payload = chat_payload(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "meteo",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
    )
    body = client.post("/v1/chat/completions", json=payload).json()

    assert body["choices"][0]["finish_reason"] == "tool_calls"
    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Roma"}


def test_tool_ordinati_deterministicamente(client):
    """Un ordine instabile dei tool invaliderebbe la cache a ogni richiesta."""
    tools = [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in ("zeta", "alfa", "media")
    ]
    client.post("/v1/chat/completions", json=chat_payload(tools=tools))
    inviati = [tool["name"] for tool in client.stub.last["tools"]]
    assert inviati == sorted(inviati)


def test_budget_blocca_prima_di_spendere(client, settings):
    settings.budget.enabled = True
    settings.budget.daily_usd = 0.0
    for stage in client.app.state.gateway.pipeline.stages:
        if stage.name == "budget":
            stage.enabled = True
            stage.config = settings.budget

    response = client.post("/v1/chat/completions", json=chat_payload(user="x"))
    assert response.status_code == 429
    assert "tetto giornaliero" in response.json()["error"]["message"].lower()
    assert not client.stub.requests, "nessuna richiesta deve raggiungere l'API"


def test_statistiche_registrano_il_risparmio(client):
    payload = chat_payload()
    client.post("/v1/chat/completions", json=payload)
    client.post("/v1/chat/completions", json=payload)  # hit di cache

    stats = client.get("/admin/stats").json()
    assert stats["requests"] == 2
    assert stats["saved_usd"] > 0
    origini = {row["source"] for row in stats["by_source"]}
    assert origini == {"api", "exact_cache"}


def test_sessione_riconosciuta_tra_i_turni(client):
    first = chat_payload()
    client.post("/v1/chat/completions", json=first)

    second = chat_payload()
    second["messages"].append({"role": "assistant", "content": "Risposta di prova."})
    second["messages"].append({"role": "user", "content": "Continuo"})
    client.post("/v1/chat/completions", json=second)

    sessioni = client.get("/admin/sessions").json()["sessions"]
    assert len(sessioni) == 1, "i due turni devono finire nella stessa sessione"
    assert sessioni[0]["turn_count"] == 2


def _ha_marker(payload: dict) -> bool:
    """Vero se la richiesta contiene almeno un cache_control."""
    testo = json.dumps(payload, default=str)
    return "cache_control" in testo
