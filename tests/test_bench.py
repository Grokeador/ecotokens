"""Test del banco di misura e della dashboard.

Il banco produce i numeri su cui si prendono decisioni di configurazione: se
misura male, si ottimizza nella direzione sbagliata con la massima fiducia. Qui
si vincolano le proprieta' che rendono il confronto onesto.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecotokens.bench import (
    guadagno_sul_caching_automatico,
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
        # Il primo gradino non e' del gateway: e' il caching automatico che
        # Anthropic offre con un campo. Sta nella scala proprio per questo -
        # senza, i 68 punti che regala sembrerebbero merito del pianificatore.
        "caching automatico",
        "pianificatore EcoTokens",
        "potatura contesto",
        "cache esatta",
        "effort adattivo",
        "riscrittura prompt",
        # Le ultime due scambiano fedelta' contro spesa, e stanno in fondo
        # apposta: separate, e non fuse in un gradino solo, perche' la riga
        # "modello economico" vale piu' di tutte le altre messe insieme e
        # nasconderla dentro un'altra falserebbe l'attribuzione.
        "effort sempre basso",
        "modello economico",
    ]
    # Il cumulato dell'ultimo gradino e' il risparmio totale della catena.
    riferimento = run.totals(BASELINE_VARIANT).cost_usd
    completo = run.totals(ABLATION_STEPS[-1][0]).cost_usd
    assert contributi[-1]["cumulative_usd"] == pytest.approx(riferimento - completo)


async def test_il_caching_automatico_domina_e_non_e_del_gateway():
    """La correzione piu' grossa mai fatta a questo progetto, fissata da un test.

    Per un anno lo stadio dominante era 'prompt caching' e veniva contato come
    merito del gateway. Da quando Anthropic offre il caching automatico - un
    solo campo in cima alla richiesta - quel gradino lo ottiene chiunque senza
    gateway, e il pianificatore di EcoTokens vale cio' che aggiunge *sopra*.

    Il test fissa entrambe le meta' della frase: che il primo gradino domini, e
    che il secondo sia molto piu' piccolo. Se un giorno si invertissero sarebbe
    una notizia, e va scoperta da qui.
    """
    run = await run_ablation(scenarios=[scenario_chat(turns=5)], label="test")
    contributi = {c["stage"]: c["saved_usd"] for c in stage_contributions(run)}

    assert contributi["caching automatico"] == max(contributi.values())
    assert contributi["pianificatore EcoTokens"] < contributi["caching automatico"] / 10


async def test_il_pianificatore_manuale_rende_a_prefisso_condiviso():
    """Dove il pianificatore serve davvero, e perche'.

    Il caching automatico piazza il breakpoint sull'ultimo blocco, cioe' dopo
    la domanda: la voce che crea non e' riutilizzabile da una domanda diversa.
    Un breakpoint su system+tools ne crea una che tutte le richieste
    successive rileggono. Su un carico di domande distinte che condividono il
    prompt di sistema la differenza e' strutturale, non marginale.
    """
    from ecotokens.bench import _abilita_cache_automatica, _abilita_cache_planner
    from ecotokens.workloads import scenario_ripetitivo

    scenario = scenario_ripetitivo(uniche=4, ripetizioni=2)
    automatico = await _run_scenario(
        scenario, make_settings(_abilita_cache_automatica), "auto", live=False
    )
    manuale = await _run_scenario(
        scenario, make_settings(_abilita_cache_planner), "manuale", live=False
    )
    assert manuale.cost_usd < automatico.cost_usd


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
        {"variant": "+ caching automatico", "cost_usd": 5.0},
        {"variant": "+ pianificatore EcoTokens", "cost_usd": 4.0},
        {"variant": "+ potatura contesto", "cost_usd": 4.0},
        {"variant": "+ cache esatta", "cost_usd": 3.0},
        {"variant": "+ effort adattivo", "cost_usd": 2.5},
        {"variant": "+ riscrittura prompt", "cost_usd": 2.4},
    ]
    contributi = stage_contributions_from_results(righe)
    per_nome = {c["stage"]: c for c in contributi}

    assert per_nome["caching automatico"]["saved_ratio"] == pytest.approx(0.5)
    # Il pianificatore vale cio' che aggiunge SOPRA il caching automatico, non
    # il totale: e' il punto di tutto il gradino nuovo.
    assert per_nome["pianificatore EcoTokens"]["saved_ratio"] == pytest.approx(0.1)
    assert per_nome["potatura contesto"]["saved_ratio"] == pytest.approx(0.0)
    assert per_nome["riscrittura prompt"]["cumulative_ratio"] == pytest.approx(0.76)


def test_una_misura_di_una_versione_piu_vecchia_si_ferma_al_gradino_mancante():
    """Non si inventa uno zero: i gradini sono cumulativi e sarebbero falsati."""
    righe = [
        {"variant": BASELINE_VARIANT, "cost_usd": 10.0},
        {"variant": "+ caching automatico", "cost_usd": 5.0},
        {"variant": "+ pianificatore EcoTokens", "cost_usd": 4.0},
        # gradini successivi assenti: misura di prima che esistessero
    ]
    contributi = stage_contributions_from_results(righe)
    assert [c["stage"] for c in contributi] == [
        "caching automatico",
        "pianificatore EcoTokens",
    ]


async def test_il_confronto_fra_versioni_riconosce_i_miglioramenti():
    database, store = open_results_store(":memory:")
    try:
        vecchia = BenchRun(id="v1", label="prima", mode="simulato", created_at=1.0)
        vecchia.measurements = [
            Measurement(scenario="x", variant=BASELINE_VARIANT, cost_usd=10.0),
            Measurement(scenario="x", variant="+ caching automatico", cost_usd=6.0),
            Measurement(scenario="x", variant="+ pianificatore EcoTokens", cost_usd=5.0),
            Measurement(scenario="x", variant="+ potatura contesto", cost_usd=5.0),
            Measurement(scenario="x", variant="+ cache esatta", cost_usd=4.5),
            Measurement(scenario="x", variant="+ effort adattivo", cost_usd=4.5),
            Measurement(scenario="x", variant="+ riscrittura prompt", cost_usd=4.5),
        ]
        await save_run(store, vecchia, corpus="prova")

        nuova = BenchRun(id="v2", label="dopo", mode="simulato", created_at=2.0)
        nuova.measurements = [
            Measurement(scenario="x", variant=BASELINE_VARIANT, cost_usd=10.0),
            Measurement(scenario="x", variant="+ caching automatico", cost_usd=6.0),
            Measurement(scenario="x", variant="+ pianificatore EcoTokens", cost_usd=4.0),
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
        assert stati["pianificatore EcoTokens"] == "migliorato"
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


# --- impronta del corpus ---------------------------------------------------


def test_l_impronta_cambia_se_cambia_il_contenuto_non_solo_l_elenco():
    """La domanda a cui `CORPUS_VERSION` non sa rispondere.

    Due corpus possono avere gli stessi scenari e dire cose diverse: lo
    scenario `costruzione` legge i sorgenti veri del progetto, quindi cresce
    con il codice. Senza impronta, la sezione dei progressi confronterebbe
    misure incomparabili senza accorgersene.
    """
    from ecotokens.workloads import Scenario, corpus_fingerprint

    def corpus(testo: str) -> list[Scenario]:
        return [
            Scenario(
                name="x",
                description="",
                requests=[{"model": "m", "messages": [{"role": "user", "content": testo}]}],
            )
        ]

    assert corpus_fingerprint(corpus("a")) == corpus_fingerprint(corpus("a"))
    assert corpus_fingerprint(corpus("a")) != corpus_fingerprint(corpus("b"))


def test_l_impronta_non_dipende_dall_ordine_delle_chiavi():
    """L'ordine delle chiavi di un dizionario non e' parte del carico.

    Senza `sort_keys` l'impronta cambierebbe da sola fra due esecuzioni che
    hanno misurato la stessa identica cosa, e ogni confronto risulterebbe
    contaminato: un avviso che scatta sempre non e' un avviso.
    """
    from ecotokens.workloads import Scenario, corpus_fingerprint

    uno = [Scenario(name="x", description="", requests=[{"model": "m", "stream": False}])]
    due = [Scenario(name="x", description="", requests=[{"stream": False, "model": "m"}])]
    assert corpus_fingerprint(uno) == corpus_fingerprint(due)


async def test_l_ablazione_registra_l_impronta_del_corpus():
    from ecotokens.bench import run_ablation
    from ecotokens.workloads import all_scenarios, corpus_fingerprint

    scenari = [scenario_chat(turns=2)]
    run = await run_ablation(scenarios=scenari, label="test")
    assert run.fingerprint == corpus_fingerprint(scenari)
    assert run.fingerprint != corpus_fingerprint(all_scenarios())


async def test_i_progressi_segnalano_un_confronto_fra_corpus_diversi():
    """L'impronta serve a questo, e solo a questo: rendere visibile la deriva.

    Il confronto non viene soppresso - nasconderlo sarebbe peggio - ma marcato,
    perche' parte del delta e' crescita del metro e non merito del gateway.
    """
    from ecotokens.bench import (
        open_results_store,
        run_ablation,
        save_run,
        stage_contributions,
        stage_progress,
    )

    database, store = open_results_store(":memory:")
    try:
        etichetta = "prova"
        prima = await run_ablation(scenarios=[scenario_chat(turns=2)], label="prima")
        await save_run(store, prima, corpus=etichetta)

        # Stessa forma, contenuto diverso: e' esattamente cio' che succede
        # quando `costruzione` rilegge sorgenti cresciuti.
        dopo = await run_ablation(scenarios=[scenario_chat(turns=3)], label="dopo")
        assert dopo.fingerprint != prima.fingerprint
        await save_run(store, dopo, corpus=etichetta)

        progresso = await stage_progress(
            store, stage_contributions(dopo), corpus=etichetta
        )
        assert progresso["available"] is True
        assert progresso["comparable"] is False
        assert progresso["stages"], "il confronto va mostrato, non soppresso"

        # E due misure sullo stesso identico carico devono risultare confrontabili.
        terza = await run_ablation(scenarios=[scenario_chat(turns=3)], label="terza")
        await save_run(store, terza, corpus=etichetta)
        progresso = await stage_progress(
            store, stage_contributions(terza), corpus=etichetta
        )
        assert progresso["comparable"] is True
    finally:
        database.close()

# --- il numero che serve a chi deve decidere se installarlo ---------------


def _corsa_finta() -> "BenchRun":
    """Una scala di ablazione con numeri scelti a mano, per fare l'aritmetica.

    Due scenari con comportamenti opposti: uno che guadagna molto dal gateway
    e uno che non guadagna quasi niente. E' il caso che conta, perche' e' la
    forbice - non la media - a dire a un utente se il progetto serve a lui.
    """
    from ecotokens.bench import BenchRun, Measurement

    run = BenchRun(id="x", label="prova", mode="simulato", created_at=1.0)
    misure = []
    # ripetitivo: da 10 a 2 (80% in meno). singolo: da 10 a 9 (10% in meno).
    costi = {
        "senza-gateway": (40.0, 40.0),
        "+ caching automatico": (10.0, 10.0),
        "+ pianificatore EcoTokens": (8.0, 9.5),
        "+ potatura contesto": (6.0, 9.5),
        "+ cache esatta": (3.0, 9.0),
        "+ effort adattivo": (2.5, 9.0),
        "+ riscrittura prompt": (2.0, 9.0),
        "+ effort sempre basso": (1.5, 8.0),
        "+ modello economico": (0.5, 3.0),
    }
    for variante, (ripetitivo, singolo) in costi.items():
        misure.append(Measurement(scenario="ripetitivo", variant=variante, cost_usd=ripetitivo))
        misure.append(Measurement(scenario="singolo", variant=variante, cost_usd=singolo))
    run.measurements = misure
    return run


def test_il_guadagno_si_misura_contro_il_caching_automatico_non_contro_il_nulla():
    """Il riferimento del totale dell'ablazione non e' il punto di partenza di nessuno.

    "Senza gateway" vuol dire senza cache, e ottenere la cache oggi costa una
    riga: Anthropic la offre a chiunque. Confrontarsi con quel nulla attribuisce
    al gateway un merito che non e' suo, e prepara una delusione a chi legge la
    percentuale e installa.
    """
    guadagno = guadagno_sul_caching_automatico(_corsa_finta())

    # Riferimento: 10 + 10, non 40 + 40.
    assert guadagno["reference_usd"] == 20.0
    # Senza toccare le risposte: 2 + 9 = 11, cioe' il 45% in meno di 20.
    senza = guadagno["senza_cambiare_la_risposta"]
    assert senza["cost_usd"] == 11.0
    assert senza["saved_ratio"] == pytest.approx(0.45)
    # Accendendo cio' che cambia il contenuto: 0,5 + 3 = 3,5.
    assert guadagno["cambiando_la_risposta"]["cost_usd"] == 3.5
    assert guadagno["cambiando_la_risposta"]["saved_ratio"] == pytest.approx(0.825)


def test_la_forbice_fra_i_carichi_resta_visibile():
    """E' l'informazione piu' utile del progetto, e la media la cancella.

    Un utente non ha "il carico medio": ne ha uno solo. Sapere che si va dal
    10% all'80% a seconda della forma del traffico gli dice se il gateway serve
    al suo caso; sapere che la media e' 45% non gli dice niente.
    """
    per_scenario = guadagno_sul_caching_automatico(_corsa_finta())["by_scenario"]

    assert [v["scenario"] for v in per_scenario] == ["ripetitivo", "singolo"], (
        "ordinati dal guadagno piu' grande al piu' piccolo"
    )
    assert per_scenario[0]["saved_ratio"] == pytest.approx(0.80)
    assert per_scenario[1]["saved_ratio"] == pytest.approx(0.10)


def test_i_totali_si_possono_leggere_per_un_solo_scenario():
    from ecotokens.bench import BenchRun, Measurement

    run = BenchRun(id="x", label="p", mode="simulato", created_at=1.0)
    run.measurements = [
        Measurement(scenario="a", variant="v", cost_usd=1.0, requests=2),
        Measurement(scenario="b", variant="v", cost_usd=3.0, requests=5),
    ]
    assert run.totals("v").cost_usd == 4.0
    assert run.totals("v", scenario="a").cost_usd == 1.0
    assert run.totals("v", scenario="a").requests == 2

def test_la_dashboard_mostra_il_confronto_col_caching_automatico():
    """Chi apre la pagina per decidere deve trovare il riferimento giusto.

    Il totale della scala di ablazione parte da "nessuna cache", che serve ad
    attribuire un merito a ogni stadio ma non e' il punto di partenza di
    nessuno. Senza questo pannello la pagina mostrerebbe solo il 95%, e chi lo
    legge installerebbe aspettandosi di dividere la bolletta per venti.
    """
    from ecotokens.dashboard import _vs_automatico

    dati = {
        "vs_automatico": {
            "reference_usd": 20.0,
            "senza_cambiare_la_risposta": {"cost_usd": 11.0, "saved_ratio": 0.45},
            "cambiando_la_risposta": {"cost_usd": 3.5, "saved_ratio": 0.825},
            "by_scenario": [
                {"scenario": "ripetitivo", "reference_usd": 10.0,
                 "cost_usd": 2.0, "saved_ratio": 0.80},
                {"scenario": "singolo", "reference_usd": 10.0,
                 "cost_usd": 9.0, "saved_ratio": 0.10},
            ],
        }
    }
    pannello = _vs_automatico(dati)
    assert "caching automatico" in pannello
    assert "45.0%" in pannello, "il totale senza toccare le risposte"
    assert "80.0%" in pannello and "10.0%" in pannello, "la forbice fra i carichi"
    # E l'avvertenza sul resto, che non e' risparmio a parita' di risposta.
    assert "82.5%" in pannello
    # A capo nel sorgente: si confronta il testo senza gli spazi bianchi.
    assert "un'altra risposta a un prezzo diverso" in " ".join(pannello.split())


def test_senza_ablazione_il_pannello_non_compare():
    """Meglio assente che con degli zeri: uno zero e' una misura, il vuoto no."""
    from ecotokens.dashboard import _vs_automatico

    assert _vs_automatico({}) == ""
    assert _vs_automatico({"vs_automatico": {"reference_usd": 0.0}}) == ""


async def test_la_dashboard_servita_mostra_il_confronto_senza_rimisurare(tmp_path):
    """Il pannello c'era solo nella pagina generata a mano, non in quella servita.

    `/admin/dashboard` risponde con `measure=false`, perche' rifare il banco a
    ogni apertura la renderebbe lenta - e in quel ramo il confronto col caching
    automatico non veniva mai calcolato. Cioe' mancava proprio nella pagina che
    la gente apre davvero. Ricostruito dall'ultima ablazione registrata.
    """
    from ecotokens.bench import ABLATION_STEPS, BASELINE_VARIANT, RIFERIMENTO_MODERNO

    settings = Settings()
    # Un file, non `:memory:`: ogni connessione in memoria e' un database a
    # se', quindi cio' che scrive il test non lo vedrebbe chi legge dopo.
    settings.storage.path = str(tmp_path / "misure.sqlite3")
    database, store = open_results_store(settings.storage.path)
    try:
        corsa = BenchRun(id="a1", label="ablazione", mode="simulato", created_at=1.0)
        costi = {BASELINE_VARIANT: 40.0, RIFERIMENTO_MODERNO: 10.0}
        for nome, _ in ABLATION_STEPS[2:]:
            costi[nome] = 5.0
        corsa.measurements = [
            Measurement(scenario="x", variant=variante, cost_usd=costo)
            for variante, costo in costi.items()
        ]
        await save_run(store, corsa, corpus="ablazione v2")
    finally:
        database.close()

    dati = await build_dashboard_data(settings, measure=False, project_root=PROGETTO)
    assert dati["vs_automatico"]["reference_usd"] == 10.0
    assert "Quanto aggiunge a chi usa" in render_dashboard(dati)


async def test_lo_streaming_risparmia_quanto_il_resto():
    """Zero richieste su cinquantuno del corpus erano in streaming.

    Il percorso vive nella rotta HTTP e non in `Gateway.complete`, quindi il
    banco non poteva raggiungerlo: il risparmio pubblicato descriveva la meta'
    del traffico reale, perche' la maggior parte delle interfacce di chat
    trasmette. Misurato, i due percorsi coincidono - ma andava misurato.
    """
    from ecotokens.bench import measure_streaming

    intero, a_pezzi = await measure_streaming("chat")

    assert intero.requests == a_pezzi.requests > 0
    assert a_pezzi.cache_read_tokens == intero.cache_read_tokens, (
        "la cache deve funzionare uguale nei due percorsi"
    )
    scarto = abs(a_pezzi.cost_usd - intero.cost_usd) / intero.cost_usd
    assert scarto < 0.01, f"i due percorsi divergono del {scarto:.1%}"
