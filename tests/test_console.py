"""Test della console dal vivo e della misura che l'ha resa possibile.

La console non e' una pagina in piu': e' il primo posto in cui il progetto
guarda il **traffico vero** con lo stesso rigore con cui guardava il banco. Il
grosso di questo file prova la misura, non il disegno, e per una ragione che
si e' vista subito: appena la console ha mostrato il risparmio dal vivo, il
numero era sbagliato, e lo era da sempre.

Due gruppi.

* L'**attribuzione**: quale stadio ha fatto cosa. La pipeline la ricava
  osservando quali note sono comparse mentre uno stadio girava, invece di
  chiedere agli stadi di dichiararlo. Uno stadio che smette di fare qualcosa
  smette di essere contato, senza che nessuno debba ricordarsene.
* La **baseline**: contro cosa si confronta il costo. Era prezzata sul modello
  che il router aveva scelto, non su quello che il client aveva chiesto - e
  quindi il declassamento, che sul banco vale il 17%, dal vivo valeva zero.
"""

from __future__ import annotations

import json

import pytest

from ecotokens.config import Settings
from ecotokens.console import _avvisi, _conta_note, build_console_data
from ecotokens.pipeline.base import BaseStage, Pipeline, RequestContext
from ecotokens.pricing import Usage
from ecotokens.store.db import Database
from ecotokens.store.repos import Store

from .conftest import chat_payload


# --- attribuzione ----------------------------------------------------------


class StadioParlante(BaseStage):
    def __init__(self, nome: str, note: list[str], acceso: bool = True) -> None:
        self.name = nome
        self.enabled = acceso
        self._note = note

    async def before(self, ctx: RequestContext) -> None:
        for nota in self._note:
            ctx.note(nota)


def contesto_nudo(settings: Settings) -> RequestContext:
    return RequestContext(
        request=None,
        settings=settings,
        store=None,
        client=None,
        counter=None,
        completion_id="test",
        model="claude-opus-5",
        params={"model": "claude-opus-5", "messages": []},
        stream=False,
    )


async def test_ogni_nota_finisce_allo_stadio_che_l_ha_scritta():
    settings = Settings(profilo="prudente")
    ctx = contesto_nudo(settings)
    await Pipeline(
        [
            StadioParlante("primo", ["ha fatto A", "ha fatto B"]),
            StadioParlante("secondo", ["ha fatto C"]),
        ]
    ).before(ctx)

    assert ctx.stage_notes == {
        "primo": ["ha fatto A", "ha fatto B"],
        "secondo": ["ha fatto C"],
    }


async def test_una_nota_nata_fuori_dalla_pipeline_non_ha_padre():
    """Le note della traduzione esistono prima che qualunque stadio giri.

    Attribuirle a uno stadio - al primo che capita, tipicamente - farebbe
    contare come intervento qualcosa che quello stadio non ha fatto, e il
    conteggio per stadio e' il numero su cui si decide dove lavorare.
    """
    settings = Settings(profilo="prudente")
    ctx = contesto_nudo(settings)
    ctx.note("prefill assistant finale rimosso")

    await Pipeline([StadioParlante("primo", ["ha fatto A"])]).before(ctx)

    assert ctx.stage_notes == {"primo": ["ha fatto A"]}
    assert "prefill assistant finale rimosso" in ctx.notes


async def test_uno_stadio_che_tace_non_risulta_intervenuto():
    settings = Settings(profilo="prudente")
    ctx = contesto_nudo(settings)
    await Pipeline([StadioParlante("muto", [])]).before(ctx)

    assert ctx.stage_notes == {}
    assert ctx.stages_enabled == ["muto"], "ma resta nel denominatore"


async def test_uno_stadio_spento_non_entra_nel_denominatore():
    """Altrimenti "non e' mai intervenuto" e "non era acceso" darebbero lo stesso zero."""
    settings = Settings(profilo="prudente")
    ctx = contesto_nudo(settings)
    await Pipeline(
        [StadioParlante("acceso", ["x"]), StadioParlante("spento", ["y"], acceso=False)]
    ).before(ctx)

    assert ctx.stages_enabled == ["acceso"]
    assert "spento" not in ctx.stage_notes


async def test_il_ledger_attribuisce_le_proprie_note(client):
    """E' l'unico stadio che deve farlo da se', e il motivo e' strutturale.

    La pipeline attribuisce a uno stadio le note comparse mentre girava, ma lo
    fa dopo che lo stadio e' tornato. La contabilita' scrive la riga **prima**
    di quel momento: senza l'eccezione, le sue note - fra cui "costo superiore
    alla baseline", che e' l'avviso piu' importante che il gateway sappia dare
    - non finirebbero mai nel registro.
    """
    client.post("/v1/chat/completions", json=chat_payload())
    eventi = await client.gateway.store.recent_events(1)
    stadi = eventi[0]["stages"]

    assert "ledger" in stadi["enabled"]
    # Puo' non aver avuto niente da dire su questa richiesta: cio' che conta e'
    # che quando dice qualcosa, quel qualcosa risulti suo.
    for nome in stadi.get("acted", {}):
        assert nome in stadi["enabled"]


# --- la baseline, cioe' contro cosa si misura ------------------------------


async def test_col_declassamento_la_baseline_resta_sul_modello_chiesto(settings, stub):
    """Il difetto che ha reso il gateway "dannoso" appena si e' guardato dal vivo.

    Il router riscrive `ctx.model`, e la contabilita' prezzava la baseline su
    quello: il confronto diventava "Haiku senza cache contro Haiku con cache",
    da cui un risparmio prossimo a zero e, bastando una scrittura di cache non
    ancora ripagata, negativo. Il declassamento - che sul banco vale il 17% del
    totale - dal vivo non compariva affatto.
    """
    import anthropic
    import httpx2
    from fastapi.testclient import TestClient

    from ecotokens.server import create_app

    settings.profilo = "aggressivo"
    settings.applica_profilo_aggressivo()
    stub_app, _ = stub
    app = create_app(settings)
    gateway = app.state.gateway
    gateway.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url="http://stub",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )
    gateway.counter._client = gateway.client

    with TestClient(app) as http:
        risposta = http.post(
            "/v1/chat/completions",
            json=chat_payload(model="claude-opus-5"),
        )
        assert risposta.status_code == 200, risposta.text
        # Dentro il contesto: all'uscita il database viene chiuso.
        eventi = await gateway.store.recent_events(1)

    evento = eventi[0]
    assert evento["model"] != "claude-opus-5", "il router doveva declassare"
    # La baseline e' prezzata su Opus, che costa cinque volte tanto: il
    # risparmio deve essere ampiamente positivo, non zero.
    assert evento["baseline_cost_usd"] > evento["cost_usd"] * 2
    assert evento["saved_usd"] > 0


async def test_senza_declassamento_la_baseline_non_cambia(client):
    """Il profilo prudente non tocca il modello: la correzione non deve spostarlo."""
    client.post("/v1/chat/completions", json=chat_payload())
    evento = (await client.gateway.store.recent_events(1))[0]
    assert evento["model"] == "claude-opus-5"
    assert evento["baseline_cost_usd"] > 0


# --- i conteggi per stadio -------------------------------------------------


@pytest.fixture
async def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


async def registra(store, *, stadi_accesi, agiti, model="claude-opus-5"):
    await store.record_usage(
        session_id=None,
        model=model,
        source="api",
        usage=Usage(input_tokens=100, output_tokens=10),
        cost_usd=0.001,
        baseline_cost_usd=0.002,
        saved_usd=0.001,
        latency_ms=12.0,
        notes=[n for note in agiti.values() for n in note],
        stage_notes=agiti,
        stages_enabled=stadi_accesi,
    )


async def test_uno_stadio_mai_intervenuto_si_distingue_da_uno_spento(store):
    for _ in range(4):
        await registra(store, stadi_accesi=["router", "context"], agiti={"router": ["x"]})

    attivita = {voce["stage"]: voce for voce in await store.stage_activity()}
    assert attivita["router"]["acted_in"] == 4
    assert attivita["context"]["acted_in"] == 0
    assert attivita["context"]["enabled_in"] == 4, "acceso e muto, non assente"
    assert "memory" not in attivita, "spento su ogni richiesta: non compare"


async def test_le_note_distinte_vengono_contate_una_per_una(store):
    await registra(store, stadi_accesi=["router"], agiti={"router": ["effort basso"]})
    await registra(store, stadi_accesi=["router"], agiti={"router": ["effort basso"]})
    await registra(store, stadi_accesi=["router"], agiti={"router": ["modello declassato"]})

    router = (await store.stage_activity())[0]
    assert dict(router["notes"]) == {"effort basso": 2, "modello declassato": 1}


async def test_le_note_che_differiscono_solo_nei_numeri_sono_la_stessa_nota(store):
    """Altrimenti ogni richiesta genera una nota distinta e il conteggio e' inutile.

    La nota del pianificatore cita i token stimati, che cambiano a ogni
    richiesta. Contate cosi' com'erano, quindici richieste davano quindici note
    diverse da uno, e il primo avviso della console diceva "11 richieste" dove
    erano 14: il taglio alle prime sei sottostimava tutto.
    """
    for token in (2188, 2026, 2342):
        await registra(
            store,
            stadi_accesi=["cache_planner"],
            agiti={"cache_planner": [f"nessun breakpoint: prompt stimato {token} token, sotto la soglia"]},
        )

    note = (await store.stage_activity())[0]["notes"]
    assert len(note) == 1, "una cosa sola fatta tre volte, non tre cose"
    testo, quante = note[0]
    assert quante == 3
    # Mostrata deve essere una nota vera, non la forma normalizzata: "prompt
    # stimato N token" non e' una frase che qualcuno voglia leggere. Ed e' la
    # piu' recente delle tre, che e' quella che descrive lo stato di adesso.
    assert testo == "nessun breakpoint: prompt stimato 2342 token, sotto la soglia"


async def test_le_righe_precedenti_alla_colonna_non_entrano_nel_conto(store):
    """Un database con storia non deve produrre un denominatore gonfiato.

    Le righe scritte prima che l'attribuzione esistesse hanno `stages` vuoto.
    Contarle direbbe che su N richieste nessuno stadio ha fatto niente, che e'
    una conclusione travestita da dato.
    """
    await store.db.execute(
        """INSERT INTO usage_events (session_id, ts, day, month, model, source, notes)
           VALUES (NULL, 0, '2026-01-01', '2026-01', 'claude-opus-5', 'api', '[]')"""
    )
    await registra(store, stadi_accesi=["router"], agiti={"router": ["x"]})

    attivita = await store.stage_activity()
    assert attivita[0]["requests_considered"] == 1
    assert attivita[0]["ratio"] == 1.0


async def test_un_database_con_storia_si_apre_senza_perdere_niente(tmp_path):
    """Chi usa gia' il gateway ha un database senza le colonne nuove.

    Una migrazione che si rifiuta di partire su un database pieno di storia e'
    peggio di un difetto: e' una perdita di dati travestita da aggiornamento.
    """
    import sqlite3

    percorso = tmp_path / "vecchio.sqlite3"
    conn = sqlite3.connect(percorso)
    conn.executescript(
        """
        CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL NOT NULL,
            day TEXT NOT NULL, month TEXT NOT NULL, model TEXT NOT NULL,
            source TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_ttl TEXT NOT NULL DEFAULT '5m', cost_usd REAL NOT NULL DEFAULT 0,
            baseline_cost_usd REAL NOT NULL DEFAULT 0, saved_usd REAL NOT NULL DEFAULT 0,
            latency_ms REAL, notes TEXT);
        INSERT INTO usage_events (session_id, ts, day, month, model, source,
            input_tokens, output_tokens, cost_usd, baseline_cost_usd, saved_usd, notes)
        VALUES ('s1', 1000, '2026-08-01', '2026-08', 'claude-opus-5', 'api',
                5000, 300, 0.032, 0.032, 0.0, '["nota vecchia"]');
        """
    )
    conn.commit()
    conn.close()

    database = Database(percorso)
    database.connect()
    try:
        colonne = {
            riga[1] for riga in database.conn.execute("PRAGMA table_info(usage_events)")
        }
        assert {"stages", "overhead_tokens", "aux_cost_usd", "client_format"} <= colonne

        vecchio = Store(database)
        assert (await vecchio.stats())["requests"] == 1, "la storia non si perde"
        assert (await vecchio.recent_events(1))[0]["notes"] == ["nota vecchia"]
        # E la riga di ieri non finisce nel denominatore di oggi: contarla
        # direbbe che su una richiesta nessuno stadio ha fatto niente.
        assert await vecchio.stage_activity() == []
    finally:
        database.close()


# --- gli avvisi ------------------------------------------------------------


def test_ogni_avviso_porta_il_proprio_numero():
    """Un avviso senza numero e' un'opinione con l'aria di una misura."""
    dati = {
        "requests": 10,
        "stages": [
            {"stage": "cache_planner", "acted_in": 3, "enabled_in": 10,
             "notes": [["nessun breakpoint: sotto la soglia", 3]]},
            {"stage": "ledger", "acted_in": 2, "enabled_in": 10,
             "notes": [["costo superiore alla baseline di 0.001 USD", 2]]},
            {"stage": "context", "acted_in": 0, "enabled_in": 10, "notes": []},
        ],
        "cache_writes": {"token_sprecati_in_mezzo": 900, "costo_sprecato_usd": 0.0011},
        "spend": {"enabled": True, "today_usd": 4.5, "daily_limit": 5.0, "month_usd": 4.5},
        "calibration": [],
    }
    avvisi = _avvisi(dati)
    assert avvisi, "con questi dati qualcosa da dire c'e'"
    for avviso in avvisi:
        assert avviso["count"], f"avviso senza numero: {avviso['title']}"
        assert avviso["level"] in {"warn", "bad", "idle"}


def test_senza_traffico_non_si_avvisa_di_niente():
    """Zero richieste non sono zero problemi: sono nessuna informazione."""
    assert _avvisi({"requests": 0, "stages": [], "cache_writes": {},
                    "spend": {"enabled": False}, "calibration": []}) == []


def test_il_conteggio_delle_note_cerca_nello_stadio_giusto():
    stadi = [
        {"stage": "router", "notes": [["modello declassato", 5]]},
        {"stage": "cache_planner", "notes": [["modello declassato", 99]]},
    ]
    assert _conta_note(stadi, "router", "declassato") == 5
    assert _conta_note(stadi, "memory", "declassato") == 0


# --- la pagina -------------------------------------------------------------


def test_la_console_non_chiede_niente_alla_rete(client):
    """Un gateway locale che apre una connessione per mostrare una tabella
    tradirebbe il motivo per cui e' locale - e racconterebbe a qualcun altro
    quando l'utente guarda il proprio traffico."""
    import re

    pagina = client.get("/ui")
    assert pagina.status_code == 200
    corpo = pagina.text

    # Nessun https, per niente: la pagina non ha motivo di uscire.
    assert "https://" not in corpo
    # Gli http rimasti sono l'esempio con curl mostrato quando non c'e'
    # ancora traffico, e quello punta al gateway stesso.
    for host in re.findall(r"http://([^/\s\"']+)", corpo):
        assert host.startswith("localhost"), f"riferimento esterno: {host}"
    # E nessun riferimento protocol-relative, che sfuggirebbe ai due sopra.
    assert not re.search(r"(src|href)\s*=\s*[\"']//", corpo)


def test_la_console_risponde_anche_sulla_radice(client):
    assert client.get("/").status_code == 200


async def test_senza_traffico_la_console_non_inventa_numeri(client):
    dati = await build_console_data(client.gateway)
    assert dati["requests"] == 0
    assert dati["alerts"] == []
    assert dati["totals"]["saved_ratio"] == 0.0
    assert dati["not_measured"], "la sezione onesta c'e' anche quando non c'e' altro"


async def test_la_console_legge_e_basta(client):
    """Una pagina che si aggiorna da sola non deve innescare lavoro.

    Se l'apertura della console eseguisse il banco, tenerla aperta cambierebbe
    cio' che sta osservando - e la spesa che mostra.
    """
    prima = client.get("/admin/live").json()
    dopo = client.get("/admin/live").json()
    assert prima["requests"] == dopo["requests"] == 0


async def test_con_traffico_la_console_riporta_quello_che_e_successo(client):
    for indice in range(3):
        payload = chat_payload()
        payload["messages"][-1] = {
            "role": "user",
            "content": f"domanda numero {indice}",
        }
        client.post("/v1/chat/completions", json=payload)

    dati = client.get("/admin/live").json()
    assert dati["requests"] == 3
    assert dati["totals"]["prompt_tokens"] > 0
    nomi = {voce["stage"] for voce in dati["stages"]}
    assert "cache_planner" in nomi
    assert len(dati["recent"]) == 3
    # Il feed deve poter dire, richiesta per richiesta, chi ha fatto cosa.
    assert any(evento["stages"].get("acted") for evento in dati["recent"])


async def test_il_json_della_console_e_serializzabile(client):
    """La pagina lo riceve come JSON: un tipo non serializzabile la spegnerebbe."""
    dati = await build_console_data(client.gateway)
    json.dumps(dati)


# --- perche' uno stadio e' spento ------------------------------------------


async def test_ogni_stadio_spento_dice_perche(client):
    """"Spento" senza una ragione costringe a chiedere a qualcuno.

    E' successo davvero: la console mostrava "memoria - spento" e l'unico modo
    di sapere il perche' era leggere il codice. Il motivo lo dichiara ora la
    configurazione, cioe' il posto dove si decide, e console e dashboard lo
    leggono da li' invece di tenerne una copia che invecchia.
    """
    dati = client.get("/admin/live").json()
    muti = [
        voce["name"]
        for voce in dati["config"]
        if not voce["enabled"] and not voce.get("reason")
    ]
    assert muti == [], f"spenti senza spiegazione: {muti}"


async def test_uno_stadio_acceso_non_porta_una_scusa(client):
    """Il motivo esiste solo mentre serve: accanto a uno attivo sarebbe rumore."""
    dati = client.get("/admin/live").json()
    assert all(not voce["reason"] for voce in dati["config"] if voce["enabled"])


def test_la_console_legge_gli_stadi_dalla_pipeline_non_dalla_configurazione(client):
    """Fra cio' che si e' scritto e cio' che gira c'e' spazio per una differenza.

    Farla vedere e' il mestiere di una console; ripetere l'intenzione no.
    """
    nella_pipeline = [s.name for s in client.gateway.pipeline.stages]
    dalla_console = [voce["name"] for voce in client.get("/admin/live").json()["config"]]
    assert dalla_console == nella_pipeline
