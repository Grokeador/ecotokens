"""Accesso ai dati: sessioni, messaggi, contabilita', cache, memoria."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..pricing import Usage
from .db import Database


def _now() -> float:
    return time.time()


def _day_month(ts: float) -> tuple[str, str]:
    moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%Y-%m")


@dataclass
class Session:
    id: str
    fingerprint: str
    model: str
    created_at: float
    updated_at: float
    turn_count: int
    message_count: int
    locked_model: str | None

    @property
    def is_first_turn(self) -> bool:
        return self.turn_count == 0

    @property
    def seconds_since_update(self) -> float:
        return max(0.0, _now() - self.updated_at)


@dataclass
class CachedResponse:
    key: str
    model: str
    response: dict[str, Any]
    usage: Usage
    created_at: float
    hits: int


class Store:
    """Tutte le query del gateway, raggruppate per dominio."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- sessioni ---------------------------------------------------------

    async def find_sessions(self, fingerprint: str, ttl_hours: int) -> list[Session]:
        """Sessioni che iniziano allo stesso modo, dalla piu' recente.

        L'incipit identifica una famiglia di conversazioni, non una singola:
        chi sceglie quale sia la continuazione giusta e' il confronto della
        cronologia, che avviene nello stadio di sessione.
        """
        cutoff = _now() - ttl_hours * 3600
        rows = await self.db.query(
            """SELECT * FROM sessions
               WHERE fingerprint = ? AND updated_at >= ?
               ORDER BY updated_at DESC""",
            (fingerprint, cutoff),
        )
        return [Session(**dict(row)) for row in rows]

    async def message_signatures(self, session_id: str) -> list[str]:
        """Cronologia normalizzata di una sessione, in ordine."""
        rows = await self.db.query(
            "SELECT signature FROM messages WHERE session_id = ? ORDER BY position",
            (session_id,),
        )
        return [row["signature"] for row in rows]

    async def create_session(self, fingerprint: str, model: str) -> Session:
        now = _now()
        session_id = uuid.uuid4().hex[:16]
        await self.db.execute(
            """INSERT INTO sessions
               (id, fingerprint, model, created_at, updated_at, turn_count, message_count)
               VALUES (?, ?, ?, ?, ?, 0, 0)""",
            (session_id, fingerprint, model, now, now),
        )
        return Session(
            id=session_id,
            fingerprint=fingerprint,
            model=model,
            created_at=now,
            updated_at=now,
            turn_count=0,
            message_count=0,
            locked_model=None,
        )

    async def touch_session(
        self, session_id: str, *, message_count: int, locked_model: str | None = None
    ) -> None:
        await self.db.execute(
            """UPDATE sessions
               SET updated_at = ?,
                   turn_count = turn_count + 1,
                   message_count = ?,
                   locked_model = COALESCE(?, locked_model)
               WHERE id = ?""",
            (_now(), message_count, locked_model, session_id),
        )

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.query(
            """SELECT s.*,
                      COALESCE(SUM(u.cost_usd), 0)  AS cost_usd,
                      COALESCE(SUM(u.saved_usd), 0) AS saved_usd
               FROM sessions s
               LEFT JOIN usage_events u ON u.session_id = s.id
               GROUP BY s.id
               ORDER BY s.updated_at DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in rows]

    # -- messaggi ---------------------------------------------------------

    async def save_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        signatures: list[str],
        store_content: bool,
    ) -> None:
        """Salva la cronologia, sovrascrivendo quella precedente.

        Le firme sono quelle dei messaggi **come sono arrivati dal client**, non
        del prompt riscritto: sono il metro con cui la richiesta successiva
        verra' riconosciuta come continuazione.
        """
        await self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        now = _now()
        # Il numero di righe segue le firme, non i messaggi inviati all'API:
        # compattazione e memoria possono aver riscritto il prompt, ma il
        # riconoscimento deve confrontarsi con cio' che il client ha mandato.
        aligned = len(messages) == len(signatures)
        rows = [
            (
                session_id,
                position,
                signature.split(":", 1)[0],
                signature,
                json.dumps(messages[position].get("content"), ensure_ascii=False, sort_keys=True)
                if store_content and aligned
                else None,
                now,
            )
            for position, signature in enumerate(signatures)
        ]
        if rows:
            await self.db.executemany(
                """INSERT INTO messages
                   (session_id, position, role, signature, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    # -- riassunti di compattazione ---------------------------------------

    async def get_summary(self, session_id: str, upto: int) -> str | None:
        """Riassunto gia' calcolato per i primi ``upto`` messaggi.

        Riusarlo alla lettera non e' un'ottimizzazione secondaria: se il testo
        del riassunto cambiasse a ogni turno, cambierebbe il prefisso del prompt
        e la cache mancherebbe sempre.
        """
        row = await self.db.query_one(
            "SELECT text FROM summaries WHERE session_id = ? AND upto = ?",
            (session_id, upto),
        )
        return row["text"] if row else None

    async def get_previous_summary(self, session_id: str, before: int) -> tuple[int, str] | None:
        """Riassunto piu' avanzato fra quelli gia' calcolati prima di ``before``.

        Serve al riassunto incrementale: quando il punto di taglio avanza, il
        riassunto nuovo parte da questo e legge solo i messaggi aggiunti nel
        frattempo, invece di rileggere da capo tutta la cronologia.
        """
        row = await self.db.query_one(
            """SELECT upto, text FROM summaries
               WHERE session_id = ? AND upto < ?
               ORDER BY upto DESC LIMIT 1""",
            (session_id, before),
        )
        return (row["upto"], row["text"]) if row else None

    async def put_summary(self, session_id: str, upto: int, text: str) -> None:
        await self.db.execute(
            """INSERT INTO summaries (session_id, upto, text, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id, upto) DO UPDATE SET text = excluded.text""",
            (session_id, upto, text, _now()),
        )

    # -- taratura dello stimatore ------------------------------------------

    async def record_token_estimate(self, *, model: str, exact: int, estimated: int) -> None:
        """Registra uno scarto fra stima locale e conteggio vero.

        Costa una riga e non costa un token: la chiamata all'API era gia' stata
        fatta per rispondere al client.
        """
        if exact <= 0:
            return
        await self.db.execute(
            "INSERT INTO token_estimates (ts, model, exact, estimated) VALUES (?, ?, ?, ?)",
            (_now(), model, int(exact), int(estimated)),
        )

    async def estimate_calibration(self) -> list[dict[str, Any]]:
        """Quanto sbaglia lo stimatore locale, per modello.

        Lo scarto medio dice se la stima e' sistematicamente alta o bassa; il
        minimo e il massimo dicono se e' affidabile o solo mediamente giusta.
        Una stima che sbaglia del +5% sempre e' utilizzabile; una che oscilla
        fra -30% e +40% con media zero non lo e', e la media da sola non lo
        farebbe vedere.
        """
        righe = await self.db.query(
            """SELECT model,
                      COUNT(*) AS campioni,
                      SUM(exact) AS token_esatti,
                      AVG((estimated - exact) * 1.0 / exact) AS scarto_medio,
                      MIN((estimated - exact) * 1.0 / exact) AS scarto_min,
                      MAX((estimated - exact) * 1.0 / exact) AS scarto_max
               FROM token_estimates
               GROUP BY model
               ORDER BY campioni DESC"""
        )
        return [dict(riga) for riga in righe]

    # -- sostituzioni lessicali --------------------------------------------

    async def verified_substitutions(self, model: str | None = None) -> list[str]:
        """Candidati che il conteggio vero ha promosso.

        Vuoto finche' nessuno ha eseguito ``ecotokens substitutions --live``:
        senza quel passaggio non si sa se una parola piu' corta sia anche piu'
        economica, e lo stadio preferisce non applicare nulla piuttosto che
        applicare un'intuizione.
        """
        if model:
            righe = await self.db.query(
                "SELECT source FROM substitution_checks WHERE verified = 1 AND model = ?",
                (model,),
            )
        else:
            righe = await self.db.query(
                "SELECT DISTINCT source FROM substitution_checks WHERE verified = 1"
            )
        return [riga["source"] for riga in righe]

    async def record_substitution_check(
        self,
        *,
        source: str,
        target: str,
        model: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        await self.db.execute(
            """INSERT INTO substitution_checks
               (source, model, target, tokens_before, tokens_after, verified, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, model) DO UPDATE SET
                 target = excluded.target,
                 tokens_before = excluded.tokens_before,
                 tokens_after = excluded.tokens_after,
                 verified = excluded.verified,
                 checked_at = excluded.checked_at""",
            (
                source,
                model,
                target,
                tokens_before,
                tokens_after,
                1 if tokens_after < tokens_before else 0,
                _now(),
            ),
        )

    async def substitution_report(self) -> list[dict[str, Any]]:
        righe = await self.db.query(
            "SELECT * FROM substitution_checks ORDER BY (tokens_before - tokens_after) DESC"
        )
        return [dict(riga) for riga in righe]

    # -- contabilita' ------------------------------------------------------

    async def record_usage(
        self,
        *,
        session_id: str | None,
        model: str,
        source: str,
        usage: Usage,
        cost_usd: float,
        baseline_cost_usd: float,
        saved_usd: float,
        cache_ttl: str = "5m",
        latency_ms: float | None = None,
        notes: list[str] | None = None,
    ) -> None:
        ts = _now()
        day, month = _day_month(ts)
        await self.db.execute(
            """INSERT INTO usage_events
               (session_id, ts, day, month, model, source, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, cache_ttl, cost_usd,
                baseline_cost_usd, saved_usd, latency_ms, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                ts,
                day,
                month,
                model,
                source,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_creation_tokens,
                usage.cache_read_tokens,
                cache_ttl,
                cost_usd,
                baseline_cost_usd,
                saved_usd,
                latency_ms,
                json.dumps(notes or [], ensure_ascii=False),
            ),
        )

    async def spend_since(self, column: str, value: str) -> float:
        """Spesa effettiva nel giorno o nel mese indicato."""
        if column not in {"day", "month"}:
            raise ValueError("column deve essere 'day' o 'month'")
        row = await self.db.query_one(
            f"SELECT COALESCE(SUM(cost_usd), 0) AS total FROM usage_events WHERE {column} = ?",
            (value,),
        )
        return float(row["total"]) if row else 0.0

    async def current_spend(self) -> tuple[float, float]:
        """(spesa di oggi, spesa del mese corrente) in dollari."""
        day, month = _day_month(_now())
        return await self.spend_since("day", day), await self.spend_since("month", month)

    async def stats(self) -> dict[str, Any]:
        totals = await self.db.query_one(
            """SELECT COUNT(*)                              AS requests,
                      COALESCE(SUM(input_tokens), 0)         AS input_tokens,
                      COALESCE(SUM(output_tokens), 0)        AS output_tokens,
                      COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                      COALESCE(SUM(cache_read_tokens), 0)    AS cache_read_tokens,
                      COALESCE(SUM(cost_usd), 0)             AS cost_usd,
                      COALESCE(SUM(baseline_cost_usd), 0)    AS baseline_cost_usd,
                      COALESCE(SUM(saved_usd), 0)            AS saved_usd
               FROM usage_events"""
        )
        by_source = await self.db.query(
            """SELECT source, COUNT(*) AS requests, COALESCE(SUM(saved_usd), 0) AS saved_usd
               FROM usage_events GROUP BY source"""
        )
        by_model = await self.db.query(
            """SELECT model, COUNT(*) AS requests,
                      COALESCE(SUM(cost_usd), 0)  AS cost_usd,
                      COALESCE(SUM(saved_usd), 0) AS saved_usd
               FROM usage_events GROUP BY model ORDER BY cost_usd DESC"""
        )
        by_day = await self.db.query(
            """SELECT day, COUNT(*) AS requests,
                      COALESCE(SUM(cost_usd), 0)  AS cost_usd,
                      COALESCE(SUM(saved_usd), 0) AS saved_usd
               FROM usage_events GROUP BY day ORDER BY day DESC LIMIT 30"""
        )
        result = dict(totals) if totals else {}
        prompt_tokens = (
            int(result.get("input_tokens", 0))
            + int(result.get("cache_creation_tokens", 0))
            + int(result.get("cache_read_tokens", 0))
        )
        result["total_prompt_tokens"] = prompt_tokens
        # Quota dei token di prompt serviti dalla cache: la metrica che dice se
        # il cache planner sta funzionando davvero.
        result["cache_hit_ratio"] = (
            int(result.get("cache_read_tokens", 0)) / prompt_tokens if prompt_tokens else 0.0
        )
        result["by_source"] = [dict(row) for row in by_source]
        result["by_model"] = [dict(row) for row in by_model]
        result["by_day"] = [dict(row) for row in by_day]
        return result

    async def cache_write_report(self, limit: int = 20_000) -> dict[str, Any]:
        """Quante scritture in cache del traffico vero non sono state rilette.

        La stessa domanda che `ecotokens cachewrites` pone al simulatore, qui
        posta ai dati veri. Il conto lo fa `cache_audit`, che vuole gli eventi
        in ordine cronologico dentro ogni sessione: da qui l'ORDER BY.

        Solo le richieste andate davvero all'API. Un hit della cache esatta non
        raggiunge Anthropic e non tocca nessuna voce di cache: contarlo
        allungherebbe le sessioni con eventi vuoti senza cambiare nulla.

        Le richieste senza sessione ricevono ognuna un identificatore suo. Non
        sapendo se continuino una conversazione, incatenarle sarebbe un'ipotesi
        travestita da dato: cosi' ognuna risulta una scrittura di coda, che e'
        la lettura piu' prudente.
        """
        from ..cache_audit import CacheEvent, audit_cache_writes
        from ..pipeline.base import SOURCE_API

        rows = await self.db.query(
            """SELECT session_id, id, model, cache_ttl,
                      cache_read_tokens, cache_creation_tokens
               FROM usage_events
               WHERE source = ?
               ORDER BY session_id, ts, id
               LIMIT ?""",
            (SOURCE_API, limit),
        )
        eventi = [
            CacheEvent(
                session_id=riga["session_id"] or f"senza-sessione:{riga['id']}",
                read_tokens=int(riga["cache_read_tokens"] or 0),
                write_tokens=int(riga["cache_creation_tokens"] or 0),
                model=riga["model"],
                cache_ttl=riga["cache_ttl"] or "5m",
            )
            for riga in rows
        ]
        return audit_cache_writes(eventi).to_dict()

    # -- cache esatta -----------------------------------------------------

    async def get_cached(self, key: str) -> CachedResponse | None:
        row = await self.db.query_one(
            "SELECT * FROM cache_entries WHERE key = ? AND expires_at > ?", (key, _now())
        )
        if row is None:
            return None
        await self.db.execute(
            "UPDATE cache_entries SET hits = hits + 1, last_hit_at = ? WHERE key = ?",
            (_now(), key),
        )
        return CachedResponse(
            key=row["key"],
            model=row["model"],
            response=json.loads(row["response"]),
            usage=Usage(**json.loads(row["usage"])) if row["usage"] else Usage(),
            created_at=row["created_at"],
            hits=row["hits"] + 1,
        )

    async def put_cached(
        self, key: str, model: str, response: dict[str, Any], usage: Usage, ttl_seconds: int
    ) -> None:
        now = _now()
        await self.db.execute(
            """INSERT INTO cache_entries (key, model, response, usage, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   response = excluded.response,
                   usage = excluded.usage,
                   created_at = excluded.created_at,
                   expires_at = excluded.expires_at""",
            (
                key,
                model,
                json.dumps(response, ensure_ascii=False),
                json.dumps(usage.__dict__),
                now,
                now + ttl_seconds,
            ),
        )

    async def prune_cache(self, max_entries: int) -> int:
        await self.db.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (_now(),))
        row = await self.db.query_one("SELECT COUNT(*) AS n FROM cache_entries")
        total = int(row["n"]) if row else 0
        if total > max_entries:
            await self.db.execute(
                """DELETE FROM cache_entries WHERE key IN (
                       SELECT key FROM cache_entries
                       ORDER BY COALESCE(last_hit_at, created_at) ASC LIMIT ?
                   )""",
                (total - max_entries,),
            )
        return total

    async def clear_caches(self) -> None:
        await self.db.execute("DELETE FROM cache_entries", ())
        await self.db.execute("DELETE FROM semantic_entries", ())

    # -- cache semantica --------------------------------------------------

    async def add_semantic(
        self,
        *,
        cache_key: str,
        model: str,
        prefix_hash: str,
        prompt: str,
        embedding: bytes,
        ttl_seconds: int,
    ) -> None:
        now = _now()
        await self.db.execute(
            """INSERT INTO semantic_entries
               (cache_key, model, prefix_hash, prompt, embedding, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cache_key, model, prefix_hash, prompt, embedding, now, now + ttl_seconds),
        )

    async def semantic_candidates(
        self, model: str, prefix_hash: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = await self.db.query(
            """SELECT cache_key, prompt, embedding FROM semantic_entries
               WHERE model = ? AND prefix_hash = ? AND expires_at > ?
               ORDER BY created_at DESC LIMIT ?""",
            (model, prefix_hash, _now(), limit),
        )
        return [dict(row) for row in rows]

    # -- memoria ----------------------------------------------------------

    async def add_facts(self, session_id: str | None, facts: list[str]) -> int:
        if not facts:
            return 0
        now = _now()
        await self.db.executemany(
            """INSERT INTO memory_facts (session_id, scope, text, created_at)
               VALUES (?, 'session', ?, ?)""",
            [(session_id, fact, now) for fact in facts],
        )
        return len(facts)

    async def search_facts(self, session_id: str | None, query: str, limit: int) -> list[str]:
        """Recupero per pertinenza: BM25 se FTS5 c'e', altrimenti lessicale."""
        if self.db.has_fts and query.strip():
            terms = [word for word in _tokenize(query) if len(word) > 2][:12]
            if terms:
                match = " OR ".join(terms)
                try:
                    rows = await self.db.query(
                        """SELECT f.id, f.text
                           FROM memory_fts
                           JOIN memory_facts f ON f.id = memory_fts.rowid
                           WHERE memory_fts MATCH ?
                             AND (f.session_id = ? OR f.scope = 'global')
                           ORDER BY bm25(memory_fts) LIMIT ?""",
                        (match, session_id, limit),
                    )
                    if rows:
                        await self._mark_used([int(row["id"]) for row in rows])
                        return [row["text"] for row in rows]
                except Exception:
                    # FTS puo' rifiutare query con sintassi inattesa: si ripiega.
                    pass
        return await self._search_facts_lexical(session_id, query, limit)

    async def _search_facts_lexical(
        self, session_id: str | None, query: str, limit: int
    ) -> list[str]:
        rows = await self.db.query(
            """SELECT id, text FROM memory_facts
               WHERE session_id = ? OR scope = 'global'
               ORDER BY created_at DESC LIMIT 500""",
            (session_id,),
        )
        query_terms = set(_tokenize(query))
        scored: list[tuple[float, int, str]] = []
        for row in rows:
            fact_terms = set(_tokenize(row["text"]))
            if not fact_terms:
                continue
            overlap = len(query_terms & fact_terms)
            if overlap:
                scored.append((overlap / len(fact_terms), int(row["id"]), row["text"]))
        scored.sort(reverse=True)
        selected = scored[:limit]
        await self._mark_used([item[1] for item in selected])
        return [item[2] for item in selected]

    async def _mark_used(self, fact_ids: list[int]) -> None:
        if not fact_ids:
            return
        now = _now()
        await self.db.executemany(
            "UPDATE memory_facts SET uses = uses + 1, last_used_at = ? WHERE id = ?",
            [(now, fact_id) for fact_id in fact_ids],
        )

    async def existing_facts(self, session_id: str | None) -> set[str]:
        rows = await self.db.query(
            "SELECT text FROM memory_facts WHERE session_id = ?", (session_id,)
        )
        return {row["text"] for row in rows}


def _tokenize(text: str) -> list[str]:
    return [
        word
        for word in "".join(c.lower() if c.isalnum() else " " for c in text).split()
        if word
    ]
