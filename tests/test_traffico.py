"""Quanto lavoro riesce a fare il gateway, e chi glielo impedisce.

Nessuno l'aveva mai misurato. Il README punta ora esplicitamente al caso
"molte richieste diverse sopra lo stesso prefisso", che e' per definizione
multiutente: il soffitto di traffico e' il muro contro cui va chi lo installa,
e non sapere dov'e' significa scoprirlo dal cliente.

Il numero misurato e' quello del **solo gateway**, con l'upstream istantaneo.
In produzione l'attesa dell'API e' di centinaia di millisecondi e nasconde
tutto, finche' il carico non cresce abbastanza da far emergere questa parte.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ecotokens.store.db import Database


def _sotto_strumentazione() -> bool:
    """Vero se coverage sta contando le righe mentre il test gira.

    Un test di velocita' eseguito sotto lo strumento misura anche lo strumento:
    la copertura moltiplica i tempi per tre o quattro, e il test fallisce
    dicendo una cosa falsa sul gateway. Saltarlo la' e' l'unica risposta
    onesta - abbassare la soglia fino a farlo passare lo renderebbe incapace
    di cogliere una regressione vera.
    """
    import sys

    return "coverage" in sys.modules and sys.gettrace() is not None


# --- il trasporto verso il database ---------------------------------------


@pytest.mark.skipif(
    _sotto_strumentazione(), reason="misura la velocita': coverage la falserebbe"
)
async def test_una_query_breve_non_paga_un_salto_fra_thread():
    """Misurato: 6,9 us dentro SQLite, 448 attraverso `asyncio.to_thread`.

    Il trasporto valeva 65 volte il lavoro, e con otto operazioni per richiesta
    erano 3,5 ms di soli salti. Bloccare il loop per qualche microsecondo costa
    meno dello scheduling che si voleva evitare.
    """
    database = Database(":memory:")
    database.connect()
    try:
        giri = 300
        inizio = time.perf_counter()
        for _ in range(giri):
            await database.query("SELECT 1")
        per_query = (time.perf_counter() - inizio) / giri * 1e6
        assert per_query < 150, f"{per_query:.0f} us: il salto fra thread e' tornato"
    finally:
        database.close()


async def test_una_query_pesante_resta_su_un_thread():
    """Altrimenti la console, che si aggiorna ogni cinque secondi, fermerebbe
    il loop mentre legge migliaia di righe - cioe' fermerebbe le richieste vere.

    Si verifica che il loop resti libero: un compito che conta mentre la query
    gira deve riuscire ad avanzare.
    """
    database = Database(":memory:")
    database.connect()
    try:
        await database.executemany(
            "INSERT INTO usage_events (session_id, ts, day, month, model, source) "
            "VALUES (?,?,?,?,?,?)",
            [(f"s{i}", 1.0 * i, "2026-08-28", "2026-08", "m", "api") for i in range(30_000)],
        )

        avanzamenti = 0

        async def conta():
            nonlocal avanzamenti
            while True:
                avanzamenti += 1
                await asyncio.sleep(0)

        compito = asyncio.create_task(conta())
        await asyncio.sleep(0)
        await database.query(
            "SELECT session_id, ts FROM usage_events ORDER BY ts", pesante=True
        )
        compito.cancel()
        assert avanzamenti > 1, "il loop e' rimasto fermo durante la query pesante"
    finally:
        database.close()


# --- il soffitto del gateway ----------------------------------------------


@pytest.mark.skipif(
    _sotto_strumentazione(), reason="misura la velocita': coverage la falserebbe"
)
@pytest.mark.parametrize("richieste", [30])
async def test_il_gateway_regge_almeno_cinquanta_richieste_al_secondo(richieste):
    """Un limite generoso: serve a cogliere un crollo, non una fluttuazione.

    Misurato a 96 al secondo dopo aver tolto i salti fra thread, 63 prima, e 93
    dopo le aggiunte al percorso caldo - salvataggio dei parametri per stadio,
    memoria dei prefissi, query del tasso di continuazione. La differenza fra
    93 e 96 sta dentro il rumore dello strumento, che su questa macchina e' di
    circa il 3%, e l'A/B sulla sola query adattiva l'ha data addirittura di
    segno opposto: e' rumore, non un guadagno.

    La soglia sta molto sotto per non diventare un test che fallisce quando la
    macchina e' occupata - cio' che deve cogliere e' il ritorno a un ordine di
    grandezza diverso.
    """
    import anthropic
    import httpx2

    from ecotokens.api.schemas import ChatCompletionRequest
    from ecotokens.bench import _abilita_prompt, make_settings
    from ecotokens.server import Gateway
    from ecotokens.simulator import create_stub

    settings = make_settings(_abilita_prompt)
    settings.storage.path = ":memory:"
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

    def richiesta(indice: int) -> ChatCompletionRequest:
        return ChatCompletionRequest.model_validate(
            {
                "model": "claude-opus-5",
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": "Assistente. " * 150},
                    {"role": "user", "content": f"domanda {indice}"},
                ],
            }
        )

    try:
        # A caldo: la prima richiesta paga import e schema, e misurarla
        # significherebbe misurare l'avvio invece del lavoro. E' l'errore in cui
        # questo progetto e' gia' caduto due volte confrontando una serie fredda
        # con una calda.
        for indice in range(5):
            await gateway.complete(richiesta(900 + indice))

        inizio = time.perf_counter()
        for indice in range(richieste):
            await gateway.complete(richiesta(indice))
        al_secondo = richieste / (time.perf_counter() - inizio)
    finally:
        await gateway.shutdown()

    assert al_secondo > 50, f"{al_secondo:.0f} richieste al secondo"
