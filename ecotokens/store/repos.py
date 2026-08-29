"""Accesso ai dati: sessioni, messaggi, contabilita', cache, memoria."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from ..pricing import Usage
from .db import Database

# Qualunque sequenza di cifre, con o senza separatori: serve a ridurre una nota
# alla frase che la descrive, togliendo le quantita' che cambiano a ogni
# richiesta.
_NUMERI = re.compile(r"[0-9][0-9.,]*")


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
        self._prefissi_visti: dict[str, float] = {}

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

    # Prefissi stabili visti passare di recente: impronta -> istante.
    #
    # Serve a una domanda sola, e delicata: **un client senza gateway avrebbe
    # avuto questo prefisso gia' in cache?** La risposta non puo' dipendere da
    # cosa abbiamo deciso noi. La prima versione la ricavava dai nostri
    # `cache_read_tokens`, e quindi spegnendo il nostro pianificatore il
    # concorrente risultava freddo su ogni richiesta: bastava smettere di
    # ottimizzare per sembrare piu' bravi del 13,8%. Circolare, e nella
    # direzione comoda.
    #
    # Qui invece si guarda il **traffico**: se lo stesso `tools` + `system` e'
    # passato di qui negli ultimi cinque minuti, allora era caldo per chiunque.
    # E' in memoria e non su disco perche' e' una domanda su una finestra di
    # cinque minuti; si azzera al riavvio, e per il primo giro di ogni prefisso
    # sbaglia **contro** il gateway, che e' il verso giusto in cui sbagliare.
    _FINESTRA_PREFISSI = 300.0
    _MAX_PREFISSI = 4096

    def prefisso_gia_visto(self, impronta: str) -> bool:
        """Vero se questo prefisso stabile e' passato negli ultimi 5 minuti."""
        adesso = _now()
        visti = self._prefissi_visti
        precedente = visti.get(impronta)
        visti[impronta] = adesso
        if len(visti) > self._MAX_PREFISSI:
            # Potatura pigra: si buttano quelli fuori finestra, e se non basta
            # i piu' vecchi. Un dizionario che cresce senza limite in un
            # processo che gira per settimane e' una perdita di memoria.
            soglia = adesso - self._FINESTRA_PREFISSI
            self._prefissi_visti = visti = {
                k: v for k, v in visti.items() if v >= soglia
            }
            if len(visti) > self._MAX_PREFISSI:
                for chiave in sorted(visti, key=visti.get)[: len(visti) // 2]:
                    del visti[chiave]
        return precedente is not None and adesso - precedente <= self._FINESTRA_PREFISSI

    async def tasso_continuazione(self, minimo: int = 5) -> float | None:
        """Quanto spesso una conversazione arriva almeno al secondo turno.

        Risponde alla domanda da cui dipende se convenga marcare la coda di una
        richiesta appena arrivata: quel marker paga 0,25x in piu' subito e
        risparmia 0,9x **solo se** un turno successivo lo rilegge. Il pareggio
        e' quindi a 0,25/0,9, e la decisione giusta e' quella che minimizza il
        costo atteso - per cui serve una **stima della frazione**, non un
        intervallo di confidenza.

        La frazione grezza pero' su poche sessioni vale zero o uno e
        deciderebbe sul rumore. Si usa quindi la media a posteriori con prior
        di Jeffreys, `(proseguite + 0,5) / (totali + 1)`: con zero
        continuazioni su cinque da' l'8%, con due su cinque il 42%, e converge
        alla frazione vera man mano che le sessioni arrivano. La decisione si
        rifa' a ogni richiesta, quindi un'installazione che cambia carattere si
        corregge da sola.

        La prima versione aspettava venti sessioni e restituiva la frazione
        secca. Misurato: su traffico a turno singolo la regola arrivava tardi e
        lasciava sul tavolo la meta' del suo effetto.
        """
        riga = await self.db.query_one(
            """SELECT COUNT(*) AS totali,
                      SUM(CASE WHEN turn_count > 1 THEN 1 ELSE 0 END) AS proseguite
               FROM sessions""",
            pesante=True,
        )
        totali = int(riga["totali"] or 0) if riga else 0
        if totali < minimo:
            return None
        proseguite = float(riga["proseguite"] or 0)
        return (proseguite + 0.5) / (totali + 1.0)

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
        baseline_ingenua_usd: float = 0.0,
        costo_modello_richiesto_usd: float = 0.0,
        cache_ttl: str = "5m",
        latency_ms: float | None = None,
        notes: list[str] | None = None,
        stage_notes: dict[str, list[str]] | None = None,
        stages_enabled: list[str] | None = None,
        overhead_tokens: int = 0,
        aux_cost_usd: float = 0.0,
        client_format: str = "",
    ) -> None:
        ts = _now()
        day, month = _day_month(ts)
        # `enabled` e' il denominatore di ogni conteggio per stadio: senza,
        # "non e' mai intervenuto" e "non era acceso" darebbero lo stesso zero.
        stadi = json.dumps(
            {"enabled": list(stages_enabled or []), "acted": stage_notes or {}},
            ensure_ascii=False,
        )
        await self.db.execute(
            """INSERT INTO usage_events
               (session_id, ts, day, month, model, source, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, cache_ttl, cost_usd,
                baseline_cost_usd, saved_usd, latency_ms, notes,
                stages, overhead_tokens, aux_cost_usd, client_format,
                baseline_ingenua_usd, costo_modello_richiesto_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                stadi,
                int(overhead_tokens),
                float(aux_cost_usd),
                client_format,
                float(baseline_ingenua_usd),
                float(costo_modello_richiesto_usd),
            ),
        )

    async def spend_since(self, column: str, value: str) -> float:
        """Spesa effettiva nel giorno o nel mese indicato."""
        if column not in {"day", "month"}:
            raise ValueError("column deve essere 'day' o 'month'")
        # Il riepilogo ha `day`, non `month`: il mese si ricava dal prefisso.
        confronto = "day = ?" if column == "day" else "substr(day, 1, 7) = ?"
        row = await self.db.query_one(
            f"""SELECT COALESCE(SUM(cost_usd), 0) AS total FROM (
                    SELECT day, cost_usd FROM usage_events
                    UNION ALL SELECT day, cost_usd FROM usage_daily
                ) WHERE {confronto}""",
            (value,),
        )
        return float(row["total"]) if row else 0.0

    async def current_spend(self) -> tuple[float, float]:
        """(spesa di oggi, spesa del mese corrente) in dollari."""
        day, month = _day_month(_now())
        return await self.spend_since("day", day), await self.spend_since("month", month)

    # Dettaglio e riepiloghi in una vista sola. Dopo `compatta_consumi` una
    # parte della storia vive in `usage_daily` e il resto in `usage_events`:
    # sommare solo il secondo farebbe **calare** i totali storici a ogni
    # compattazione, cioe' trasformerebbe una pulizia in una falsificazione.
    # Le colonne del dettaglio che il riepilogo non ha - latenza, note, stadi -
    # restano fuori apposta: sono le domande a cui, passati i giorni di
    # dettaglio, il gateway non sa piu' rispondere, e va detto invece di
    # rispondere con uno zero.
    _CONSUMI = """
        SELECT day, model, source, 1 AS requests, input_tokens, output_tokens,
               cache_creation_tokens, cache_read_tokens, cost_usd,
               baseline_cost_usd, baseline_ingenua_usd,
               costo_modello_richiesto_usd, saved_usd
        FROM usage_events
        UNION ALL
        SELECT day, model, source, requests, input_tokens, output_tokens,
               cache_creation_tokens, cache_read_tokens, cost_usd,
               baseline_cost_usd, baseline_ingenua_usd,
               costo_modello_richiesto_usd, saved_usd
        FROM usage_daily
    """

    async def stats(self) -> dict[str, Any]:
        totals = await self.db.query_one(
            f"""SELECT COALESCE(SUM(requests), 0)            AS requests,
                      COALESCE(SUM(input_tokens), 0)         AS input_tokens,
                      COALESCE(SUM(output_tokens), 0)        AS output_tokens,
                      COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                      COALESCE(SUM(cache_read_tokens), 0)    AS cache_read_tokens,
                      COALESCE(SUM(cost_usd), 0)             AS cost_usd,
                      COALESCE(SUM(baseline_cost_usd), 0)    AS baseline_cost_usd,
                      COALESCE(SUM(baseline_ingenua_usd), 0) AS baseline_ingenua_usd,
                      -- Il costo delle **sole** righe che hanno la baseline
                      -- realistica. Le righe scritte prima che quella colonna
                      -- esistesse hanno zero, e zero li' vuol dire "non
                      -- registrata": sommarne il costo contro una baseline
                      -- assente darebbe un merito del gateway spaventosamente
                      -- negativo il giorno dell'aggiornamento, su traffico
                      -- che non e' cambiato di una virgola.
                      COALESCE(SUM(CASE WHEN baseline_ingenua_usd > 0
                                        THEN cost_usd ELSE 0 END), 0) AS costo_confrontabile,
                      COALESCE(SUM(CASE WHEN baseline_ingenua_usd > 0
                                        THEN requests ELSE 0 END), 0) AS richieste_confrontabili,
                      -- Il risparmio si divide in due meta' che non si
                      -- possono sommare senza dirlo: il pianificatore lascia
                      -- la risposta identica, il declassamento la cambia. Il
                      -- profilo spedito ha il declassamento acceso, mentre i
                      -- numeri pubblicati dal progetto sono misurati con il
                      -- profilo prudente: senza questa colonna un utente
                      -- confronta due cifre che misurano cose diverse.
                      COALESCE(SUM(costo_modello_richiesto_usd), 0)
                          AS costo_modello_richiesto_usd,
                      COALESCE(SUM(CASE WHEN costo_modello_richiesto_usd > 0
                                        THEN requests ELSE 0 END), 0)
                          AS richieste_con_sostituzione,
                      COALESCE(SUM(saved_usd), 0)            AS saved_usd
               FROM ({self._CONSUMI})""",
            pesante=True,
        )
        by_source = await self.db.query(
            f"""SELECT source, SUM(requests) AS requests,
                      COALESCE(SUM(saved_usd), 0) AS saved_usd
               FROM ({self._CONSUMI}) GROUP BY source""",
            pesante=True,
        )
        by_model = await self.db.query(
            f"""SELECT model, SUM(requests) AS requests,
                      COALESCE(SUM(cost_usd), 0)  AS cost_usd,
                      COALESCE(SUM(saved_usd), 0) AS saved_usd
               FROM ({self._CONSUMI}) GROUP BY model ORDER BY cost_usd DESC""",
            pesante=True,
        )
        by_day = await self.db.query(
            f"""SELECT day, SUM(requests) AS requests,
                      COALESCE(SUM(cost_usd), 0)  AS cost_usd,
                      COALESCE(SUM(saved_usd), 0) AS saved_usd
               FROM ({self._CONSUMI}) GROUP BY day ORDER BY day DESC LIMIT 30""",
            pesante=True,
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

    async def cache_write_report(self, limit: int = 2_000) -> dict[str, Any]:
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
            # Prima si prendono le N piu' recenti per `id` - che e' la chiave
            # primaria, quindi il taglio e' immediato - e solo dopo si ordina
            # per sessione. Scritto al contrario, `ORDER BY session_id` doveva
            # ordinare l'intera tabella per poter applicare il LIMIT, e la
            # finestra non serviva a niente: su ventimila eventi la query
            # restava a 552 ms anche chiedendone duemila.
            """SELECT session_id, id, model, cache_ttl,
                      cache_read_tokens, cache_creation_tokens
               FROM (
                   SELECT session_id, id, model, cache_ttl,
                          cache_read_tokens, cache_creation_tokens, ts
                   FROM usage_events
                   WHERE source = ?
                   ORDER BY id DESC
                   LIMIT ?
               )
               ORDER BY session_id, ts, id""",
            (SOURCE_API, limit),
            pesante=True,
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
        conto = audit_cache_writes(eventi).to_dict()
        # La finestra conta: una scrittura fatta prima di essa sembra una
        # scrittura di coda, perche' la rilettura che l'ha ripagata e' fuori
        # dall'orizzonte. Dirlo e' l'unico modo di non far leggere come
        # "sprecato" cio' che e' solo "non piu' visibile".
        conto["finestra"] = len(eventi)
        return conto

    @staticmethod
    def _forma_della_nota(nota: str) -> str:
        """La nota senza i suoi numeri: "2188 token" e "2026 token" sono la stessa cosa.

        Senza questo, ogni nota che cita una quantita' e' una nota diversa, e
        contarle significa contare le richieste una per una. La frase che resta
        e' quella che descrive **cosa** lo stadio ha fatto, che e' la domanda.
        """
        return _NUMERI.sub("N", nota)

    async def stage_activity(self, limit: int = 2_000) -> list[dict[str, Any]]:
        """Quante volte ogni stadio ha fatto qualcosa, sul traffico vero.

        E' la domanda che il progetto si e' imposto di fare **prima** di
        raffinare uno stadio, e finora sapeva rispondere solo sul banco. Il
        conto e' su due denominatori diversi, e la differenza e' tutto il
        punto:

        * `enabled_in` - le richieste in cui lo stadio era acceso;
        * `acted_in`   - quelle in cui ha prodotto almeno una nota.

        Uno stadio acceso su mille richieste e intervenuto su zero non e' uno
        stadio da migliorare: e' uno stadio da capire perche' tace. E' cosi'
        che si e' scoperto che l'effort adattivo veniva spento da un veto sul
        45% del traffico, dopo mesi passati a raffinarne l'euristica.

        Uno stadio che agisce senza scrivere una nota risulta inattivo. E' una
        distorsione nota e si preferisce a quella opposta: contarlo per il
        solo fatto di essere stato chiamato conterebbe tutto, sempre.
        """
        righe = await self.db.query(
            "SELECT stages FROM usage_events ORDER BY id DESC LIMIT ?",
            (limit,),
            pesante=True,
        )
        accesi: Counter[str] = Counter()
        agiti: Counter[str] = Counter()
        campioni: dict[str, Counter[str]] = {}
        # La forma normalizzata serve a contare, non a leggere: "sessione
        # NeeNfaN" non e' una frase. Di ogni forma si tiene un originale da
        # mostrare - il primo incontrato, e siccome le righe arrivano dalla
        # piu' recente, e' quello che descrive lo stato di adesso.
        esempi: dict[str, dict[str, str]] = {}
        registrate = 0
        for riga in righe:
            grezzo = riga["stages"] or ""
            if not grezzo:
                # Riga scritta prima che questa colonna esistesse. Contarla nel
                # denominatore la farebbe sembrare una richiesta in cui nessuno
                # stadio ha fatto niente, che e' una conclusione, non un dato.
                continue
            try:
                dati = json.loads(grezzo)
            except json.JSONDecodeError:
                continue
            registrate += 1
            for nome in dati.get("enabled", []):
                accesi[nome] += 1
            for nome, note in (dati.get("acted") or {}).items():
                agiti[nome] += 1
                campione = campioni.setdefault(nome, Counter())
                esempio = esempi.setdefault(nome, {})
                for nota in note:
                    forma = self._forma_della_nota(nota)
                    campione[forma] += 1
                    esempio.setdefault(forma, nota)

        esito = [
            {
                "stage": nome,
                "enabled_in": totale,
                "acted_in": agiti.get(nome, 0),
                "ratio": agiti.get(nome, 0) / totale if totale else 0.0,
                # Tutte le forme distinte con la loro frequenza, non solo le
                # prime: e' da qui che si ricavano gli avvisi della console, e
                # un conteggio troncato e' un conteggio sbagliato. Normalizzate
                # le forme sono poche - una per cosa che lo stadio sa fare.
                "notes": [
                    [esempi.get(nome, {}).get(forma, forma), quante]
                    for forma, quante in (campioni.get(nome) or Counter()).most_common()
                ],
            }
            for nome, totale in accesi.items()
        ]
        esito.sort(key=lambda voce: (-voce["ratio"], voce["stage"]))
        return [{**voce, "requests_considered": registrate} for voce in esito]

    async def latency_report(self, limit: int = 2_000) -> list[dict[str, Any]]:
        """Quanto ci mette una risposta, separata per provenienza.

        E' l'altra faccia del risparmio: un hit di cache costa zero token, e la
        differenza di latenza rispetto a una chiamata vera dice quanto vale
        anche per chi aspetta. La mediana, non la media: una sola richiesta
        lenta sposta la media e non sposta l'esperienza.
        """
        righe = await self.db.query(
            """SELECT source, latency_ms FROM usage_events
               WHERE latency_ms IS NOT NULL ORDER BY id DESC LIMIT ?""",
            (limit,),
            pesante=True,
        )
        per_fonte: dict[str, list[float]] = {}
        for riga in righe:
            per_fonte.setdefault(riga["source"], []).append(float(riga["latency_ms"]))
        esito = []
        for fonte, valori in per_fonte.items():
            valori.sort()
            esito.append(
                {
                    "source": fonte,
                    "requests": len(valori),
                    "median_ms": median(valori),
                    "p95_ms": valori[min(len(valori) - 1, int(len(valori) * 0.95))],
                }
            )
        esito.sort(key=lambda voce: voce["median_ms"])
        return esito

    async def overhead_report(self) -> dict[str, Any]:
        """I token che il gateway aggiunge di suo, e le chiamate che fa per se'.

        Sono le due voci che un gateway ha interesse a non mostrare: entrambe
        sono costi che nascono qui e che l'utente paga senza averli chiesti.
        """
        riga = await self.db.query_one(
            """SELECT COALESCE(SUM(overhead_tokens), 0) AS overhead_tokens,
                      COALESCE(SUM(aux_cost_usd), 0)    AS aux_cost_usd,
                      COALESCE(SUM(cost_usd), 0)        AS cost_usd,
                      COUNT(*)                          AS requests
               FROM usage_events"""
        )
        dati = dict(riga) if riga else {}
        costo = float(dati.get("cost_usd") or 0)
        dati["aux_ratio"] = (float(dati.get("aux_cost_usd") or 0) / costo) if costo else 0.0
        return dati

    async def profilo_traffico(self) -> dict[str, Any]:
        """I segnali grezzi con cui riconoscere che forma ha il traffico.

        Nessuna colonna nuova: tutto viene da cio' che il registro gia' scrive.
        Il metodo **non interpreta** - restituisce numeri, e la lettura sta in
        `ecotokens/consiglia.py`. La separazione non e' formale: un metodo che
        classificasse renderebbe impossibile guardare i segnali quando la
        classificazione sembra sbagliata.

        Una nota su cosa **non** c'e': l'impronta del prefisso stabile
        (`stable_prefix_hash`) vive solo in memoria, per rispondere a
        "l'abbiamo gia' visto negli ultimi cinque minuti". Non e' sul disco,
        quindi la quota di prefisso condiviso fra sessioni si stima per via
        indiretta - sessioni a turno singolo che rileggono comunque dalla cache
        - ed e' una stima, non una misura.
        """
        totali = await self.db.query_one(
            """SELECT COUNT(*)                                AS richieste,
                      COUNT(DISTINCT session_id)              AS sessioni,
                      COALESCE(AVG(input_tokens + cache_creation_tokens
                                   + cache_read_tokens), 0)   AS prompt_medio,
                      COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens
               FROM usage_events"""
        )
        sessioni = await self.db.query_one(
            """SELECT COUNT(*)                                    AS totali,
                      COALESCE(AVG(turn_count), 0)                AS turni_medi,
                      COALESCE(SUM(CASE WHEN turn_count <= 1
                                        THEN 1 ELSE 0 END), 0)    AS a_turno_singolo
               FROM sessions"""
        )
        per_fonte = await self.db.query(
            "SELECT source, COUNT(*) AS richieste FROM usage_events GROUP BY source"
        )

        dati = dict(totali) if totali else {}
        dati["sessioni_registrate"] = int((sessioni or {})["totali"] or 0) if sessioni else 0
        dati["turni_medi"] = float((sessioni or {})["turni_medi"] or 0) if sessioni else 0.0
        singole = int((sessioni or {})["a_turno_singolo"] or 0) if sessioni else 0
        dati["quota_turno_singolo"] = (
            singole / dati["sessioni_registrate"] if dati["sessioni_registrate"] else 0.0
        )

        richieste = int(dati.get("richieste") or 0)
        da_cache = sum(
            int(riga["richieste"])
            for riga in per_fonte
            if str(riga["source"]) in ("exact_cache", "semantic_cache")
        )
        dati["quota_da_cache"] = da_cache / richieste if richieste else 0.0

        # La potatura del contesto scatta quando ci sono almeno 20.000 token di
        # `tool_result` da potare. E' quindi un indicatore di traffico agentico
        # piu' diretto di qualunque conteggio di token: non misura quanto e'
        # grosso il prompt, ma di **cosa** e' fatto.
        attivita = await self.stage_activity()
        dati["quota_potatura"] = 0.0
        for voce in attivita:
            if voce.get("stage") == "context" and voce.get("enabled_in"):
                dati["quota_potatura"] = voce["acted_in"] / voce["enabled_in"]
                break

        dati["tasso_continuazione"] = await self.tasso_continuazione()
        return dati

    async def recent_events(self, limit: int = 25) -> list[dict[str, Any]]:
        """Le ultime richieste, con cio' che ogni stadio ha fatto a ciascuna."""
        righe = await self.db.query(
            """SELECT id, ts, session_id, model, source, client_format,
                      input_tokens, output_tokens, cache_creation_tokens,
                      cache_read_tokens, cache_ttl, cost_usd, baseline_cost_usd,
                      saved_usd, latency_ms, overhead_tokens, notes, stages
               FROM usage_events ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        eventi = []
        for riga in righe:
            voce = dict(riga)
            for campo, vuoto in (("notes", []), ("stages", {})):
                try:
                    voce[campo] = json.loads(voce[campo] or "null") or vuoto
                except json.JSONDecodeError:
                    voce[campo] = vuoto
            voce["prompt_tokens"] = (
                int(voce["input_tokens"])
                + int(voce["cache_creation_tokens"])
                + int(voce["cache_read_tokens"])
            )
            eventi.append(voce)
        return eventi

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

    async def compatta_consumi(self, keep_detail_days: int) -> dict[str, int]:
        """Aggrega per giorno il dettaglio piu' vecchio, poi lo cancella.

        Il dettaglio - una riga per richiesta, con note e attribuzione per
        stadio - serve a rispondere a "cosa e' successo stamattina". I totali
        servono per sempre. Tenere il primo per rispondere al secondo fa
        crescere il registro senza limite e rallenta ogni pagina che lo legge.

        L'aggregazione precede la cancellazione **nella stessa chiamata**, e i
        totali sono sommati in SQL: cancellare e basta renderebbe falso il
        risparmio storico, che e' esattamente il difetto del metro che questo
        progetto passa il tempo a correggere. Se qualcosa va storto fra le due,
        si perde l'aggregazione e non i dati, che e' il verso giusto.
        """
        confine = (
            datetime.now(timezone.utc) - timedelta(days=max(0, keep_detail_days))
        ).strftime("%Y-%m-%d")

        da_compattare = await self.db.query_one(
            "SELECT COUNT(*) AS n FROM usage_events WHERE day < ?", (confine,)
        )
        quante = int(da_compattare["n"]) if da_compattare else 0
        if not quante:
            return {"compattate": 0, "giorni": 0, "confine": confine}

        giorni = await self.db.query_one(
            "SELECT COUNT(DISTINCT day) AS n FROM usage_events WHERE day < ?", (confine,)
        )
        # ON CONFLICT: un giorno gia' riepilogato puo' ricevere altre righe se
        # la compattazione viene eseguita due volte a distanza di poco.
        await self.db.execute(
            """INSERT INTO usage_daily (day, model, source, requests, input_tokens,
                   output_tokens, cache_creation_tokens, cache_read_tokens,
                   cost_usd, baseline_cost_usd, baseline_ingenua_usd,
                   costo_modello_richiesto_usd, saved_usd)
               SELECT day, model, source, COUNT(*),
                      SUM(input_tokens), SUM(output_tokens),
                      SUM(cache_creation_tokens), SUM(cache_read_tokens),
                      SUM(cost_usd), SUM(baseline_cost_usd),
                      SUM(baseline_ingenua_usd),
                      SUM(costo_modello_richiesto_usd), SUM(saved_usd)
               FROM usage_events WHERE day < ?
               GROUP BY day, model, source
               ON CONFLICT(day, model, source) DO UPDATE SET
                   requests = requests + excluded.requests,
                   input_tokens = input_tokens + excluded.input_tokens,
                   output_tokens = output_tokens + excluded.output_tokens,
                   cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,
                   cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                   cost_usd = cost_usd + excluded.cost_usd,
                   baseline_cost_usd = baseline_cost_usd + excluded.baseline_cost_usd,
                   baseline_ingenua_usd = baseline_ingenua_usd + excluded.baseline_ingenua_usd,
                   costo_modello_richiesto_usd = costo_modello_richiesto_usd
                       + excluded.costo_modello_richiesto_usd,
                   saved_usd = saved_usd + excluded.saved_usd""",
            (confine,),
        )
        await self.db.execute("DELETE FROM usage_events WHERE day < ?", (confine,))
        return {
            "compattate": quante,
            "giorni": int(giorni["n"]) if giorni else 0,
            "confine": confine,
        }

    async def load_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Storico delle misure registrate, dalla piu' recente.

        Sta qui e non nel banco perche' e' una lettura di tabelle: il banco
        importa l'SDK Anthropic, il simulatore e i carichi - 6,7 secondi - e
        chi vuole solo rileggere cio' che e' gia' stato misurato non ha motivo
        di pagarli. Il quadro apriva in 8,4 secondi per questo.
        """
        righe = await self.db.query(
            "SELECT * FROM bench_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        storico: list[dict[str, Any]] = []
        for riga in righe:
            risultati = await self.db.query(
                "SELECT * FROM bench_results WHERE run_id = ?", (riga["id"],)
            )
            storico.append({**dict(riga), "results": [dict(r) for r in risultati]})
        return storico

    async def save_retention(self, esiti: list[Any]) -> None:
        """Registra un giro di misura della ritenzione."""
        adesso = _now()
        await self.db.executemany(
            """INSERT INTO retention_runs
               (created_at, scenario, variant, kept, lost, prompt_tokens, summaries)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    adesso,
                    voce.scenario,
                    voce.variante,
                    len(voce.sopravvissuti),
                    len(voce.perduti),
                    voce.prompt_tokens,
                    voce.riassunti_nuovi,
                )
                for voce in esiti
            ],
        )

    async def latest_retention(self) -> dict[str, Any]:
        """L'ultimo giro registrato, o vuoto se non ne e' mai stato fatto uno.

        Solo l'ultimo: mescolare due giri darebbe medie fra configurazioni
        diverse, che e' il modo classico di ottenere un numero plausibile e
        privo di significato.
        """
        ultimo = await self.db.query_one(
            "SELECT MAX(created_at) AS quando FROM retention_runs"
        )
        quando = ultimo["quando"] if ultimo else None
        if not quando:
            return {}
        righe = await self.db.query(
            """SELECT scenario, variant, kept, lost, prompt_tokens, summaries
               FROM retention_runs WHERE created_at = ? ORDER BY scenario, id""",
            (quando,),
        )
        return {"created_at": quando, "rows": [dict(r) for r in righe]}

    async def stable_facts(self, session_id: str | None, limit: int) -> list[str]:
        """Tutti i fatti della sessione, in ordine fisso di inserimento.

        L'ordine e' la meta' del punto. `search_facts` ordina per pertinenza
        alla domanda, quindi lo stesso insieme puo' uscire in ordine diverso a
        due turni vicini: il blocco cambia, il prefisso cambia, e la cache non
        trova niente. Qui l'ordine e' l'id, che non si muove mai.

        Il taglio prende i **piu' vecchi**, non i piu' recenti: sono i fatti
        gia' presenti nei turni scorsi, quindi tenerli e' cio' che mantiene il
        blocco identico a se stesso. Tagliare dal fondo lo farebbe cambiare a
        ogni fatto nuovo, che e' il difetto che si sta evitando.
        """
        righe = await self.db.query(
            """SELECT text FROM memory_facts WHERE session_id = ?
               ORDER BY id LIMIT ?""",
            (session_id, limit),
        )
        return [riga["text"] for riga in righe]

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
