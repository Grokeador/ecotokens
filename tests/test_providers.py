"""Lo strato provider: traduzione, conteggi, fabbrica.

Nessun test tocca la rete. Gli adattatori parlano con un trasporto finto, e
cio' che si verifica e' **cosa sarebbe partito** e come viene riletto cio' che
torna: e' li' che un multi-provider sbaglia in silenzio, perche' una traduzione
storta non solleva niente - produce una richiesta valida che dice un'altra
cosa.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from ecotokens.config import Settings
from ecotokens.pricing import model_info
from ecotokens.providers.base import ErroreProvider, nome_a_valle
from ecotokens.providers.fabbrica import ProviderIgnoto, costruisci_client
from ecotokens.providers.google_ import AdattatoreGoogle
from ecotokens.providers.openai_compat import AdattatoreOpenAI


def _aggancia(adattatore, gestore):
    """Sostituisce il trasporto dell'adattatore con uno finto."""
    adattatore._client = httpx2.AsyncClient(
        base_url=str(adattatore._client.base_url),
        headers=adattatore._client.headers,
        transport=httpx2.MockTransport(gestore),
    )
    return adattatore


def _openai(gestore, *, max_retries: int = 0):
    return _aggancia(
        AdattatoreOpenAI(
            base_url="https://esempio.invalido/v1",
            api_key="chiave-di-prova",
            timeout=5.0,
            max_retries=max_retries,
        ),
        gestore,
    )


def _google(gestore):
    return _aggancia(
        AdattatoreGoogle(api_key="k", base_url=None, timeout=5.0, max_retries=0),
        gestore,
    )


RISPOSTA_MINIMA = {
    "id": "chatcmpl-1",
    "model": "gpt-di-prova",
    "choices": [{"finish_reason": "stop", "message": {"content": "ciao"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}


def _spia(risposta=None):
    """Un gestore che registra il corpo ricevuto e risponde una cosa fissa."""
    visto: dict = {}

    def gestore(richiesta):
        visto.clear()
        visto.update(json.loads(richiesta.content))
        visto["_url"] = str(richiesta.url)
        return httpx2.Response(200, json=risposta or RISPOSTA_MINIMA)

    return visto, gestore


# --- andata: cosa parte davvero -------------------------------------------


async def test_system_e_messaggi_in_dialetto_openai():
    visto, gestore = _spia()

    await _openai(gestore).messages.create(
        model="gpt-di-prova",
        max_tokens=100,
        system="sei un revisore",
        messages=[{"role": "user", "content": "ciao"}],
    )

    assert visto["messages"][0] == {"role": "system", "content": "sei un revisore"}
    assert visto["messages"][1] == {"role": "user", "content": "ciao"}
    assert visto["max_completion_tokens"] == 100


async def test_i_marker_di_cache_non_partono():
    """``cache_control`` e' un campo Anthropic: altrove e' un 400."""
    visto, gestore = _spia()

    await _openai(gestore).messages.create(
        model="gpt-di-prova",
        max_tokens=10,
        system=[{"type": "text", "text": "regole", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "ciao"}],
    )

    assert "cache_control" not in json.dumps(visto)
    assert visto["messages"][0]["content"] == "regole"


async def test_un_giro_di_tool_completo():
    """La forma che conta: chiamata, risultato, e il testo che li accompagna."""
    visto, gestore = _spia()

    await _openai(gestore).messages.create(
        model="gpt-di-prova",
        max_tokens=10,
        messages=[
            {"role": "user", "content": "leggi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "vado"},
                    {"type": "tool_use", "id": "t1", "name": "leggi", "input": {"f": "a.py"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "contenuto"}
                ],
            },
        ],
        tools=[
            {
                "name": "leggi",
                "description": "legge un file",
                "input_schema": {"type": "object", "properties": {"f": {"type": "string"}}},
            }
        ],
    )

    assistente = visto["messages"][1]
    assert assistente["tool_calls"][0]["id"] == "t1"
    assert assistente["tool_calls"][0]["function"]["name"] == "leggi"
    assert json.loads(assistente["tool_calls"][0]["function"]["arguments"]) == {"f": "a.py"}

    assert visto["messages"][2] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "contenuto",
    }
    assert visto["tools"][0]["function"]["parameters"]["properties"] == {
        "f": {"type": "string"}
    }


async def test_piu_risultati_diventano_piu_messaggi():
    """Tre tool in parallelo - la forma che costa un round-trip invece di tre -
    non devono perdersi passando da un formato che li vuole separati."""
    visto, gestore = _spia()

    await _openai(gestore).messages.create(
        model="gpt-di-prova",
        max_tokens=10,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": f"r{i}"}
                    for i in range(3)
                ]
                + [{"type": "text", "text": "e ora concludi"}],
            }
        ],
    )

    assert [m["role"] for m in visto["messages"]] == ["tool", "tool", "tool", "user"]
    assert [m["content"] for m in visto["messages"][:3]] == ["r0", "r1", "r2"]
    assert visto["messages"][-1]["content"] == "e ora concludi"


async def test_un_risultato_a_blocchi_non_diventa_json():
    """``tool_result`` accetta anche una lista di blocchi: mandarla serializzata
    farebbe pagare le graffe come testo."""
    visto, gestore = _spia()

    await _openai(gestore).messages.create(
        model="gpt-di-prova",
        max_tokens=10,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "riga uno"}],
                    }
                ],
            }
        ],
    )

    assert visto["messages"][0]["content"] == "riga uno"


# --- ritorno: come viene riletto cio' che torna ---------------------------


async def test_i_token_da_cache_non_si_contano_due_volte():
    """OpenAI include i token letti da cache nel totale, Anthropic no.

    Sommarli come stanno gonfierebbe il prompt proprio nelle richieste in cui
    il gateway sta risparmiando: il registro mostrerebbe il contrario di cio'
    che sta succedendo.
    """

    def gestore(richiesta):
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-2",
                "model": "gpt-di-prova",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 800},
                },
            },
        )

    messaggio = await _openai(gestore).messages.create(
        model="gpt-di-prova", max_tokens=10, messages=[{"role": "user", "content": "x"}]
    )

    assert messaggio.usage.input_tokens == 200
    assert messaggio.usage.cache_read_input_tokens == 800
    # Il totale del prompt resta quello dichiarato dal provider.
    assert messaggio.usage.input_tokens + messaggio.usage.cache_read_input_tokens == 1000


async def test_le_chiamate_tornano_come_blocchi_anthropic():
    def gestore(richiesta):
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-3",
                "model": "gpt-di-prova",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "cerca",
                                        "arguments": '{"q": "password"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    messaggio = await _openai(gestore).messages.create(
        model="gpt-di-prova", max_tokens=10, messages=[{"role": "user", "content": "x"}]
    )

    assert messaggio.stop_reason == "tool_use"
    blocco = messaggio.content[0]
    assert blocco.type == "tool_use"
    assert blocco.id == "call_1"
    assert blocco.name == "cerca"
    assert blocco.input == {"q": "password"}


async def test_argomenti_json_rotti_non_abbattono_il_giro():
    """Il modello sbaglia il JSON: e' un fatto da consegnargli dentro il
    risultato del tool, dove puo' correggersi, non un crollo del ciclo."""

    def gestore(richiesta):
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-4",
                "model": "gpt-di-prova",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "x", "arguments": "{rotto"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    messaggio = await _openai(gestore).messages.create(
        model="gpt-di-prova", max_tokens=10, messages=[{"role": "user", "content": "x"}]
    )
    assert messaggio.content[0].input["_argomenti_non_validi"] == "{rotto"


# --- errori ----------------------------------------------------------------


async def test_un_4xx_non_viene_ritentato():
    tentativi = {"n": 0}

    def gestore(richiesta):
        tentativi["n"] += 1
        return httpx2.Response(400, json={"error": {"message": "modello ignoto"}})

    with pytest.raises(ErroreProvider) as errore:
        await _openai(gestore, max_retries=3).messages.create(
            model="x", max_tokens=10, messages=[{"role": "user", "content": "x"}]
        )

    assert errore.value.stato == 400
    assert "modello ignoto" in str(errore.value)
    assert tentativi["n"] == 1, "una richiesta sbagliata resta sbagliata al secondo giro"


async def test_un_5xx_viene_ritentato():
    tentativi = {"n": 0}

    def gestore(richiesta):
        tentativi["n"] += 1
        return httpx2.Response(503, text="non disponibile")

    with pytest.raises(ErroreProvider):
        await _openai(gestore, max_retries=2).messages.create(
            model="x", max_tokens=10, messages=[{"role": "user", "content": "x"}]
        )
    assert tentativi["n"] == 3


async def test_lo_streaming_e_dichiarato_non_finto():
    """Uno streaming simulato - richiesta intera, poi spezzettata - sembrerebbe
    funzionare e cambierebbe la latenza senza dirlo."""

    def gestore(richiesta):  # pragma: no cover - non deve arrivarci
        raise AssertionError("non doveva partire nessuna richiesta")

    with pytest.raises(ErroreProvider) as errore:
        await _openai(gestore).messages.create(
            model="x",
            max_tokens=10,
            stream=True,
            messages=[{"role": "user", "content": "x"}],
        )
    assert errore.value.stato == 400


async def test_il_conteggio_dei_token_e_stimato_non_esplode():
    """Nessun provider non-Anthropic conta un prompt senza eseguirlo: la stima
    locale e' dichiarata, e deve comunque rispondere un numero."""

    def gestore(richiesta):  # pragma: no cover - non deve arrivarci
        raise AssertionError("count_tokens non deve chiamare la rete")

    conteggio = await _openai(gestore).messages.count_tokens(
        model="x", messages=[{"role": "user", "content": "una domanda qualunque"}]
    )
    assert conteggio.input_tokens > 0


# --- Google ----------------------------------------------------------------


async def test_google_separa_sistema_e_rinomina_l_assistente():
    visto, gestore = _spia(
        {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 2},
        }
    )

    messaggio = await _google(gestore).messages.create(
        model="gemini-di-prova",
        max_tokens=64,
        system="sei un revisore",
        messages=[
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "eccomi"},
        ],
    )

    assert visto["systemInstruction"]["parts"][0]["text"] == "sei un revisore"
    assert [c["role"] for c in visto["contents"]] == ["user", "model"]
    assert visto["generationConfig"]["maxOutputTokens"] == 64
    assert ":generateContent" in visto["_url"]
    assert messaggio.usage.input_tokens == 12
    assert messaggio.usage.output_tokens == 2


async def test_google_prende_la_chiave_dall_intestazione_non_dall_url():
    """Una chiave in query string finisce nei log del server e nei proxy."""
    adattatore = AdattatoreGoogle(api_key="segreta", base_url=None, timeout=5.0, max_retries=0)
    assert adattatore._client.headers.get("x-goog-api-key") == "segreta"
    assert "segreta" not in str(adattatore._client.base_url)


async def test_google_toglie_le_parole_di_schema_che_rifiuta():
    """``additionalProperties`` fa rispondere 400, e l'errore non dice quale
    campo: uno strumento valido altrove farebbe fallire l'intera richiesta."""
    visto, gestore = _spia(
        {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "x"}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
    )

    await _google(gestore).messages.create(
        model="g",
        max_tokens=10,
        messages=[{"role": "user", "content": "x"}],
        tools=[
            {
                "name": "s",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"a": {"type": "string", "default": "x"}},
                },
            }
        ],
    )

    partito = json.dumps(visto)
    assert "additionalProperties" not in partito
    assert "default" not in partito
    assert "properties" in partito


async def test_google_traduce_un_giro_di_tool():
    visto, gestore = _spia(
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"functionCall": {"name": "cerca", "args": {"q": "x"}}}]},
                }
            ],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        }
    )

    messaggio = await _google(gestore).messages.create(
        model="g",
        max_tokens=10,
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "cerca", "input": {"q": "x"}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "trovato"}
                ],
            },
        ],
    )

    assert visto["contents"][0]["parts"][0]["functionCall"]["name"] == "cerca"
    risposta = visto["contents"][1]["parts"][0]["functionResponse"]
    assert risposta["response"]["content"] == "trovato"
    assert messaggio.content[0].type == "tool_use"
    assert messaggio.content[0].name == "cerca"


async def test_google_senza_candidati_non_serve_una_risposta_vuota():
    """Gemini risponde 200 con zero candidati quando un filtro blocca tutto:
    senza controllo diventerebbe una risposta vuota servita come valida - e
    messa in cache."""

    def gestore(richiesta):
        return httpx2.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(ErroreProvider, match="SAFETY"):
        await _google(gestore).messages.create(
            model="g", max_tokens=10, messages=[{"role": "user", "content": "x"}]
        )


# --- fabbrica --------------------------------------------------------------


def test_il_provider_ignoto_si_ferma_all_avvio():
    """Un errore di configurazione scoperto sotto carico costa piu' di uno
    scoperto al lancio."""
    with pytest.raises(ProviderIgnoto):
        costruisci_client(Settings(upstream={"provider": "inesistente"}))


def test_anthropic_resta_il_client_dell_sdk():
    """Il percorso su cui i numeri sono misurati non passa da nessuna
    traduzione in piu'."""
    import anthropic

    client = costruisci_client(Settings(upstream={"provider": "anthropic", "api_key": "k"}))
    assert isinstance(client, anthropic.AsyncAnthropic)


def test_ollama_ha_un_indirizzo_predefinito_e_non_vuole_chiave():
    client = costruisci_client(Settings(upstream={"provider": "ollama"}))
    assert "11434" in str(client._client.base_url)
    assert "authorization" not in client._client.headers


def test_compatibile_senza_indirizzo_lo_dice():
    with pytest.raises(ProviderIgnoto, match="base_url"):
        costruisci_client(Settings(upstream={"provider": "compatibile"}))


def test_ogni_provider_noto_si_costruisce():
    """Un provider elencato ma non costruibile e' una promessa non mantenuta."""
    from ecotokens.providers.fabbrica import PROVIDER_NOTI

    for nome in PROVIDER_NOTI:
        impostazioni = {"provider": nome, "api_key": "k"}
        if nome == "compatibile":
            impostazioni["base_url"] = "https://esempio.invalido/v1"
        assert costruisci_client(Settings(upstream=impostazioni)) is not None


# --- tariffe ---------------------------------------------------------------


def test_un_modello_locale_costa_zero_e_perde_il_prefisso():
    info = model_info("locale/qwen2.5-coder:7b")
    assert (info.input_per_mtok, info.output_per_mtok) == (0.0, 0.0)
    assert info.provider == "locale"
    assert nome_a_valle("locale/qwen2.5-coder:7b") == "qwen2.5-coder:7b"


def test_un_modello_locale_non_ripiega_sulle_tariffe_di_claude():
    """E' il difetto che questa riga esiste per impedire: un modello gratuito
    prezzato come Opus inventa spesa che nessuno ha pagato."""
    from ecotokens.pricing import DEFAULT_MODEL

    assert model_info("locale/llama3.1:8b").id != DEFAULT_MODEL


def test_le_tariffe_esterne_si_dichiarano_non_si_indovinano():
    from ecotokens.pricing import DEFAULT_MODEL

    Settings(tariffe={"gpt-di-prova": {"input_per_mtok": 1.25, "output_per_mtok": 10.0}})
    info = model_info("gpt-di-prova")
    assert (info.input_per_mtok, info.output_per_mtok) == (1.25, 10.0)

    # Il registro e' di modulo: ripulirlo qui evita che gli altri test lo
    # ereditino secondo l'ordine di esecuzione.
    Settings()
    assert model_info("gpt-di-prova").id == DEFAULT_MODEL
