"""Test del traduttore OpenAI -> Anthropic."""

from __future__ import annotations

import json

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.translate.from_anthropic import to_openai_response, usage_payload
from ecotokens.translate.to_anthropic import build_anthropic_params
from ecotokens.pricing import Usage
from ecotokens.wording import OPERATOR_OPEN


@pytest.fixture
def settings() -> Settings:
    return Settings()


def translate(settings, **payload):
    payload.setdefault("model", "claude-opus-5")
    payload.setdefault("messages", [{"role": "user", "content": "ciao"}])
    request = ChatCompletionRequest.model_validate(payload)
    return build_anthropic_params(request, settings)


def test_parametri_di_campionamento_scartati(settings):
    result = translate(settings, temperature=0.5, top_p=0.9, seed=42)
    assert set(result.dropped) == {"temperature", "top_p", "seed"}
    assert "temperature" not in result.params


def test_alias_dei_modelli(settings):
    assert translate(settings, model="gpt-4o").model == "claude-opus-5"
    assert translate(settings, model="gpt-4o-mini").model == "claude-haiku-4-5"
    assert translate(settings, model="claude-sonnet-5").model == "claude-sonnet-5"


def test_system_iniziale_diventa_campo_system(settings):
    result = translate(
        settings,
        messages=[
            {"role": "system", "content": "Istruzioni."},
            {"role": "user", "content": "ciao"},
        ],
    )
    assert result.params["system"] == [{"type": "text", "text": "Istruzioni."}]
    assert len(result.params["messages"]) == 1


def test_system_a_meta_conversazione_resta_nei_messaggi(settings):
    """Su Opus 5 e' un canale nativo che non invalida il prefisso in cache."""
    result = translate(
        settings,
        model="claude-opus-5",
        messages=[
            {"role": "user", "content": "ciao"},
            {"role": "system", "content": "Modalita' concisa."},
        ],
    )
    assert result.params["messages"][-1] == {
        "role": "system",
        "content": "Modalita' concisa.",
    }


def test_system_a_meta_degrada_sui_modelli_senza_supporto(settings):
    result = translate(
        settings,
        model="claude-sonnet-5",
        messages=[
            {"role": "user", "content": "ciao"},
            {"role": "system", "content": "Modalita' concisa."},
        ],
    )
    ultimo = result.params["messages"][-1]
    assert ultimo["role"] == "user"
    assert OPERATOR_OPEN in ultimo["content"][0]["text"]
    assert "Modalita' concisa." in ultimo["content"][0]["text"]


def test_prefill_finale_rimosso(settings):
    result = translate(
        settings,
        messages=[
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "Sto per"},
        ],
    )
    assert len(result.params["messages"]) == 1
    assert any("prefill" in nota for nota in result.notes)


def test_primo_messaggio_sempre_user(settings):
    result = translate(settings, messages=[{"role": "assistant", "content": "gia' detto"}])
    assert result.params["messages"][0]["role"] == "user"


def test_tool_ordinati_per_nome(settings):
    tools = [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in ("zeta", "alfa", "media")
    ]
    result = translate(settings, tools=tools)
    assert [tool["name"] for tool in result.params["tools"]] == ["alfa", "media", "zeta"]


def test_tool_choice(settings):
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    assert translate(settings, tools=tools, tool_choice="required").params["tool_choice"] == {
        "type": "any"
    }
    assert translate(settings, tools=tools, tool_choice="none").params["tool_choice"] == {
        "type": "none"
    }
    scelto = translate(
        settings, tools=tools, tool_choice={"type": "function", "function": {"name": "f"}}
    )
    assert scelto.params["tool_choice"] == {"type": "tool", "name": "f"}


def test_ciclo_di_tool_completo(settings):
    result = translate(
        settings,
        messages=[
            {"role": "user", "content": "meteo?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "meteo", "arguments": '{"citta": "Roma"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "18 gradi"},
        ],
    )
    messaggi = result.params["messages"]
    assert messaggi[1]["content"][0]["type"] == "tool_use"
    assert messaggi[1]["content"][0]["input"] == {"citta": "Roma"}
    assert messaggi[2]["content"][0]["type"] == "tool_result"
    assert messaggi[2]["content"][0]["tool_use_id"] == "call_1"


def test_tool_result_paralleli_accorpati(settings):
    """Spezzarli su piu' messaggi scoraggia le chiamate parallele."""
    result = translate(
        settings,
        messages=[
            {"role": "user", "content": "due cose"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "1"},
            {"role": "tool", "tool_call_id": "b", "content": "2"},
        ],
    )
    ultimo = result.params["messages"][-1]
    assert len(ultimo["content"]) == 2
    assert all(block["type"] == "tool_result" for block in ultimo["content"])


def test_argomenti_malformati_non_fanno_esplodere(settings):
    result = translate(
        settings,
        messages=[
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "f", "arguments": "non-json"}}
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
        ],
    )
    assert result.params["messages"][1]["content"][0]["input"] == {
        "_raw_arguments": "non-json"
    }


def test_immagine_base64(settings):
    result = translate(
        settings,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "cosa vedi?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ],
    )
    blocchi = result.params["messages"][0]["content"]
    assert blocchi[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "AAAA",
    }


def test_response_format_json_schema(settings):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    result = translate(
        settings,
        response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": schema}},
    )
    assert result.params["output_config"]["format"] == {
        "type": "json_schema",
        "schema": schema,
    }


def test_response_format_json_object_diventa_istruzione(settings):
    """Non esiste una modalita' JSON senza schema: si istruisce in coda."""
    result = translate(settings, response_format={"type": "json_object"})
    assert "format" not in result.params["output_config"]
    testo = json.dumps(result.params["messages"], ensure_ascii=False)
    assert "oggetto JSON valido" in testo


def test_max_tokens_limitato_al_tetto_del_modello(settings):
    result = translate(settings, model="claude-haiku-4-5", max_tokens=999_999)
    assert result.params["max_tokens"] == 64_000


def test_usage_riporta_il_prompt_intero():
    """prompt_tokens deve essere la somma dei tre contatori di input."""
    usage = Usage(input_tokens=100, output_tokens=20, cache_creation_tokens=300, cache_read_tokens=600)
    payload = usage_payload(usage)
    assert payload["prompt_tokens"] == 1000
    assert payload["prompt_tokens_details"]["cached_tokens"] == 600
    assert payload["total_tokens"] == 1020


def test_risposta_con_rifiuto():
    class Blocco:
        type = "text"
        text = ""

    class Dettagli:
        category = "cyber"
        explanation = "no"

    class Messaggio:
        content = [Blocco()]
        stop_reason = "refusal"
        stop_details = Dettagli()

    risposta = to_openai_response(Messaggio(), model="claude-opus-5", usage=Usage())
    assert risposta["choices"][0]["finish_reason"] == "content_filter"
    assert risposta["choices"][0]["ecotokens_refusal"]["category"] == "cyber"
