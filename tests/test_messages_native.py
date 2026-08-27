"""Test della porta nativa ``POST /v1/messages``.

Il gateway parla con un solo provider - Anthropic - ma accetta due dialetti.
Quello OpenAI serve alle applicazioni che gia' esistono; questo serve ai client
che parlano gia' Claude e che, passando dall'altra porta, dovrebbero farsi
tradurre due volte per tornare al punto di partenza.

La proprieta' piu' importante verificata qui e' che le due porte condividono
la stessa cache: la stessa domanda posta nei due dialetti deve costare una
volta sola.
"""

from __future__ import annotations

import json


def payload(testo: str = "Ciao", **extra):
    corpo = {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": testo}],
        "max_tokens": 1024,
    }
    corpo.update(extra)
    return corpo


def test_richiesta_nativa_risponde_nel_formato_anthropic(client):
    risposta = client.post("/v1/messages", json=payload())
    assert risposta.status_code == 200, risposta.text

    corpo = risposta.json()
    # Forma Anthropic, non OpenAI: niente "choices", niente "object".
    assert corpo["type"] == "message"
    assert corpo["role"] == "assistant"
    assert "choices" not in corpo
    assert corpo["content"][0]["text"] == "Risposta di prova."
    assert corpo["ecotokens"]["source"] == "api"


def test_la_richiesta_nativa_non_viene_tradotta(client):
    """Il corpo deve arrivare all'API come l'ha scritto il client."""
    client.post("/v1/messages", json=payload("Domanda diretta"))
    inviata = client.stub.last
    assert inviata["messages"][0]["content"] == "Domanda diretta"


def test_i_parametri_rifiutati_dai_modelli_attuali_vengono_tolti(client):
    """Anche un client nativo puo' mandarli per abitudine: sarebbe un 400."""
    risposta = client.post("/v1/messages", json=payload(temperature=0.7, top_p=0.4))
    assert risposta.status_code == 200
    inviata = client.stub.last
    assert "temperature" not in inviata
    assert "top_p" not in inviata
    assert any("campionamento" in nota for nota in risposta.json()["ecotokens"]["notes"])


def test_la_pipeline_lavora_anche_sulla_porta_nativa(client):
    """Cache planner, sessioni e contabilita' non dipendono dal dialetto."""
    lungo = {"model": "claude-opus-5", "max_tokens": 1024,
             "system": [{"type": "text", "text": "istruzione " * 900}],
             "messages": [{"role": "user", "content": "ciao"}]}
    client.post("/v1/messages", json=lungo)
    assert "cache_control" in json.dumps(client.stub.last, default=str)

    stats = client.get("/admin/stats").json()
    assert stats["requests"] == 1


def test_la_cache_esatta_serve_anche_le_richieste_native(client):
    corpo = payload()
    client.post("/v1/messages", json=corpo)
    chiamate = len(client.stub.requests)

    risposta = client.post("/v1/messages", json=corpo).json()
    assert len(client.stub.requests) == chiamate, "la seconda non deve uscire"
    assert risposta["ecotokens"]["source"] == "exact_cache"
    assert risposta["content"][0]["text"] == "Risposta di prova."


def test_le_due_porte_condividono_la_cache(client):
    """La stessa domanda nei due dialetti deve costare una volta sola.

    E' la conseguenza di calcolare la chiave sui parametri Anthropic invece
    che sulla richiesta in arrivo: dopo la traduzione le due richieste sono la
    stessa cosa, e non c'e' motivo di pagarla due volte.
    """
    client.post(
        "/v1/chat/completions",
        json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "Ciao"}],
              "max_tokens": 1024},
    )
    chiamate = len(client.stub.requests)

    risposta = client.post("/v1/messages", json=payload("Ciao")).json()
    assert len(client.stub.requests) == chiamate, "il dialetto non deve creare una voce nuova"
    assert risposta["ecotokens"]["source"] == "exact_cache"
    assert risposta["type"] == "message", "la risposta esce nel dialetto di chi ha chiesto"


def test_streaming_nativo(client):
    with client.stream("POST", "/v1/messages", json=payload(stream=True)) as risposta:
        assert risposta.status_code == 200
        righe = [r for r in risposta.iter_lines() if r]

    eventi = [r[7:] for r in righe if r.startswith("event: ")]
    assert eventi[0] == "message_start"
    assert eventi[-1] == "message_stop"

    dati = [json.loads(r[6:]) for r in righe if r.startswith("data: ")]
    testo = "".join(
        d.get("delta", {}).get("text", "")
        for d in dati
        if d.get("type") == "content_block_delta"
    )
    assert testo == "Risposta di prova."


def test_streaming_nativo_da_cache(client):
    """Chi chiede uno stream deve riceverne uno anche su un hit di cache."""
    client.post("/v1/messages", json=payload())

    with client.stream("POST", "/v1/messages", json=payload(stream=True)) as risposta:
        righe = [r for r in risposta.iter_lines() if r]

    eventi = [r[7:] for r in righe if r.startswith("event: ")]
    assert eventi[0] == "message_start"
    assert eventi[-1] == "message_stop"


def test_corpo_non_valido(client):
    assert client.post("/v1/messages", json={"model": "claude-opus-5"}).status_code == 400
    assert client.post("/v1/messages", json={"messages": []}).status_code == 400
