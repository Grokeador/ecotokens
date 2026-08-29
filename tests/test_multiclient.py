"""Piu' applicazioni dietro lo stesso gateway: chi ha speso cosa, e chi si ferma.

Con una sola chiave anonima e un solo tetto globale non si sa chi ha speso cosa,
e un ciclo impazzito consuma il tetto di tutti. Le chiavi con un nome chiudono
la prima meta'; i tetti per client la seconda.

Il tetto per client si controlla **oltre** a quello globale e mai al suo posto:
dieci client ciascuno sotto il proprio limite sfondano comunque il totale, e i
test qui sotto lo fissano perche' e' precisamente il tipo di svista che una
rete di sicurezza nuova rende possibile.
"""

from __future__ import annotations

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from ecotokens.config import Settings
from ecotokens.server import create_app
from ecotokens.simulator import create_stub

from .conftest import chat_payload

CHIAVI = {"applicazione-a": "chiave-a", "applicazione-b": "chiave-b"}


@pytest.fixture
def multi():
    """Gateway con due chiavi che hanno un nome."""
    config = Settings(profilo="prudente")
    config.storage.path = ":memory:"
    config.memory.enabled = False
    config.semantic_cache.enabled = False
    config.exact_cache.enabled = False  # ogni richiesta deve costare davvero
    config.budget.enabled = False
    config.server.chiavi = dict(CHIAVI)

    app = create_app(config)
    gateway = app.state.gateway
    stub_app, _ = create_stub()
    gateway.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url="http://stub",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )
    gateway.counter._client = gateway.client
    with TestClient(app) as client:
        client.gateway = gateway
        client.settings = config
        yield client


def stats(client):
    """Anche `/admin` e' protetto dalle chiavi: senza, torna 401 e il test
    fallirebbe leggendo il corpo dell'errore invece dei dati."""
    risposta = client.get(
        "/admin/stats", headers={"Authorization": "Bearer chiave-a"}
    )
    assert risposta.status_code == 200, risposta.text
    return risposta.json()


def chiedi(client, chiave: str | None, **extra):
    intestazioni = {"Authorization": f"Bearer {chiave}"} if chiave else {}
    return client.post(
        "/v1/chat/completions", json=chat_payload(**extra), headers=intestazioni
    )


# --- attribuzione ---------------------------------------------------------


def test_ogni_chiave_porta_il_proprio_nome(multi):
    assert chiedi(multi, "chiave-a").status_code == 200
    assert chiedi(multi, "chiave-b", messages=[
        {"role": "system", "content": "Sei un assistente. " * 200},
        {"role": "user", "content": "altra domanda"},
    ]).status_code == 200

    per_client = {
        riga["client"]: riga for riga in stats(multi)["by_client"]
    }
    assert set(per_client) == set(CHIAVI)
    for riga in per_client.values():
        assert riga["requests"] == 1
        assert riga["cost_usd"] > 0


def test_una_chiave_sconosciuta_non_passa(multi):
    assert chiedi(multi, "chiave-inventata").status_code == 401


def test_senza_chiave_non_passa(multi):
    assert multi.post("/v1/chat/completions", json=chat_payload()).status_code == 401


def test_chi_spende_di_piu_sta_in_cima(multi):
    """L'ordinamento non e' estetico: la domanda che si fa a questa tabella e'
    'chi mi sta costando', e la risposta deve stare sulla prima riga."""
    for indice in range(3):
        chiedi(multi, "chiave-a", messages=[
            {"role": "system", "content": "Sei un assistente. " * 200},
            {"role": "user", "content": f"domanda {indice}"},
        ])
    chiedi(multi, "chiave-b")

    righe = stats(multi)["by_client"]
    assert righe[0]["client"] == "applicazione-a"
    assert righe[0]["requests"] == 3


# --- i tetti --------------------------------------------------------------


def test_il_tetto_di_un_client_non_ferma_l_altro(multi):
    """La proprieta' per cui esistono i tetti per client."""
    multi.settings.budget.enabled = True
    multi.settings.budget.daily_usd = 1_000.0  # il globale non deve interferire
    multi.settings.budget.monthly_usd = 1_000.0
    multi.settings.budget.tetti_client = {"applicazione-a": 0.0001}
    for stadio in multi.gateway.pipeline.stages:
        if stadio.name == "budget":
            stadio.enabled = True

    # La prima di A passa (non ha ancora speso), la seconda no.
    assert chiedi(multi, "chiave-a").status_code == 200
    esaurito = chiedi(multi, "chiave-a", messages=[
        {"role": "system", "content": "Sei un assistente. " * 200},
        {"role": "user", "content": "ancora"},
    ])
    assert esaurito.status_code != 200
    assert "applicazione-a" in esaurito.text

    # B non ha un tetto proprio e continua.
    assert chiedi(multi, "chiave-b", messages=[
        {"role": "system", "content": "Sei un assistente. " * 200},
        {"role": "user", "content": "domanda di b"},
    ]).status_code == 200


def test_il_tetto_globale_li_ferma_comunque_tutti_e_due(multi):
    """Dieci client ciascuno sotto il proprio limite sfondano il totale: il
    tetto globale resta l'ultima difesa, e il per-client non lo sostituisce."""
    multi.settings.budget.enabled = True
    multi.settings.budget.daily_usd = 0.0001
    multi.settings.budget.client_daily_usd = 1_000.0
    for stadio in multi.gateway.pipeline.stages:
        if stadio.name == "budget":
            stadio.enabled = True

    chiedi(multi, "chiave-a")  # consuma il globale
    for chiave in ("chiave-a", "chiave-b"):
        risposta = chiedi(multi, chiave, messages=[
            {"role": "system", "content": "Sei un assistente. " * 200},
            {"role": "user", "content": f"dopo il tetto, {chiave}"},
        ])
        assert risposta.status_code != 200, chiave


def test_un_client_senza_nome_non_ha_tetto_proprio():
    """Applicare il tetto comune agli anonimi li metterebbe in un mucchio solo,
    dove il primo che spende blocca tutti gli altri."""
    from ecotokens.pipeline.budget import BudgetStage

    config = Settings()
    config.budget.client_daily_usd = 5.0
    stadio = BudgetStage(config)
    assert stadio._tetto_del_client("") == 0.0
    assert stadio._tetto_del_client("applicazione-a") == 5.0


def test_l_eccezione_per_nome_vince_sul_tetto_comune():
    from ecotokens.pipeline.budget import BudgetStage

    config = Settings()
    config.budget.client_daily_usd = 5.0
    config.budget.tetti_client = {"applicazione-b": 0.5}
    stadio = BudgetStage(config)
    assert stadio._tetto_del_client("applicazione-a") == 5.0
    assert stadio._tetto_del_client("applicazione-b") == 0.5
