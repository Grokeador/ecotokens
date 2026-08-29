"""Un nome di modello che non conosciamo non deve produrre dollari inventati.

`resolve_model` ripiega sul default quando non riconosce un nome, ed e' la
cosa giusta da fare per **servire** la richiesta: un guasto degrada, non
abbatte. Ma il ripiego passava anche a `pricing`, e li' diventava una bugia:
un `llama-3.3-70b` o un `claude-opuss-5` sbagliato di battitura veniva
prezzato a 5/25 USD per Mtok - le tariffe di Opus 5 - e finiva nel merito del
gateway come se quel confronto significasse qualcosa.

Il difetto non si vedeva perche' non produce nessun errore: produce un numero
plausibile. E' la stessa forma di tutti gli altri difetti del metro trovati in
questo progetto, e questi test esistono per impedirgli di tornare.
"""

from __future__ import annotations

import pytest

from ecotokens.pricing import DEFAULT_MODEL, model_info, modello_riconosciuto, resolve_model

from .conftest import chat_payload

# --- cosa conta come "riconosciuto" ---------------------------------------


@pytest.mark.parametrize(
    "nome",
    [
        "claude-opus-5",  # esatto
        "CLAUDE-OPUS-5",  # maiuscolo
        "  claude-sonnet-5  ",  # con spazi
        "opus",  # alias breve
        "gpt-4o",  # alias di provenienza OpenAI, mappato di proposito
        "claude-opus-5-20260101",  # suffisso di data
    ],
)
def test_i_nomi_del_catalogo_sono_riconosciuti(nome):
    assert modello_riconosciuto(nome) is True


@pytest.mark.parametrize(
    "nome",
    [
        "llama-3.3-70b",
        "qwen2.5-coder:32b",
        "mistral-local",
        "claude-opuss-5",  # errore di battitura: la forma piu' insidiosa
        "claude-opus-6",  # un modello futuro che ancora non conosciamo
    ],
)
def test_i_nomi_fuori_catalogo_non_lo_sono(nome):
    assert modello_riconosciuto(nome) is False


def test_nessun_nome_e_una_scelta_non_una_supposizione():
    """Senza `model` si usa il default della configurazione: e' deliberato."""
    assert modello_riconosciuto(None) is True
    assert modello_riconosciuto("") is True


def test_il_ripiego_resta_quello_di_prima():
    """Riconoscere non cambia cosa viene servito, solo cosa viene dichiarato."""
    for nome in ("llama-3.3-70b", "claude-opuss-5"):
        assert resolve_model(nome) == DEFAULT_MODEL
        assert model_info(nome).id == DEFAULT_MODEL


# --- e cosa succede a una richiesta vera ----------------------------------


def test_un_modello_ignoto_viene_servito_lo_stesso(client):
    """Prima regola del gateway: sta in mezzo, non puo' bloccare il passaggio."""
    risposta = client.post(
        "/v1/chat/completions", json=chat_payload(model="llama-3.3-70b")
    )
    assert risposta.status_code == 200
    assert risposta.json()["choices"][0]["message"]["content"]


def test_ma_non_entra_nel_merito_del_gateway(client):
    """La spesa resta registrata; il **confronto** no, perche' sarebbe finto."""
    client.post("/v1/chat/completions", json=chat_payload(model="llama-3.3-70b"))

    stats = client.get("/admin/stats").json()
    assert stats["requests"] == 1, "la richiesta deve restare contata"
    assert stats["cost_usd"] > 0, "la spesa e' reale e va registrata"
    assert stats["richieste_confrontabili"] == 0, (
        "una richiesta prezzata con le tariffe di un altro modello non puo' "
        "sostenere il confronto con nessun concorrente"
    )
    assert stats["baseline_ingenua_usd"] == 0


def test_un_modello_noto_invece_ci_entra(client):
    """Il controllo di sicurezza: senza questo, il test sopra passerebbe anche
    se avessimo escluso *tutto* dal confronto."""
    client.post("/v1/chat/completions", json=chat_payload(model="claude-opus-5"))

    stats = client.get("/admin/stats").json()
    assert stats["richieste_confrontabili"] == 1
    assert stats["baseline_ingenua_usd"] > 0


def test_il_motivo_e_scritto_nella_risposta(client):
    """Chi legge deve poter sapere **perche'** una richiesta non e' confrontata,
    e deve poterlo sapere dalla risposta stessa: la pagina di statistiche dice
    quante richieste reggono il confronto, ma non quali ne sono uscite."""
    risposta = client.post(
        "/v1/chat/completions", json=chat_payload(model="qwen2.5-coder:32b")
    ).json()

    note = risposta["ecotokens"]["notes"]
    assert any("sconosciuto" in nota for nota in note), note
    assert any("qwen2.5-coder:32b" in nota for nota in note), note


def test_un_modello_noto_non_produce_quella_nota(client):
    risposta = client.post(
        "/v1/chat/completions", json=chat_payload(model="claude-opus-5")
    ).json()
    note = risposta["ecotokens"]["notes"]
    assert not any("sconosciuto" in nota for nota in note), note
