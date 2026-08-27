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
    cost_usd                REAL NOT NULL DEFAULT 0,
    baseline_cost_usd       REAL NOT NULL DEFAULT 0,
    saved_usd               REAL NOT NULL DEFAULT 0,
    latency_ms              REAL,
    notes                   TEXT
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

CREATE TABLE IF NOT EXISTS bench_runs (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    label       TEXT NOT NULL,
    mode        TEXT NOT NULL,
    corpus      TEXT,
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

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Esegue una scrittura e restituisce il lastrowid."""
        cursor = await asyncio.to_thread(self._execute, sql, params)
        return int(cursor.lastrowid or 0)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        await asyncio.to_thread(self._executemany, sql, list(rows))

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._query, sql, params)

    async def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._query_one, sql, params)
