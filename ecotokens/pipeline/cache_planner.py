"""Piazzamento automatico dei breakpoint ``cache_control``.

E' la leva di risparmio piu' forte del gateway: le riletture di cache costano
0.1x rispetto al prezzo pieno di input. Ma e' anche l'unica che, sbagliata,
fa **aumentare** la spesa, perche' una scrittura costa 1.25x (TTL 5 minuti) o
2x (TTL un'ora). Una scrittura mai riletta e' una perdita netta.

Da qui le tre regole che governano questo stadio:

1. Niente marker se il prefisso non raggiunge la soglia minima del modello:
   sotto quella soglia la cache non si crea e l'API non lo segnala.
2. Il primo turno **scrive** in cache, contrariamente all'intuizione: il
   prefisso piu' grosso (prompt di sistema e tool) e' condiviso anche fra
   conversazioni diverse, quindi quella scrittura viene riletta dalla richiesta
   successiva. Saltarla si e' rivelato piu' costoso, non piu' prudente.
3. Un marker intermedio nei turni lunghi: la finestra di lookback e' di 20
   blocchi, e oltre quella distanza il breakpoint non trova la voce
   precedente e manca la cache in silenzio.
"""

from __future__ import annotations

from typing import Any

from ..pricing import model_info
from ..tokens import estimate_content_tokens, estimate_tools_tokens
from .base import BaseStage, RequestContext

# Distanza massima, in blocchi di contenuto, entro cui un breakpoint riesce a
# trovare la voce di cache precedente.
LOOKBACK_BLOCKS = 20


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Blocchi marcabili di un messaggio, se ce ne sono.

    I messaggi ``role: "system"`` a meta' conversazione hanno contenuto
    testuale e non sono un bersaglio valido per un marker.
    """
    if message.get("role") == "system":
        return None
    content = message.get("content")
    if isinstance(content, list) and content:
        return content
    return None


class CachePlannerStage(BaseStage):
    name = "cache_planner"
    riscrive = True  # Piazza i `cache_control` dentro i blocchi.

    def __init__(self, settings: Any) -> None:
        self.config = settings.cache_planner
        self.enabled = self.config.enabled

    async def before(self, ctx: RequestContext) -> None:
        ctx.cache_ttl = self._choose_ttl(ctx)
        marker = {"type": "ephemeral"}
        if ctx.cache_ttl == "1h":
            marker["ttl"] = "1h"

        # Delega al server: un solo campo in cima, nessun marker sui blocchi.
        # Il breakpoint finisce sull'ultimo blocco memorizzabile e avanza da
        # solo a ogni turno. La soglia minima la controlla il server, che e' il
        # solo a conoscere il conteggio vero dei token.
        if self.config.mode == "automatico":
            ctx.params["cache_control"] = dict(marker)
            ctx.note(f"caching automatico delegato al server, TTL {ctx.cache_ttl}")
            return

        info = model_info(ctx.model)
        minimum = info.cache_min_tokens

        # Comportamento opzionale, spento: ha senso solo quando ogni richiesta
        # ha un prefisso tutto suo, che nessun'altra potra' rileggere.
        if self.config.skip_first_turn and ctx.history_turns == 0:
            ctx.note("nessun breakpoint: primo turno e skip_first_turn attivo")
            return

        messages = ctx.params.get("messages") or []
        prefix_total = (
            estimate_tools_tokens(ctx.params.get("tools"))
            + estimate_content_tokens(ctx.params.get("system"))
            + sum(estimate_content_tokens(message.get("content")) for message in messages)
        )
        if prefix_total < minimum:
            ctx.note(
                f"nessun breakpoint: prompt stimato {prefix_total} token, "
                f"sotto la soglia di {minimum} di {ctx.model}"
            )
            return

        placed = 0
        budget = self.config.max_breakpoints

        # 1. Ultimo blocco system: cattura tools + system insieme, perche' i
        #    tool renderizzano prima del system.
        head_tokens = estimate_tools_tokens(ctx.params.get("tools")) + estimate_content_tokens(
            ctx.params.get("system")
        )
        system = ctx.params.get("system")
        # Il tetto vale anche per il primo marker: senza questo controllo
        # `max_breakpoints = 0` ne piazzava comunque uno, e la configurazione
        # veniva ignorata in silenzio.
        if placed < budget and isinstance(system, list) and system and head_tokens >= minimum:
            system[-1]["cache_control"] = dict(marker)
            placed += 1
            ctx.note(f"breakpoint su system+tools ({head_tokens} token stimati)")

        # 2. Marker intermedi nei turni lunghi, per non superare il lookback.
        placed += self._place_intermediate(ctx, messages, marker, budget - placed - 1)

        # 3. Ultimo blocco dell'ultimo messaggio: rende riutilizzabile l'intera
        #    conversazione alla richiesta successiva.
        if placed < budget:
            for message in reversed(messages):
                blocks = _blocks(message)
                if blocks:
                    blocks[-1]["cache_control"] = dict(marker)
                    placed += 1
                    ctx.note("breakpoint sull'ultimo turno")
                    break

        if placed:
            ctx.note(f"cache TTL {ctx.cache_ttl}, {placed} breakpoint piazzati")

    def _place_intermediate(
        self,
        ctx: RequestContext,
        messages: list[dict[str, Any]],
        marker: dict[str, Any],
        budget: int,
    ) -> int:
        """Marker intermedi quando un solo turno aggiunge troppi blocchi.

        Il caso reale e' il ciclo agentico con molte chiamate parallele: un
        turno puo' aggiungere venti o trenta blocchi tra ``tool_use`` e
        ``tool_result``, e allora il breakpoint finale dista dal marker del
        turno precedente piu' dei 20 blocchi di lookback e non lo trova.

        Si misura quindi la coda aggiunta di recente, non la lunghezza totale
        della conversazione: in una chat normale ogni turno aggiunge pochi
        blocchi e questi marker non servono.
        """
        if budget <= 0:
            return 0

        # Blocchi della coda, dal fondo verso l'inizio. Tre messaggi coprono il
        # delta tipico di un turno: assistant, tool result, nuovo user.
        tail: list[tuple[list[dict[str, Any]], int]] = []
        for message in reversed(messages[-3:]):
            blocks = _blocks(message)
            if not blocks:
                continue
            for index in range(len(blocks) - 1, -1, -1):
                tail.append((blocks, index))

        if len(tail) <= LOOKBACK_BLOCKS:
            return 0

        placed = 0
        step = max(1, self.config.intermediate_every_blocks)
        # Si parte da `step` per non toccare il blocco finale, che riceve gia'
        # il marker dell'ultimo turno.
        for offset in range(step, len(tail), step):
            if placed >= budget:
                break
            blocks, index = tail[offset]
            if "cache_control" in blocks[index]:
                continue
            blocks[index] = {**blocks[index], "cache_control": dict(marker)}
            placed += 1
        if placed:
            ctx.note(
                f"{placed} breakpoint intermedi: il turno aggiunge {len(tail)} blocchi, "
                f"oltre i {LOOKBACK_BLOCKS} di lookback"
            )
        return placed

    def _choose_ttl(self, ctx: RequestContext) -> str:
        """Sceglie tra TTL 5 minuti e 1 ora.

        Il TTL lungo tiene viva la voce attraverso le pause, ma la scrittura
        costa il doppio e servono almeno tre richieste per rientrare. Si adotta
        solo per sessioni gia' lunghe che mostrano pause reali fra un turno e
        l'altro.
        """
        session = ctx.session
        if session is None:
            return "5m"
        if (
            session.turn_count >= self.config.long_ttl_min_turns
            and session.seconds_since_update >= self.config.long_ttl_min_gap_seconds
        ):
            return "1h"
        return "5m"
