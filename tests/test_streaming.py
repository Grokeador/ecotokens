"""Test della traduzione in streaming.

Due percorsi, e quello meno ovvio e' il piu' importante: quando un client
chiede ``stream: true`` e la risposta arriva dalla **cache**, non c'e' nessuno
stream da tradurre - va inventato. Se quel codice sbaglia, il client fallisce
nel proprio parser invece che nel gateway, e l'errore che vede l'utente non
somiglia per niente alla causa.

E' il caso felice della funzione che risparmia di piu' - un hit di cache - e
prima di questo file non era esercitato da nessun test.
"""

from __future__ import annotations

import json

import pytest

from ecotokens.translate.stream import replay_response_as_stream


def risposta_cache(**extra) -> dict:
    base = {
        "id": "chatcmpl-abc",
        "model": "claude-opus-5",
        "created": 1700000000,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Ciao, come posso aiutarti?"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108},
    }
    base.update(extra)
    return base


async def raccogli(generatore) -> list[str]:
    return [pezzo async for pezzo in generatore]


def eventi(pezzi: list[str]) -> list[dict]:
    """I payload JSON, escluso il [DONE] finale."""
    fuori = []
    for pezzo in pezzi:
        corpo = pezzo.removeprefix("data: ").strip()
        if corpo and corpo != "[DONE]":
            fuori.append(json.loads(corpo))
    return fuori


# --- la forma del flusso ---------------------------------------------------


async def test_una_risposta_dalla_cache_esce_come_flusso_valido():
    pezzi = await raccogli(
        replay_response_as_stream(risposta_cache(), include_usage=False)
    )
    assert pezzi[-1].strip().endswith("[DONE]"), "un client aspetta il [DONE]"

    payload = eventi(pezzi)
    assert payload[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert all(voce["object"] == "chat.completion.chunk" for voce in payload)


async def test_il_testo_arriva_intero_anche_se_spezzato():
    """Spezzare e' un dettaglio di trasporto: il contenuto non deve cambiare."""
    risposta = risposta_cache()
    atteso = risposta["choices"][0]["message"]["content"]

    pezzi = await raccogli(
        replay_response_as_stream(risposta, include_usage=False, chunk_size=3)
    )
    ricostruito = "".join(
        voce["choices"][0]["delta"].get("content", "")
        for voce in eventi(pezzi)
        if voce["choices"]
    )
    assert ricostruito == atteso


async def test_l_identita_della_risposta_non_cambia_fra_un_pezzo_e_l_altro():
    """`id`, `model` e `created` devono restare gli stessi su tutti i chunk.

    Un client che li usa per raggruppare i pezzi di una risposta vedrebbe
    altrimenti piu' risposte diverse dove ce n'e' una sola.
    """
    pezzi = await raccogli(
        replay_response_as_stream(risposta_cache(), include_usage=True, chunk_size=4)
    )
    payload = eventi(pezzi)
    assert len({voce["id"] for voce in payload}) == 1
    assert len({voce["model"] for voce in payload}) == 1
    assert len({voce["created"] for voce in payload}) == 1


async def test_il_finish_reason_arriva_una_volta_sola_e_alla_fine():
    pezzi = await raccogli(
        replay_response_as_stream(risposta_cache(), include_usage=False, chunk_size=5)
    )
    payload = eventi(pezzi)
    con_ragione = [
        indice
        for indice, voce in enumerate(payload)
        if voce["choices"] and voce["choices"][0].get("finish_reason")
    ]
    assert len(con_ragione) == 1
    assert con_ragione[0] == len(payload) - 1


# --- l'usage, che e' il motivo per cui esiste il gateway -------------------


async def test_l_usage_esce_solo_se_richiesto():
    senza = eventi(
        await raccogli(replay_response_as_stream(risposta_cache(), include_usage=False))
    )
    assert all("usage" not in voce for voce in senza)

    con = eventi(
        await raccogli(replay_response_as_stream(risposta_cache(), include_usage=True))
    )
    ultimi = [voce for voce in con if "usage" in voce]
    assert len(ultimi) == 1
    assert ultimi[0]["usage"]["total_tokens"] == 108
    # Il chunk di usage non porta scelte: e' la convenzione che i client
    # OpenAI si aspettano, e uno `choices` non vuoto li confonderebbe.
    assert ultimi[0]["choices"] == []


async def test_il_blocco_ecotokens_sopravvive_allo_streaming():
    """E' l'unico posto dove un client vede costo e risparmio di quella risposta.

    Perderlo in streaming significherebbe che l'informazione esiste solo per
    meta' del traffico, e chi la legge non se ne accorgerebbe.
    """
    risposta = risposta_cache(ecotokens={"source": "exact_cache", "saved_usd": 0.01})
    payload = eventi(
        await raccogli(replay_response_as_stream(risposta, include_usage=True))
    )
    finale = [voce for voce in payload if "usage" in voce][0]
    assert finale["ecotokens"]["source"] == "exact_cache"


# --- i casi limite ---------------------------------------------------------


async def test_una_risposta_con_soli_tool_call_esce_comunque():
    """Un hit di cache su una risposta senza testo non deve produrre un flusso vuoto."""
    risposta = risposta_cache()
    risposta["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"x"}'},
            }
        ],
    }
    risposta["choices"][0]["finish_reason"] = "tool_calls"

    payload = eventi(await raccogli(replay_response_as_stream(risposta, include_usage=False)))
    chiamate = [
        voce for voce in payload if voce["choices"] and voce["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(chiamate) == 1
    assert chiamate[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert payload[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_una_risposta_vuota_produce_un_flusso_ben_formato():
    """Meglio uno stream corto che uno stream rotto."""
    risposta = risposta_cache()
    risposta["choices"][0]["message"] = {"role": "assistant", "content": ""}
    pezzi = await raccogli(replay_response_as_stream(risposta, include_usage=False))
    assert pezzi[-1].strip().endswith("[DONE]")
    assert eventi(pezzi), "almeno il chunk di apertura deve esserci"


async def test_una_risposta_senza_choices_non_fa_esplodere_il_gateway():
    """Difesa contro una voce di cache scritta da una versione precedente."""
    pezzi = await raccogli(
        replay_response_as_stream({"id": "x", "model": "m"}, include_usage=False)
    )
    assert pezzi[-1].strip().endswith("[DONE]")


@pytest.mark.parametrize("dimensione", [1, 7, 1000])
async def test_la_dimensione_dei_pezzi_non_cambia_il_contenuto(dimensione):
    risposta = risposta_cache()
    atteso = risposta["choices"][0]["message"]["content"]
    payload = eventi(
        await raccogli(
            replay_response_as_stream(risposta, include_usage=False, chunk_size=dimensione)
        )
    )
    ricostruito = "".join(
        voce["choices"][0]["delta"].get("content", "") for voce in payload if voce["choices"]
    )
    assert ricostruito == atteso
