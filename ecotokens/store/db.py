"""Connessione SQLite e schema.

SQLite in modalita' WAL: zero configurazione, zero servizi da installare,
adatto al carico di un gateway locale. Le query girano in un thread separato
con ``asyncio.to_thread`` per non bloccare il loop di FastAPI; un lock serializza
le scritture, dato che la connessione e' condivisa tra thread.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    -- Non e' unica: piu' conversazioni possono iniziare allo stesso modo, e
    -- si distinguono confrontando la cronologia, non l'incipit.
    fingerprint     TEXT NOT NULL,
    model           TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    message_count   INTEGER NOT NULL DEFAULT 0,
    locked_model    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_fingerprint ON sessions(fingerprint);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    position    INTEGER NOT NULL,
    role        TEXT NOT NULL,
    -- Testo normalizzato del messaggio: serve a riconoscere che la cronologia
    -- in arrivo continua quella gia' vista.
    signature   TEXT NOT NULL DEFAULT '',
    content     TEXT,
    created_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_pos ON messages(session_id, position);

CREATE TABLE IF NOT EXISTS summaries (
    session_id  TEXT NOT NULL,
    upto        INTEGER NOT NULL,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (session_id, upto)
);

-- Esito del conteggio vero su ogni candidato alla sostituzione lessicale.
-- Esiste perche' l'intuizione su quanti token vale una parola e' inaffidabile:
-- solo `messages.count_tokens` lo sa, e la risposta dipende dal modello.
CREATE TABLE IF NOT EXISTS substitution_checks (
    source          TEXT NOT NULL,
    model           TEXT NOT NULL,
    target          TEXT NOT NULL,
    tokens_before   INTEGER NOT NULL,
    tokens_after    INTEGER NOT NULL,
    verified        INTEGER NOT NULL,
    checked_at      REAL NOT NULL,
    PRIMARY KEY (source, model)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id              TEXT,
    ts                      REAL NOT NULL,
    day                     TEXT NOT NULL,
    month                   TEXT NOT NULL,
    model                   TEXT NOT NULL,
    source                  TEXT NOT NULL,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    -- TTL con cui la scrittura in cache e' stata pagata: 1.25x a cinque
    -- minuti, 2x a un'ora. Senza questo, il costo di una scrittura mai
    -- riletta si sbaglierebbe di quattro volte.
    cache_ttl               TEXT NOT NULL DEFAULT '5m',
    cost_usd                REAL NOT NULL DEFAULT 0,
    baseline_cost_usd       REAL NOT NULL DEFAULT 0,
    saved_usd               REAL NOT NULL DEFAULT 0,
    latency_ms              REAL,
    notes                   TEXT,
    -- Note attribuite allo stadio che le ha prodotte, piu' l'elenco degli
    -- stadi accesi: senza il secondo, uno stadio a zero interventi non si
    -- distingue da uno spento.
    stages                  TEXT NOT NULL DEFAULT '',
    -- Token che il gateway ha aggiunto di suo al prompt. L'utente li paga
    -- senza averli scritti.
    overhead_tokens         INTEGER NOT NULL DEFAULT 0,
    -- Spesa delle chiamate che il gateway fa per conto proprio (riassunti,
    -- estrazione dei fatti). E' dentro cost_usd, ma va potuta separare:
    -- altrimenti uno stadio che chiama un modello sembra gratuito.
    aux_cost_usd            REAL NOT NULL DEFAULT 0,
    -- Porta d'ingresso: dialetto OpenAI o nativo Anthropic.
    client_format           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_events(day);
CREATE INDEX IF NOT EXISTS idx_usage_month ON usage_events(month);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id);

-- Confronto fra la stima locale dei token e il conteggio vero dell'API.
-- Ogni chiamata a /v1/messages/count_tokens ne produce una riga: e' l'unico
-- modo che il progetto ha di sapere quanto vale il proprio metro senza
-- spendere apposta per scoprirlo.
CREATE TABLE IF NOT EXISTS token_estimates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    model       TEXT NOT NULL,
    exact       INTEGER NOT NULL,
    estimated   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_estimates_model ON token_estimates(model);

-- Riepilogo per giorno dei consumi il cui dettaglio e' stato cancellato.
--
-- `usage_events` ha una riga per richiesta, con note e attribuzione per
-- stadio: su un servizio con qualche migliaio di richieste al giorno cresce
-- senza limite, e le pagine che lo leggono rallentano con lui. Il dettaglio
-- serve per qualche giorno - "cosa e' successo stamattina" - i totali per
-- sempre. Da qui due tabelle invece di una politica di cancellazione: buttare
-- e basta renderebbe falsi i totali storici, che e' il difetto del metro che
-- questo progetto passa il tempo a correggere.
CREATE TABLE IF NOT EXISTS usage_daily (
    day                     TEXT NOT NULL,
    model                   TEXT NOT NULL,
    source                  TEXT NOT NULL,
    requests                INTEGER NOT NULL DEFAULT 0,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cost_usd                REAL NOT NULL DEFAULT 0,
    baseline_cost_usd       REAL NOT NULL DEFAULT 0,
    baseline_ingenua_usd    REAL NOT NULL DEFAULT 0,
    saved_usd               REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model, source)
);
CREATE INDEX IF NOT EXISTS idx_usage_daily_day ON usage_daily(day);

CREATE TABLE IF NOT EXISTS bench_runs (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    label       TEXT NOT NULL,
    mode        TEXT NOT NULL,
    corpus      TEXT,
    -- Impronta del contenuto del corpus, non del suo elenco. Serve a sapere
    -- se due misure sono confrontabili: lo scenario `costruzione` legge i
    -- sorgenti veri del progetto, quindi il carico cresce insieme al codice e
    -- due esecuzioni distanti nel tempo non misurano la stessa cosa.
    fingerprint TEXT NOT NULL DEFAULT '',
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_bench_runs_time ON bench_runs(created_at);

CREATE TABLE IF NOT EXISTS bench_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    scenario            TEXT NOT NULL,
    variant             TEXT NOT NULL,
    requests            INTEGER NOT NULL DEFAULT 0,
    upstream_calls      INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    full_price_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0,
    latency_ms          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bench_results_run ON bench_results(run_id);

-- Esiti di `ecotokens ritenzione`. Registrati perche' la misura dura mezzo
-- minuto e il quadro deve aprirsi subito: una pagina di controllo che si fa
-- aspettare non viene guardata, e una che non viene guardata non controlla.
CREATE TABLE IF NOT EXISTS retention_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    scenario    TEXT NOT NULL,
    variant     TEXT NOT NULL,
    kept        INTEGER NOT NULL DEFAULT 0,
    lost        INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    summaries   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_retention_created ON retention_runs(created_at);

CREATE TABLE IF NOT EXISTS cache_entries (
    key         TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    usage       TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);

CREATE TABLE IF NOT EXISTS semantic_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key   TEXT NOT NULL,
    model       TEXT NOT NULL,
    prefix_hash TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantic_lookup
    ON semantic_entries(model, prefix_hash, expires_at);

CREATE TABLE IF NOT EXISTS memory_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    scope        TEXT NOT NULL DEFAULT 'session',
    text         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    uses         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_facts(session_id);
"""

# Indicizzazione BM25 dei fatti di memoria. FTS5 e' compilato nelle build
# ufficiali di CPython, ma non e' garantito ovunque: se manca, il recupero
# ripiega su un punteggio lessicale calcolato in Python.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text,
    content='memory_facts',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memory_facts_ai AFTER INSERT ON memory_facts BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_facts_ad AFTER DELETE ON memory_facts BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_facts_au AFTER UPDATE ON memory_facts BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


# Colonne aggiunte dopo che lo schema era gia' in giro. `CREATE TABLE IF NOT
# EXISTS` non tocca una tabella che esiste, quindi un database creato da una
# versione precedente resterebbe senza - e le query nuove fallirebbero su di
# esso invece che su un database vuoto, cioe' proprio dove i dati contano.
COLONNE_AGGIUNTE: list[tuple[str, str, str]] = [
    ("usage_events", "cache_ttl", "TEXT NOT NULL DEFAULT '5m'"),
    ("bench_runs", "fingerprint", "TEXT NOT NULL DEFAULT ''"),
    # Le quattro grandezze che il gateway calcolava gia' e poi buttava via.
    # Non e' stato un risparmio: senza di esse, sul traffico vero non si poteva
    # rispondere a "quante volte ogni stadio ha fatto qualcosa", che e' la
    # domanda che il progetto si e' imposto di fare prima di raffinare uno
    # stadio. Il default vuoto va letto come "non registrato", non come zero.
    ("usage_events", "stages", "TEXT NOT NULL DEFAULT ''"),
    ("usage_events", "overhead_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_events", "aux_cost_usd", "REAL NOT NULL DEFAULT 0"),
    ("usage_events", "client_format", "TEXT NOT NULL DEFAULT ''"),
    # La baseline che conta davvero: cosa avrebbe pagato un client senza
    # gateway ma non ingenuo, cioe' uno che si mette da solo un `cache_control`
    # in cima al system prompt. Il default zero va letto come "non registrata":
    # le righe scritte prima di questa colonna non possono saperlo, e riempirle
    # con la baseline a prezzo pieno le farebbe sembrare misurate.
    ("usage_events", "baseline_ingenua_usd", "REAL NOT NULL DEFAULT 0"),
    ("usage_daily", "baseline_ingenua_usd", "REAL NOT NULL DEFAULT 0"),
]


def _aggiungi_colonne_mancanti(conn: sqlite3.Connection) -> None:
    """Migrazione minima: aggiunge le colonne nuove alle tabelle esistenti.

    SQLite non ha `ADD COLUMN IF NOT EXISTS`, quindi si guarda prima. Ogni
    colonna qui elencata deve avere un default: le righe gia' scritte non
    possono saperne il valore, e un default sbagliato e' comunque meglio di
    una migrazione che si rifiuta di partire su un database pieno di storia.
    """
    for tabella, colonna, tipo in COLONNE_AGGIUNTE:
        presenti = {riga[1] for riga in conn.execute(f"PRAGMA table_info({tabella})")}
        if presenti and colonna not in presenti:
            conn.execute(f"ALTER TABLE {tabella} ADD COLUMN {colonna} {tipo}")


class Database:
    """Wrapper asincrono minimale attorno a sqlite3."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self.has_fts = False

    # -- ciclo di vita ---------------------------------------------------

    def connect(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _aggiungi_colonne_mancanti(conn)
        try:
            conn.executescript(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            self.has_fts = False
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.commit()
                self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database non connesso: chiamare connect() prima")
        return self._conn

    # -- primitive sincrone ----------------------------------------------

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # -- API asincrona ----------------------------------------------------
    #
    # Le operazioni del percorso caldo girano **sul loop**, non su un thread.
    # Misurato: una `SELECT 1` costa 6,9 us dentro SQLite e 448 attraverso
    # `asyncio.to_thread` - il trasporto vale 65 volte il lavoro. Con otto
    # operazioni per richiesta erano 3,5 ms di soli salti fra thread su 15,8
    # totali, e toglierli porta il gateway da 63 a 94 richieste al secondo.
    #
    # Bloccare il loop per qualche microsecondo non e' un problema: e' meno di
    # quanto costi lo scheduling che si voleva evitare. Lo diventa per le query
    # di osservazione, che leggono migliaia di righe e arrivano a decine di
    # millisecondi: quelle passano da `pesante=True` e restano su un thread,
    # cosi' non fermano le richieste vere mentre la console si aggiorna.

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Esegue una scrittura e restituisce il lastrowid."""
        cursor = self._execute(sql, params)
        return int(cursor.lastrowid or 0)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self._executemany(sql, list(rows))

    async def query(
        self, sql: str, params: Sequence[Any] = (), *, pesante: bool = False
    ) -> list[sqlite3.Row]:
        if pesante:
            return await asyncio.to_thread(self._query, sql, params)
        return self._query(sql, params)

    async def query_one(
        self, sql: str, params: Sequence[Any] = (), *, pesante: bool = False
    ) -> sqlite3.Row | None:
        if pesante:
            return await asyncio.to_thread(self._query_one, sql, params)
        return self._query_one(sql, params)
