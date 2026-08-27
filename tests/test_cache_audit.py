"""Test del conto delle scritture in cache mai rilette.

Il modulo sotto esame ricostruisce un'informazione che l'API non fornisce:
*quale* scrittura una rilettura abbia ripagato. La ricostruzione poggia su una
sola proprieta' - la cache e' un match di prefisso, quindi le letture crescono
da sinistra - e i test qui sotto servono a fissarla, perche' se quella
proprieta' venisse letta male il numero risultante sembrerebbe comunque
plausibile. E' esattamente il modo in cui questo progetto ha gia' sbagliato
sette misure su quattordici.
"""

from __future__ import annotations

import pytest

from ecotokens.cache_audit import CacheEvent, audit_cache_writes, costo_scrittura_sprecata
from ecotokens.config import Settings
from ecotokens.store.db import Database
from ecotokens.store.repos import Store


def evento(sessione: str, letti: int, scritti: int, **extra) -> CacheEvent:
    return CacheEvent(session_id=sessione, read_tokens=letti, write_tokens=scritti, **extra)


# --- attribuzione ---------------------------------------------------------


def test_una_sessione_sana_ripaga_tutto_tranne_la_coda():
    """Il caso normale: ogni turno rilegge cio' che il turno prima ha scritto."""
    conto = audit_cache_writes(
        [
            evento("s", 0, 1000),  # scrive il prefisso di sistema
            evento("s", 1000, 300),  # lo rilegge, e allunga
            evento("s", 1300, 200),  # rilegge tutto, e allunga ancora
        ]
    )
    assert conto.token_recuperati == 1300
    assert conto.token_sprecati_in_mezzo == 0
    # L'ultima scrittura non ha un dopo: e' di coda, non un difetto.
    assert conto.token_sprecati_di_coda == 200


def test_una_scrittura_seguita_e_mai_riletta_e_in_mezzo():
    """Il caso che si vuole scoprire: il prefisso cambia e la scrittura resta orfana."""
    conto = audit_cache_writes(
        [
            evento("s", 0, 1000),
            # Il prefisso e' cambiato: si riparte da zero invece di rileggere.
            evento("s", 0, 800),
            evento("s", 800, 0),
        ]
    )
    assert conto.token_sprecati_in_mezzo == 1000
    assert conto.token_sprecati_di_coda == 0
    assert conto.quota_sprecata_in_mezzo == pytest.approx(1000 / 1800)


def test_la_coda_e_tenuta_separata_dallo_spreco_in_mezzo():
    """Non vanno sommate: solo una delle due dipende da una decisione del gateway."""
    conto = audit_cache_writes([evento("a", 0, 500), evento("b", 0, 500)])
    # Due sessioni di una richiesta ciascuna: entrambe le scritture sono di coda.
    assert conto.token_sprecati_di_coda == 1000
    assert conto.token_sprecati_in_mezzo == 0
    assert conto.sessioni == 2


def test_si_prende_la_rilettura_piu_favorevole_non_la_prima():
    """Il conto e' un limite inferiore allo spreco, e deve restare tale.

    Una voce puo' sopravvivere a un turno che non la tocca e venire ripresa
    dopo. Attribuire solo alla richiesta immediatamente successiva
    gonfierebbe lo spreco, cioe' farebbe sembrare il gateway peggiore di
    quanto sia - un errore nella direzione opposta a quella di solito temuta,
    ma pur sempre un errore.
    """
    conto = audit_cache_writes(
        [
            evento("s", 0, 1000),
            evento("s", 0, 0),  # un turno che non rilegge niente
            evento("s", 1000, 0),  # ma il successivo si'
        ]
    )
    assert conto.token_sprecati_in_mezzo == 0
    assert conto.token_recuperati == 1000


def test_le_sessioni_non_si_ripagano_a_vicenda():
    conto = audit_cache_writes([evento("a", 0, 1000), evento("b", 5000, 0)])
    assert conto.token_sprecati_di_coda == 1000
    assert conto.token_recuperati == 0


def test_una_rilettura_parziale_ripaga_solo_la_sua_parte():
    conto = audit_cache_writes([evento("s", 0, 1000), evento("s", 400, 0)])
    assert conto.token_recuperati == 400
    assert conto.token_sprecati_in_mezzo == 600


def test_le_richieste_senza_scrittura_non_contano_come_scritture():
    conto = audit_cache_writes([evento("s", 0, 0), evento("s", 0, 0)])
    assert conto.scritture == 0
    assert conto.quota_sprecata == 0.0


# --- il prezzo ------------------------------------------------------------


def test_il_costo_e_il_sovrapprezzo_non_il_prezzo_pieno():
    """Quei token si sarebbero pagati comunque: la perdita e' la differenza.

    A cinque minuti si e' pagato 1.25x invece di 1x, quindi si e' buttato lo
    0.25. Contare l'intero 1.25 direbbe che marcare la cache costa cinque
    volte quello che costa davvero.
    """
    pieno = 1_000_000 / 1_000_000 * 5.0  # un milione di token a $5/Mtok
    assert costo_scrittura_sprecata(1_000_000, "claude-opus-5", "5m") == pytest.approx(
        pieno * 0.25
    )
    # A un'ora la scrittura costa il doppio, quindi il buttato e' il prezzo pieno.
    assert costo_scrittura_sprecata(1_000_000, "claude-opus-5", "1h") == pytest.approx(pieno)


def test_il_ttl_lungo_pesa_quattro_volte_tanto():
    corto = costo_scrittura_sprecata(10_000, "claude-opus-5", "5m")
    lungo = costo_scrittura_sprecata(10_000, "claude-opus-5", "1h")
    assert lungo == pytest.approx(corto * 4)


# --- il ponte con i dati veri ---------------------------------------------


@pytest.fixture
async def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


async def test_il_conto_sui_dati_veri_legge_il_ttl_di_ogni_riga(store):
    """Il TTL sta per riga perche' cambia per riga: lo decide il pianificatore.

    Senza la colonna, il costo di una scrittura a un'ora verrebbe calcolato
    come se fosse a cinque minuti, cioe' un quarto del vero.
    """
    from ecotokens.pricing import Usage

    async def registra(sessione: str, letti: int, scritti: int, ttl: str) -> None:
        await store.record_usage(
            session_id=sessione,
            model="claude-opus-5",
            source="api",
            usage=Usage(
                input_tokens=0,
                output_tokens=10,
                cache_creation_tokens=scritti,
                cache_read_tokens=letti,
            ),
            cost_usd=0.0,
            baseline_cost_usd=0.0,
            saved_usd=0.0,
            cache_ttl=ttl,
        )

    await registra("s", 0, 10_000, "1h")

    conto = await store.cache_write_report()
    assert conto["token_sprecati_di_coda"] == 10_000
    atteso = costo_scrittura_sprecata(10_000, "claude-opus-5", "1h")
    assert conto["costo_sprecato_usd"] == pytest.approx(atteso)


async def test_gli_hit_di_cache_non_entrano_nel_conto(store):
    """Non raggiungono l'API: contarli allungherebbe le sessioni a vuoto."""
    from ecotokens.pricing import Usage

    vuoto = Usage(input_tokens=0, output_tokens=0, cache_creation_tokens=0, cache_read_tokens=0)
    await store.record_usage(
        session_id="s",
        model="claude-opus-5",
        source="exact_cache",
        usage=vuoto,
        cost_usd=0.0,
        baseline_cost_usd=0.0,
        saved_usd=0.1,
    )
    conto = await store.cache_write_report()
    assert conto["sessioni"] == 0


async def test_le_richieste_senza_sessione_non_vengono_incatenate(store):
    """Senza sessione non si sa se continuino qualcosa: ognuna sta per se'."""
    from ecotokens.pricing import Usage

    for _ in range(3):
        await store.record_usage(
            session_id=None,
            model="claude-opus-5",
            source="api",
            usage=Usage(
                input_tokens=0, output_tokens=1, cache_creation_tokens=500, cache_read_tokens=0
            ),
            cost_usd=0.0,
            baseline_cost_usd=0.0,
            saved_usd=0.0,
        )
    conto = await store.cache_write_report()
    assert conto["sessioni"] == 3
    assert conto["token_sprecati_di_coda"] == 1500
    assert conto["token_sprecati_in_mezzo"] == 0


def test_la_colonna_ttl_viene_aggiunta_a_un_database_gia_esistente(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` non tocca una tabella che c'e' gia'.

    Senza migrazione, le query nuove fallirebbero proprio sui database pieni
    di storia, cioe' gli unici su cui questa misura abbia qualcosa da dire.
    """
    import sqlite3

    percorso = tmp_path / "vecchio.db"
    vecchio = sqlite3.connect(percorso)
    vecchio.execute(
        """CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL NOT NULL,
            day TEXT NOT NULL, month TEXT NOT NULL, model TEXT NOT NULL,
            source TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0, baseline_cost_usd REAL NOT NULL DEFAULT 0,
            saved_usd REAL NOT NULL DEFAULT 0, latency_ms REAL, notes TEXT)"""
    )
    vecchio.execute(
        "INSERT INTO usage_events (ts, day, month, model, source) VALUES (1, 'x', 'y', 'm', 'api')"
    )
    vecchio.commit()
    vecchio.close()

    database = Database(percorso)
    database.connect()
    try:
        colonne = {riga[1] for riga in database.conn.execute("PRAGMA table_info(usage_events)")}
        assert "cache_ttl" in colonne
        # La riga preesistente sopravvive e prende il default.
        riga = database.conn.execute("SELECT cache_ttl FROM usage_events").fetchone()
        assert riga[0] == "5m"
    finally:
        database.close()


# --- il tetto dei breakpoint ----------------------------------------------


async def test_il_tetto_dei_breakpoint_vale_anche_per_il_primo():
    """`max_breakpoints` veniva ignorato dal marker su system+tools.

    Non e' un valore che si usi in produzione, ma una configurazione che il
    codice ignora in silenzio e' peggio di una che rifiuta: la misura che la
    adotta crede di aver provato una cosa e ne ha provata un'altra.
    """
    from ecotokens.api.schemas import ChatCompletionRequest
    from ecotokens.pipeline.base import RequestContext
    from ecotokens.pipeline.cache_planner import CachePlannerStage
    from ecotokens.translate.to_anthropic import build_anthropic_params

    settings = Settings()
    settings.cache_planner.max_breakpoints = 0
    request = ChatCompletionRequest.model_validate(
        {
            "model": "claude-opus-5",
            "messages": [
                {"role": "system", "content": "istruzioni dettagliate " * 400},
                {"role": "user", "content": "ciao"},
            ],
        }
    )
    traduzione = build_anthropic_params(request, settings)
    ctx = RequestContext(
        request=request,
        settings=settings,
        store=None,
        client=None,
        counter=None,
        completion_id="test",
        model=traduzione.model,
        params=traduzione.params,
        stream=False,
    )
    await CachePlannerStage(settings).before(ctx)

    marcati = sum(
        1 for blocco in (ctx.params.get("system") or []) if "cache_control" in blocco
    )
    assert marcati == 0


# --- la regressione che ha motivato la misura -----------------------------


async def test_il_confine_di_potatura_lascia_scritture_orfane_se_avanza_spesso():
    """La ragione per cui questo modulo esiste.

    Il pianificatore, da solo, non lascia orfana nessuna scrittura a meta'
    sessione: scrive il prefisso e il turno dopo lo rilegge. Ma la potatura
    sposta il confine, e appena il confine si sposta il prefisso cambia -
    quindi cio' che era stato appena scritto non lo rileggera' piu' nessuno.

    E' la stessa lezione gia' imparata tre volte in questo progetto (un confine
    che insegue la coda distrugge la cache), vista pero' da un lato nuovo: non
    "la cache non trova", ma "si e' pagato 1,25x per scrivere qualcosa che
    nessuno leggera'". Il test fissa il verso della relazione, non i valori:
    quelli dipendono dal corpus.
    """
    from ecotokens.bench import _abilita_contesto, _run_scenario, make_settings
    from ecotokens.cache_audit import audit_cache_writes
    from ecotokens.workloads import scenario_agente

    async def orfani(passo: int) -> int:
        def config(settings):
            _abilita_contesto(settings)
            settings.context.prune_step_turns = passo

        eventi = []
        await _run_scenario(
            scenario_agente(turns=14, tool_per_turno=6),
            make_settings(config),
            f"passo-{passo}",
            live=False,
            raccolta=eventi,
        )
        return audit_cache_writes(eventi).token_sprecati_in_mezzo

    fitto = await orfani(2)
    rado = await orfani(12)
    assert fitto > rado, (
        "un confine che avanza spesso deve lasciare piu' scritture orfane: "
        f"passo 2 ne lascia {fitto}, passo 12 ne lascia {rado}"
    )
