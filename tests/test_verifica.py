"""Test di `ecotokens verifica`.

Il comando confronta le assunzioni del simulatore con l'API vera. Qui non c'e'
l'API vera - i test non devono richiedere rete - quindi si prova cio' che si
puo' provare senza: che i controlli girino, che il conto delle chiamate sia
quello dichiarato, che i nomi combacino col registro, e soprattutto **che il
comando dica di essere circolare quando lo e'**.

Quest'ultimo e' il test che conta. Un comando di verifica puntato al
simulatore produce una schermata di spunte verdi che non porta nessuna
informazione, ed e' la stessa forma di errore che in questo progetto ha gia'
dichiarato tre volte il gateway dannoso o inutile: uno strumento che risponde
in modo plausibile a una domanda che non ha fatto.
"""

from __future__ import annotations

import anthropic
import httpx2
import pytest

from ecotokens.assunzioni import ASSUNZIONI
from ecotokens.simulator import create_stub
from ecotokens.verifica import (
    CHIAMATE_PREVISTE,
    COMBACIA,
    CONTROLLI,
    nomi_coperti,
    nomi_scoperti,
    verifica,
)


@pytest.fixture
def client_simulato():
    stub_app, _ = create_stub()
    client = anthropic.AsyncAnthropic(
        api_key="prova",
        base_url="http://simulatore",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )
    yield client


# --- la proprieta' che viene prima di tutte --------------------------------


async def test_contro_il_simulatore_il_rapporto_si_dichiara_circolare(client_simulato):
    """Senza questa riga il comando produrrebbe cinque spunte verdi
    indistinguibili da una verifica vera."""
    rapporto = await verifica(client_simulato, "claude-opus-5", circolare=True)
    assert rapporto.circolare is True
    assert "non dice niente sull'API vera" in rapporto.riepilogo()


async def test_una_verifica_vera_non_porta_quell_avvertimento(client_simulato):
    """Il contrario dello stesso test: l'avvertimento deve sparire quando non
    serve, o si impara a ignorarlo."""
    rapporto = await verifica(client_simulato, "claude-opus-5")
    assert "ATTENZIONE" not in rapporto.riepilogo()


# --- i controlli girano ----------------------------------------------------


async def test_tutti_i_controlli_concludono(client_simulato):
    """Un controllo che si rompe da solo non dice niente sull'assunzione, ed e'
    per questo che ha un esito suo invece di contare come divergenza."""
    rapporto = await verifica(client_simulato, "claude-opus-5", circolare=True)
    assert len(rapporto.controlli) == len(CONTROLLI)
    for controllo in rapporto.controlli:
        assert controllo.esito == COMBACIA, f"{controllo.assunzione}: {controllo.osservato}"


async def test_il_conto_delle_chiamate_dichiarato_e_quello_vero(client_simulato):
    """Il comando annuncia quante chiamate fara' **prima** di farle: un comando
    che spende deve dire quanto. Se il numero fosse solo scritto in una
    costante, invecchierebbe al primo controllo aggiunto."""
    rapporto = await verifica(client_simulato, "claude-opus-5", circolare=True)
    assert rapporto.chiamate == CHIAMATE_PREVISTE


# --- l'aggancio al registro delle assunzioni -------------------------------


def test_ogni_controllo_nomina_un_assunzione_che_esiste():
    """Un nome sbagliato non fallisce: produce un rapporto che parla di una
    voce inesistente, e sembra piu' completo di quello che e'."""
    nomi = {a.nome for a in ASSUNZIONI}
    assert nomi_coperti() <= nomi, nomi_coperti() - nomi


def test_il_rapporto_dice_anche_cosa_non_controlla():
    """Un elenco di cio' che si sa fare, senza quello di cio' che non si sa
    fare, si legge come se coprisse tutto."""
    scoperte = nomi_scoperti()
    assert scoperte
    assert not (scoperte & nomi_coperti())
    assert "Quanti tool result conserva la potatura" in scoperte


# --- cio' che il simulatore ha imparato a rifiutare ------------------------


def test_il_simulatore_rifiuta_il_quinto_breakpoint(client):
    """Trovato dal giro circolare: il simulatore ne accettava cinque.

    Non era una semplificazione innocua. Un pianificatore che ne emettesse
    cinque avrebbe superato ogni test e sarebbe fallito solo in produzione -
    con il test verde proprio sul caso che doveva cogliere.
    """
    risposta = client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-5",
            "max_tokens": 16,
            "system": [
                {"type": "text", "text": f"blocco {i} " * 50,
                 "cache_control": {"type": "ephemeral"}}
                for i in range(5)
            ],
            "messages": [{"role": "user", "content": "ok"}],
        },
    )
    assert risposta.status_code == 400
    assert "cache_control" in risposta.text


def test_il_simulatore_rifiuta_i_parametri_che_l_api_rifiuta():
    """Protegge i test del file piu' delicato del progetto.

    Si interroga il simulatore **direttamente**, non attraverso il gateway:
    passando dal gateway il parametro non arriverebbe mai, perche' la
    sanificazione lo toglie - ed e' proprio quella che si vuole poter mettere
    alla prova. Se domani smettesse di rimuovere `temperature`, con un
    simulatore tollerante tutti i test resterebbero verdi mentre il gateway
    darebbe 400 su ogni richiesta di un client OpenAI, cioe' su tutte.
    """
    from fastapi.testclient import TestClient

    stub_app, _ = create_stub()
    with TestClient(stub_app) as diretto:
        risposta = diretto.post(
            "/v1/messages",
            json={
                "model": "claude-opus-5",
                "max_tokens": 16,
                "temperature": 0.5,
                "messages": [{"role": "user", "content": "ok"}],
            },
        )
    assert risposta.status_code == 400
    assert "temperature" in risposta.text


def test_la_sanificazione_regge_davanti_a_un_simulatore_severo(client):
    """E adesso questo test significa qualcosa: passa perche' il gateway
    rimuove i parametri, non perche' il simulatore li ignora."""
    risposta = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "ciao"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.2,
        },
    )
    assert risposta.status_code == 200, risposta.text
    assert not (set(client.stub.last) & {"temperature", "top_p", "frequency_penalty"})
