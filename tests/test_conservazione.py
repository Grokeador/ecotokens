"""Il registro dei consumi non deve crescere senza limite.

`usage_events` ha una riga per richiesta, con note e attribuzione per stadio.
Nessun comando la cancellava mai: `purge` toccava solo le cache. Su un servizio
con qualche migliaio di richieste al giorno il registro diventa il collo di
bottiglia, e le pagine che lo leggono rallentano con lui.

La tentazione e' cancellare i giorni vecchi. Sarebbe pero' una **pulizia che
falsifica**: i totali storici calerebbero a ogni passaggio, e il gateway
direbbe di aver risparmiato meno di quanto ha risparmiato. Da qui due tabelle -
il dettaglio recente e un riepilogo per giorno - e da qui questi test, che
guardano soprattutto la cosa che non deve cambiare.
"""

from __future__ import annotations

import pytest

from ecotokens.store.db import Database
from ecotokens.store.repos import Store


@pytest.fixture
async def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


async def registra(store, giorno: str, quante: int = 1, **valori) -> None:
    await store.db.executemany(
        """INSERT INTO usage_events (session_id, ts, day, month, model, source,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
               cost_usd, baseline_cost_usd, saved_usd, latency_ms, notes, stages)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "s1", 1.0 * i, giorno, giorno[:7],
                valori.get("model", "claude-opus-5"), valori.get("source", "api"),
                100, 10, 50, 90,
                valori.get("cost_usd", 0.01), valori.get("baseline_cost_usd", 0.05),
                valori.get("saved_usd", 0.04), 12.0, "[]", "",
            )
            for i in range(quante)
        ],
    )


# --- cio' che non deve cambiare -------------------------------------------


async def test_compattare_non_sposta_di_un_centesimo_i_totali(store):
    """La sola cosa che rende la compattazione accettabile.

    Se i totali calassero, questa non sarebbe una pulizia: sarebbe il gateway
    che dimentica di aver risparmiato, cioe' il difetto del metro che il
    progetto passa il tempo a correggere - stavolta introdotto di proposito.
    """
    await registra(store, "2020-01-01", 5)
    await registra(store, "2026-08-28", 3)
    prima = await store.stats()

    await store.compatta_consumi(keep_detail_days=30)
    dopo = await store.stats()

    for campo in ("requests", "input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_creation_tokens", "cost_usd", "baseline_cost_usd", "saved_usd"):
        assert prima[campo] == pytest.approx(dopo[campo]), campo


async def test_il_dettaglio_vecchio_sparisce_davvero(store):
    """Altrimenti non e' una compattazione, e' una copia."""
    await registra(store, "2020-01-01", 5)
    await registra(store, "2026-08-28", 3)

    esito = await store.compatta_consumi(keep_detail_days=30)
    assert esito["compattate"] == 5
    assert esito["giorni"] == 1

    rimaste = await store.db.query_one("SELECT COUNT(*) AS n FROM usage_events")
    assert rimaste["n"] == 3


async def test_la_spesa_corrente_vede_anche_i_riepiloghi(store):
    """Il tetto di spesa legge da qui: se non vedesse i giorni compattati,
    dopo una compattazione crederebbe di non aver ancora speso niente."""
    await registra(store, "2020-01-01", 4, cost_usd=0.25)
    await store.compatta_consumi(keep_detail_days=1)

    totale = await store.spend_since("month", "2020-01")
    assert totale == pytest.approx(1.0)


async def test_ripetere_la_compattazione_non_raddoppia_niente(store):
    """Due esecuzioni ravvicinate possono trovare lo stesso giorno."""
    await registra(store, "2020-01-01", 4)
    await store.compatta_consumi(keep_detail_days=30)
    prima = await store.stats()

    await registra(store, "2020-01-01", 2)
    await store.compatta_consumi(keep_detail_days=30)
    dopo = await store.stats()

    assert dopo["requests"] == prima["requests"] + 2


async def test_senza_niente_da_compattare_non_fa_niente(store):
    await registra(store, "2026-08-28", 3)
    esito = await store.compatta_consumi(keep_detail_days=30)
    assert esito["compattate"] == 0
    assert (await store.db.query_one("SELECT COUNT(*) AS n FROM usage_events"))["n"] == 3


async def test_i_totali_restano_separati_per_modello_e_origine(store):
    """Il riepilogo aggrega per giorno, non appiattisce tutto in una riga:
    "per modello" e "per origine" devono continuare a rispondere."""
    await registra(store, "2020-01-01", 2, model="claude-opus-5")
    await registra(store, "2020-01-01", 3, model="claude-haiku-4-5")
    await registra(store, "2020-01-01", 1, source="exact_cache")
    await store.compatta_consumi(keep_detail_days=30)

    dati = await store.stats()
    per_modello = {r["model"]: r["requests"] for r in dati["by_model"]}
    assert per_modello == {"claude-opus-5": 3, "claude-haiku-4-5": 3}
    per_origine = {r["source"]: r["requests"] for r in dati["by_source"]}
    assert per_origine == {"api": 5, "exact_cache": 1}


async def test_cio_che_si_perde_e_dichiarato(store):
    """Latenza, note e attribuzione per stadio non entrano nel riepilogo.

    Non e' una svista: sono le domande a cui, passati i giorni di dettaglio, il
    gateway non sa piu' rispondere. Il conteggio per stadio deve quindi
    ignorare i giorni compattati invece di inventarne uno a zero.
    """
    await registra(store, "2020-01-01", 5)
    await store.compatta_consumi(keep_detail_days=30)

    assert await store.stage_activity() == []
    assert await store.latency_report() == []
    # Ma i soldi ci sono ancora.
    assert (await store.stats())["requests"] == 5
