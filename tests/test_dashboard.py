"""La dashboard **con i dati dentro**.

`test_bench.py` gia' verifica che la pagina si generi senza misure registrate.
Ma quel percorso attraversa i rami "non c'e' niente da mostrare", che sono la
meta' piu' facile: la meta' che un utente vede davvero e' l'altra, e restava
scoperta - 189 righe su 389, la superficie non provata piu' grande del
progetto.

Il difetto tipico di una pagina generata non e' un errore di logica: e' una
chiave rinominata da una parte e non dall'altra, che con i dati assenti non si
manifesta affatto e con i dati presenti solleva alla prima riga.
"""

from __future__ import annotations

import anthropic
import httpx2
import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.dashboard import build_dashboard_data, render_dashboard
from ecotokens.server import Gateway
from ecotokens.simulator import create_stub


@pytest.fixture
async def con_traffico(tmp_path):
    """Un database vero, con dentro traffico vero.

    Non `:memory:`: la dashboard apre il proprio collegamento dal percorso in
    configurazione, e un database in memoria le arriverebbe vuoto - cioe' si
    tornerebbe a provare esattamente il ramo gia' coperto.
    """
    settings = Settings(profilo="prudente")
    settings.storage.path = str(tmp_path / "dati.db")
    settings.exact_cache.enabled = True

    gateway = Gateway(settings)
    stub_app, _ = create_stub()
    gateway.client = anthropic.AsyncAnthropic(
        api_key="prova",
        base_url="http://simulatore",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )
    await gateway.startup()

    def richiesta(testo: str) -> ChatCompletionRequest:
        return ChatCompletionRequest.model_validate(
            {
                "model": "claude-opus-5",
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": "Assistente. " * 200},
                    {"role": "user", "content": testo},
                ],
            }
        )

    try:
        for indice in range(6):
            await gateway.complete(richiesta(f"domanda {indice}"))
        # Una ripetuta, per far comparire anche la riga della cache esatta.
        await gateway.complete(richiesta("domanda 0"))
        yield settings
    finally:
        await gateway.shutdown()


async def test_la_pagina_si_genera_con_i_dati_dentro(con_traffico, tmp_path):
    dati = await build_dashboard_data(con_traffico, measure=False, project_root=tmp_path)
    pagina = render_dashboard(dati)

    assert pagina.startswith("<!doctype html>")
    assert "Nessuna richiesta registrata" not in pagina
    assert dati["live"], "il traffico registrato non e' arrivato alla pagina"


async def test_la_pagina_mostra_il_merito_e_non_solo_il_totale(con_traffico, tmp_path):
    """La correzione piu' importante di questa giornata deve valere anche qui:
    il risparmio totale si misura contro un client che non usa affatto la
    cache, e da solo attribuisce al gateway anche cio' che Anthropic da'
    gratis."""
    dati = await build_dashboard_data(con_traffico, measure=False, project_root=tmp_path)
    pagina = render_dashboard(dati)

    assert "Merito del gateway" in pagina
    assert "Chi marca il proprio system prompt" in pagina


async def test_il_registro_delle_correzioni_arriva_in_pagina(con_traffico, tmp_path):
    """E' la parte del progetto che racconta cosa si credeva e non era vero.
    Una dashboard che la perde diventa una vetrina."""
    dati = await build_dashboard_data(con_traffico, measure=False, project_root=tmp_path)
    pagina = render_dashboard(dati)

    assert len(dati["tuning"]) > 40
    assert "metro" in pagina.lower()


async def test_con_i_dati_dentro_resta_senza_javascript_e_senza_rete(
    con_traffico, tmp_path
):
    """Le due proprieta' erano provate sulla pagina vuota. I rami con i dati
    ne generano altro HTML, ed e' li' che una risorsa remota si intrufola."""
    import re

    dati = await build_dashboard_data(con_traffico, measure=False, project_root=tmp_path)
    pagina = render_dashboard(dati)

    assert "<script" not in pagina
    esterni = re.findall(r'(?:src|href)="(https?://[^"]+)"', pagina)
    assert all(
        "fonts.googleapis.com" in url or "fonts.gstatic.com" in url for url in esterni
    ), esterni


async def test_le_percentuali_in_pagina_non_sono_mai_infinite(con_traffico, tmp_path):
    """Una divisione per zero mascherata da percentuale e' il modo piu' rapido
    di rendere ridicola una pagina di misure."""
    dati = await build_dashboard_data(con_traffico, measure=False, project_root=tmp_path)
    pagina = render_dashboard(dati)

    for guasto in ("inf", "nan", "None%", "NaN"):
        assert f">{guasto}" not in pagina, guasto


async def test_la_pagina_completa_si_genera_da_una_misura_vera(tmp_path, monkeypatch):
    """Le sezioni delle misure - scenari, stadi, interazioni, compattazione -
    esistono solo dopo un banco vero, ed erano il pezzo scoperto piu' grande
    del progetto: 150 righe di rendering che un utente incontra al primo
    `ecotokens dashboard`.

    Si esegue con **un solo scenario** invece dei cinque: la misura completa
    costa 103 secondi, e raddoppiare la suite per un test e' un cattivo
    scambio. Costruire a mano il dizionario sarebbe stato piu' rapido e
    peggiore - una copia scritta a mano invecchia, e un test che passa su una
    forma che la produzione non produce piu' e' peggio di nessun test.
    """
    from ecotokens import dashboard as modulo
    from ecotokens.workloads import all_scenarios

    tutti = all_scenarios(None)
    monkeypatch.setattr(modulo, "all_scenarios", lambda root=None: tutti[:1])

    settings = Settings(profilo="prudente")
    settings.storage.path = str(tmp_path / "misure.db")

    dati = await build_dashboard_data(settings, measure=True, project_root=tmp_path)
    pagina = render_dashboard(dati)

    assert dati["scenarios"], "nessuno scenario misurato"
    assert dati["stages"], "nessun contributo per stadio"
    assert dati["totals"], "nessun totale"
    assert pagina.startswith("<!doctype html>")
    assert len(pagina) > 50_000, "la pagina completa e' molto piu' grande di quella vuota"
    for guasto in (">inf", ">nan", ">None"):
        assert guasto not in pagina, guasto
