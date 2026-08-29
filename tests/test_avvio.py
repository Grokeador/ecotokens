"""Quanto costa aprire, e quanto costa guardare.

Due proprieta' che nessun test copriva e che si erano rotte entrambe in
silenzio, perche' non producono un errore: producono un'attesa.

Il quadro si vende come "si apre subito" - e' scritto nella sua docstring e nel
README - e ci metteva **8,4 secondi**, perche' per leggere quattro nomi di
variante importava `bench`, che si tira dietro l'SDK Anthropic, il simulatore e
i carichi. Una pagina che dichiara di essere veloce e non lo e' e' una
contraddizione della stessa famiglia di quelle gia' corrette altrove: la
documentazione dice una cosa, il programma ne fa un'altra.

Le pagine di osservazione, poi, leggevano il registro intero a ogni
aggiornamento: su ventimila eventi `stage_activity` impiegava quasi un secondo,
e la console la chiama ogni cinque. La pagina che osserva diventava il carico
piu' pesante del gateway.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from ecotokens.store.db import Database
from ecotokens.store.repos import Store

from .conftest import sotto_strumentazione


def _importa(modulo: str) -> float:
    """Secondi impiegati a importare un modulo in un processo pulito.

    Un processo nuovo ogni volta: nello stesso interprete il secondo import
    e' gratis, ed e' esattamente il modo in cui una regressione del genere
    resta invisibile.
    """
    inizio = time.perf_counter()
    esito = subprocess.run(
        [sys.executable, "-c", f"import {modulo}"], capture_output=True, timeout=120
    )
    assert esito.returncode == 0, esito.stderr.decode(errors="replace")
    return time.perf_counter() - inizio


def test_il_quadro_non_si_tira_dietro_il_banco():
    """La prova strutturale, che non dipende dalla velocita' della macchina.

    Un tempo limite sarebbe fragile - passa o fallisce a seconda di quanto e'
    carico il computer - mentre "quali moduli sono stati caricati" e' un fatto.
    """
    esito = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json; import ecotokens.quadro; "
            "print(json.dumps([m for m in ('ecotokens.bench', 'anthropic', "
            "'ecotokens.simulator', 'ecotokens.workloads') if m in sys.modules]))",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert esito.returncode == 0, esito.stderr
    caricati = json.loads(esito.stdout.strip().splitlines()[-1])
    assert caricati == [], f"il quadro carica ancora: {caricati}"


def test_importare_il_pacchetto_non_legge_i_metadati():
    """La versione costa 265 ms e quasi nessun comando la usa.

    Farla pagare a chiunque importi il pacchetto era un peggioramento
    introdotto per una comodita' di scrittura: ora si legge alla prima
    richiesta.
    """
    esito = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, ecotokens; "
            "print('importlib.metadata' in sys.modules); "
            "assert ecotokens.__version__; "
            "print('importlib.metadata' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert esito.returncode == 0, esito.stderr
    prima, dopo = esito.stdout.strip().splitlines()[-2:]
    assert prima == "False", "i metadati vengono letti all'import"
    assert dopo == "True", "e alla prima richiesta invece si'"


@pytest.mark.parametrize("modulo", ["ecotokens", "ecotokens.varianti"])
def test_i_moduli_leggeri_restano_leggeri(modulo):
    """Un limite generoso: serve a cogliere un ordine di grandezza, non un ritardo."""
    assert _importa(modulo) < 3.0


# --- le pagine di osservazione non leggono tutto il registro ---------------


@pytest.fixture
async def registro_pieno():
    """Ventimila eventi: il registro di un servizio dopo qualche giorno."""
    database = Database(":memory:")
    database.connect()
    stadi = json.dumps(
        {
            "enabled": ["session", "cache_planner", "router", "ledger"],
            "acted": {"router": ["effort abbassato da high a low"]},
        }
    )
    await database.executemany(
        """INSERT INTO usage_events (session_id, ts, day, month, model, source,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
               cache_ttl, cost_usd, baseline_cost_usd, saved_usd, latency_ms, notes, stages)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                f"s{i % 400}", 1_780_000_000 + i, "2026-08-28", "2026-08",
                "claude-opus-5", "api", 1000, 100, 500, 900, "5m",
                0.01, 0.05, 0.04, 12.0, "[]", stadi,
            )
            for i in range(20_000)
        ],
    )
    yield Store(database)
    database.close()


async def test_i_rapporti_guardano_una_finestra_non_tutto(registro_pieno):
    """Duemila, non ventimila. La domanda delle pagine e' "cosa succede adesso"."""
    attivita = await registro_pieno.stage_activity()
    assert attivita[0]["requests_considered"] == 2_000

    scritture = await registro_pieno.cache_write_report()
    assert scritture["finestra"] == 2_000


async def test_la_finestra_e_dichiarata_nel_rapporto(registro_pieno):
    """Un conteggio su un sottoinsieme che si presenta come totale e' il solito
    numero plausibile e sbagliato."""
    scritture = await registro_pieno.cache_write_report()
    assert "finestra" in scritture
    assert scritture["finestra"] <= scritture["scritture"] + 1


async def test_le_pagine_di_osservazione_restano_sotto_il_mezzo_secondo(registro_pieno):
    """Insieme, non una alla volta: la console le chiama tutte ogni cinque
    secondi, e mentre girano tengono il lock del database - cioe' rallentano le
    richieste vere. Il limite e' generoso apposta, perche' quello che si vuole
    cogliere e' il ritorno del secondo pieno, non una fluttuazione.
    """
    # Prima chiamata a vuoto: gli import pigri dentro i metodi si pagano una
    # volta sola, e contarli qui misurerebbe l'avvio invece del lavoro.
    await registro_pieno.cache_write_report()

    inizio = time.perf_counter()
    await registro_pieno.stats()
    await registro_pieno.stage_activity()
    await registro_pieno.cache_write_report()
    await registro_pieno.latency_report()
    await registro_pieno.recent_events(25)
    trascorso = time.perf_counter() - inizio

    # Sotto strumentazione ogni chiamata Python costa piu' volte il suo prezzo,
    # e un limite a orologio misura lo strumento invece delle query. Il test
    # falliva solo con `coverage run` **e** la suite intera, mai in isolamento:
    # un verdetto che dipende da cosa ha girato prima non e' un verdetto.
    #
    # Qui si allarga invece di saltare - al contrario dei test di `test_traffico`
    # - perche' la proprieta' difesa e' grossolana: non tornare all'ordine dei
    # secondi resta verificabile anche con un limite quadruplicato.
    budget = 2.0 if sotto_strumentazione() else 0.5
    assert trascorso < budget, (
        f"le pagine di osservazione hanno impiegato {trascorso:.3f}s "
        f"(limite {budget}s)"
    )
