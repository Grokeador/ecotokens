"""Test del banco di misura e della dashboard.

Il banco produce i numeri su cui si prendono decisioni di configurazione: se
misura male, si ottimizza nella direzione sbagliata con la massima fiducia. Qui
si vincolano le proprieta' che rendono il confronto onesto.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecotokens.bench import (
    ABLATION_STEPS,
    BASELINE_VARIANT,
    FULL_VARIANT,
    Comparison,
    Measurement,
    _abilita_cache_planner,
    _run_scenario,
    load_runs,
    make_settings,
    open_results_store,
    run_benchmark,
    save_run,
    stage_contributions,
    stage_contributions_from_results,
    stage_progress,
    BenchRun,
    run_ablation,
)
from ecotokens.config import Settings
from ecotokens.dashboard import build_dashboard_data, render_dashboard
from ecotokens.workloads import (
    all_scenarios,
    scenario_chat,
    scenario_costruzione,
    scenario_ripetitivo,
    scenarios_by_name,
)

PROGETTO = Path(__file__).resolve().parent.parent


# --- carichi ---------------------------------------------------------------


def test_gli_scenari_rispediscono_la_cronologia():
    """Ogni turno reinvia tutto: e' cio' che fanno i client OpenAI."""
    scenario = scenario_chat(turns=4)
    lunghezze = [len(richiesta["messages"]) for richiesta in scenario.requests]
    assert lunghezze == sorted(lunghezze)
    assert lunghezze[-1] > lunghezze[0]


def test_lo_scenario_costruzione_usa_i_file_veri():
    scenario = scenario_costruzione(PROGETTO, max_files=4)
    assert scenario.size > 1
    testo = str(scenario.requests[-1]["messages"])
    assert "ecotokens/" in testo, "deve contenere percorsi reali del progetto"
    assert "def " in testo, "deve contenere codice reale, non testo inventato"


def test_scenario_costruzione_su_cartella_vuota(tmp_path):
    """Un progetto senza sorgenti non deve far esplodere il banco."""
    scenario = scenario_costruzione(tmp_path)
    assert scenario.size == 0


def test_scenario_sconosciuto():
    with pytest.raises(ValueError, match="Scenario sconosciuto"):
        scenarios_by_name(["inesistente"], PROGETTO)


# --- confronto -------------------------------------------------------------


async def test_la_variante_di_riferimento_non_ottimizza_nulla():
    scenario = scenario_chat(turns=3)
    misura = await _run_scenario(scenario, make_settings(None), BASELINE_VARIANT, live=False)

    assert misura.cache_read_tokens == 0
    assert misura.cache_write_tokens == 0
    assert misura.full_price_tokens == misura.prompt_tokens
    assert misura.upstream_calls == misura.requests


async def test_il_gateway_riduce_i_token_a_prezzo_pieno():
    run = await run_benchmark(scenarios=[scenario_chat(turns=5)], label="test")
    prima = run.totals(BASELINE_VARIANT)
    dopo = run.totals(FULL_VARIANT)

    assert dopo.full_price_tokens < prima.full_price_tokens
    assert dopo.cache_read_tokens > 0
    assert dopo.cost_usd < prima.cost_usd


async def test_la_cache_esatta_evita_le_chiamate():
    run = await run_benchmark(scenarios=[scenario_ripetitivo(uniche=2, ripetizioni=3)], label="test")
    prima = run.totals(BASELINE_VARIANT)
    dopo = run.totals(FULL_VARIANT)

    assert prima.upstream_calls == prima.requests
    assert dopo.upstream_calls < dopo.requests


async def test_le_varianti_non_si_contaminano():
    """Ogni combinazione parte da cache vuota.

    Se il riferimento riempisse la cache usata poi dalla variante ottimizzata,
    il confronto misurerebbe un risparmio che non esiste.
    """
    scenario = scenario_chat(turns=3)
    prima = await _run_scenario(scenario, make_settings(None), BASELINE_VARIANT, live=False)
    ripetuta = await _run_scenario(scenario, make_settings(None), BASELINE_VARIANT, live=False)

    assert prima.cost_usd == pytest.approx(ripetuta.cost_usd)
    assert ripetuta.cache_read_tokens == 0


async def test_il_carico_e_identico_nelle_due_varianti():
    """Il numero di richieste deve essere lo stesso: cambia solo come vengono servite."""
    run = await run_benchmark(scenarios=[scenario_chat(turns=4)], label="test")
    assert run.totals(BASELINE_VARIANT).requests == run.totals(FULL_VARIANT).requests


def test_il_confronto_calcola_il_risparmio():
    prima = Measurement(scenario="x", variant="a", cost_usd=1.0, full_price_tokens=1000)
    dopo = Measurement(scenario="x", variant="b", cost_usd=0.25, full_price_tokens=100)
    confronto = Comparison(scenario="x", before=prima, after=dopo)

    assert confronto.saved_usd == pytest.approx(0.75)
    assert confronto.saved_ratio == pytest.approx(0.75)
    assert confronto.tokens_avoided == 900


# --- ablazione -------------------------------------------------------------


async def test_l_ablazione_attribuisce_il_risparmio():
    run = await run_ablation(scenarios=[scenario_chat(turns=4)], label="test")
    contributi = stage_contributions(run)

    assert [c["stage"] for c in contributi] == [
        "prompt caching",
        "potatura contesto",
        "cache esatta",
        "effort adattivo",
        "riscrittura prompt",
    ]
    # Il cumulato dell'ultimo gradino e' il risparmio totale della catena.
    riferimento = run.totals(BASELINE_VARIANT).cost_usd
    completo = run.totals(ABLATION_STEPS[-1][0]).cost_usd
    assert contributi[-1]["cumulative_usd"] == pytest.approx(riferimento - completo)


async def test_il_prompt_caching_e_lo_stadio_dominante():
    run = await run_ablation(scenarios=[scenario_chat(turns=5)], label="test")
    contributi = {c["stage"]: c["saved_usd"] for c in stage_contributions(run)}
    assert contributi["prompt caching"] == max(contributi.values())


# --- persistenza e dashboard ----------------------------------------------


async def test_le_misure_restano_registrate():
    database, store = open_results_store(":memory:")
    try:
        run = await run_benchmark(scenarios=[scenario_chat(turns=3)], label="registrata")
        await save_run(store, run, corpus="confronto")

        storico = await load_runs(store)
        assert len(storico) == 1
        assert storico[0]["label"] == "registrata"
        assert len(storico[0]["results"]) == len(run.measurements)
    finally:
        database.close()


async def test_la_dashboard_si_genera_senza_misure():
    """Senza dati registrati la pagina deve comunque essere valida."""
    settings = Settings()
    settings.storage.path = ":memory:"
    dati = await build_dashboard_data(settings, measure=False, project_root=PROGETTO)
    pagina = render_dashboard(dati)

    assert pagina.startswith("<!doctype html>")
    assert "Bilancio token" in pagina
    assert "Nessuna richiesta registrata" in pagina


def test_la_dashboard_non_ha_dipendenze_esterne():
    """Deve funzionare offline: nessuna risorsa remota oltre ai font."""
    import re

    dati = {
        "generated_at": 0, "mode": "simulato", "scenarios": [], "stages": [],
        "interactions": [], "history": [], "totals": None, "live": None,
        "config": [], "tuning": [],
    }
    pagina = render_dashboard(dati)

    esterni = re.findall(r'(?:src|href)="(https?://[^"]+)"', pagina)
    assert all("fonts.googleapis.com" in url or "fonts.gstatic.com" in url for url in esterni), (
        f"riferimenti esterni non ammessi: {esterni}"
    )
    assert "<script" not in pagina, "la pagina non deve dipendere da JavaScript"


def test_la_dashboard_definisce_entrambi_i_temi():
    """Un colore definito solo dentro una media query non si applica mai
    nello stato predefinito, e la pagina risulterebbe illeggibile."""
    dati = {
        "generated_at": 0, "mode": "simulato", "scenarios": [], "stages": [],
        "interactions": [], "history": [], "totals": None, "live": None,
        "config": [], "tuning": [],
    }
    pagina = render_dashboard(dati)

    assert "prefers-color-scheme: dark" in pagina
    assert ':root[data-theme="dark"]' in pagina
    assert ':root:not([data-theme="light"])' in pagina
    # Il fondo del body deve venire da un token, altrimenti eredita quello dell ospite.
    assert "background: var(--ground)" in pagina


async def test_la_dashboard_completa_contiene_le_sezioni():
    settings = Settings()
    settings.storage.path = ":memory:"
    dati = await build_dashboard_data(settings, measure=False, project_root=PROGETTO)
    dati["scenarios"] = [
        {
            "name": "chat", "description": "prova", "requests": 4,
            "upstream_before": 4, "upstream_after": 4,
            "cost_before": 1.0, "cost_after": 0.4, "saved_ratio": 0.6,
            "cache_ratio": 0.7, "tokens_avoided": 900,
        }
    ]
    dati["totals"] = {
        "requests": 4, "cost_before": 1.0, "cost_after": 0.4, "saved_usd": 0.6,
        "saved_ratio": 0.6, "prompt_tokens": 1000,
        "flow_before": {"full": 1000, "write": 0, "read": 0},
        "flow_after": {"full": 100, "write": 200, "read": 700},
        "upstream_before": 4, "upstream_after": 4,
        "output_before": 100, "output_after": 80,
    }
    pagina = render_dashboard(dati)

    for titolo in (
        "Dove finiscono i token di prompt",
        "Per tipo di carico",
        "Configurazione in vigore",
        "Cosa e' cambiato misurando",
    ):
        assert titolo in pagina, f"sezione mancante: {titolo}"
    assert "60.0%" in pagina


def test_tutti_gli_scenari_sono_disponibili():
    nomi = {scenario.name for scenario in all_scenarios(PROGETTO)}
    assert nomi == {"chat", "agente", "ripetitivo", "prompt-verboso", "costruzione"}


# --- progressi fra versioni -----------------------------------------------


def test_i_contributi_si_ricostruiscono_da_una_misura_registrata():
    """Una misura vecchia deve restare interrogabile anche dopo."""
    righe = [
        {"variant": BASELINE_VARIANT, "cost_usd": 10.0},
        {"variant": "+ prompt caching", "cost_usd": 4.0},
        {"variant": "+ potatura contesto", "cost_usd": 4.0},
        {"variant": "+ cache esatta", "cost_usd": 3.0},
        {"variant": "+ effort adattivo", "cost_usd": 2.5},
        {"variant": "+ riscrittura prompt", "cost_usd": 2.4},
    ]
    contributi = stage_contributions_from_results(righe)
    per_nome = {c["stage"]: c for c in contributi}

    assert per_nome["prompt caching"]["saved_ratio"] == pytest.approx(0.6)
    assert per_nome["potatura contesto"]["saved_ratio"] == pytest.approx(0.0)
    assert per_nome["riscrittura prompt"]["cumulative_ratio"] == pytest.approx(0.76)


def test_una_misura_di_una_versione_piu_vecchia_si_ferma_al_gradino_mancante():
    """Non si inventa uno zero: i gradini sono cumulativi e sarebbero falsati."""
    righe = [
        {"variant": BASELINE_VARIANT, "cost_usd": 10.0},
        {"variant": "+ prompt caching", "cost_usd": 4.0},
        # gradini successivi assenti: misura di prima che esistessero
    ]
    contributi = stage_contributions_from_results(righe)
    assert [c["stage"] for c in contributi] == ["prompt caching"]


async def test_il_confronto_fra_versioni_riconosce_i_miglioramenti():
    database, store = open_results_store(":memory:")
    try:
        vecchia = BenchRun(id="v1", label="prima", mode="simulato", created_at=1.0)
        vecchia.measurements = [
            Measurement(scenario="x", variant=BASELINE_VARIANT, cost_usd=10.0),
            Measurement(scenario="x", variant="+ prompt caching", cost_usd=5.0),
            Measurement(scenario="x", variant="+ potatura contesto", cost_usd=5.0),
            Measurement(scenario="x", variant="+ cache esatta", cost_usd=4.5),
            Measurement(scenario="x", variant="+ effort adattivo", cost_usd=4.5),
            Measurement(scenario="x", variant="+ riscrittura prompt", cost_usd=4.5),
        ]
        await save_run(store, vecchia, corpus="prova")

        nuova = BenchRun(id="v2", label="dopo", mode="simulato", created_at=2.0)
        nuova.measurements = [
            Measurement(scenario="x", variant=BASELINE_VARIANT, cost_usd=10.0),
            Measurement(scenario="x", variant="+ prompt caching", cost_usd=4.0),
            Measurement(scenario="x", variant="+ potatura contesto", cost_usd=4.5),
            Measurement(scenario="x", variant="+ cache esatta", cost_usd=4.0),
            Measurement(scenario="x", variant="+ effort adattivo", cost_usd=4.0),
            Measurement(scenario="x", variant="+ riscrittura prompt", cost_usd=4.0),
        ]
        await save_run(store, nuova, corpus="prova")

        corrente = stage_contributions(nuova)
        progresso = await stage_progress(store, corrente, corpus="prova")

        assert progresso["available"]
        stati = {voce["stage"]: voce["status"] for voce in progresso["stages"]}
        assert stati["prompt caching"] == "migliorato"
        assert stati["potatura contesto"] == "peggiorato"
        assert stati["cache esatta"] == "invariato"
    finally:
        database.close()


async def test_senza_una_misura_precedente_il_confronto_si_astiene():
    database, store = open_results_store(":memory:")
    try:
        progresso = await stage_progress(store, [], corpus="inesistente")
        assert progresso["available"] is False
        assert progresso["runs_found"] == 0
    finally:
        database.close()


# --- riga di comando ------------------------------------------------------


def test_il_filtro_di_scenario_seleziona_un_sottoinsieme():
    """Serve a calibrare la prima misura --live a pochi centesimi."""
    scelti = scenarios_by_name(["chat"], PROGETTO)
    assert [s.name for s in scelti] == ["chat"]
    assert sum(s.size for s in scelti) < sum(s.size for s in all_scenarios(PROGETTO))


def test_uno_scenario_inesistente_elenca_quelli_validi():
    with pytest.raises(ValueError) as errore:
        scenarios_by_name(["pippo"], PROGETTO)
    messaggio = str(errore.value)
    assert "chat" in messaggio and "costruzione" in messaggio


def test_senza_credenziali_una_misura_live_si_ferma_prima_di_spendere(monkeypatch):
    """L'SDK segnalerebbe il problema a meta' della prima richiesta, con una
    traccia di stack. Meglio fermarsi prima, con l'istruzione giusta."""
    import typer

    from ecotokens import cli

    class ClientSenzaCredenziali:
        api_key = None
        auth_token = None
        credentials = None

    class FintoModulo:
        AsyncAnthropic = staticmethod(lambda *a, **k: ClientSenzaCredenziali())

    monkeypatch.setitem(__import__("sys").modules, "anthropic", FintoModulo)
    with pytest.raises(typer.Exit) as uscita:
        cli.esigi_credenziali()
    assert uscita.value.exit_code == 2


def test_con_credenziali_la_misura_live_procede(monkeypatch):
    from ecotokens import cli

    class ClientConChiave:
        api_key = "sk-finta"
        auth_token = None
        credentials = None

    class FintoModulo:
        AsyncAnthropic = staticmethod(lambda *a, **k: ClientConChiave())

    monkeypatch.setitem(__import__("sys").modules, "anthropic", FintoModulo)
    cli.esigi_credenziali()  # non deve sollevare nulla
