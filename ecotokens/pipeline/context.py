"""Gestione della finestra di contesto.

Due meccanismi, applicati in quest'ordine perche' hanno costi molto diversi.

**Potatura lato server** (``context_management``): elimina dalla richiesta i
vecchi ``tool_result`` e, se richiesto, i blocchi di pensiero. Non costa
chiamate aggiuntive, non riscrive nulla di quello che l'utente ha detto ed e'
quindi il primo strumento da usare. In un ciclo agentico i tool result sono
quasi sempre la voce di spesa piu' grossa del prompt.

**Riassunto locale**: quando neanche la potatura basta, la parte vecchia della
conversazione viene sostituita da un riassunto prodotto da un modello
economico. Il punto delicato e' che il riassunto viene **calcolato una volta
sola e poi riusato alla lettera**: riassumere di nuovo a ogni turno cambierebbe
il prefisso del prompt e farebbe mancare la cache a ogni richiesta, spendendo
piu' di quanto la compattazione fa risparmiare.

Nota sulla compattazione server-side dell'API (``compact_20260112``): non e'
usata qui perche' richiede di riaccodare i blocchi di compattazione a ogni
turno successivo, mentre un client OpenAI rispedisce la propria cronologia e
quei blocchi andrebbero persi al primo giro.
"""

from __future__ import annotations

import logging
from typing import Any

from ..pricing import model_info, resolve_model
from ..tokens import estimate_content_tokens, estimate_prompt_tokens
from .base import BaseStage, RequestContext

logger = logging.getLogger("ecotokens.context")

CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"

SUMMARY_PROMPT = (
    "Riassumi la conversazione qui sotto in un massimo di 15 punti elenco. "
    "Conserva: decisioni prese, vincoli, dati concreti (nomi, numeri, percorsi di file), "
    "e le richieste ancora aperte. Ometti convenevoli e ripetizioni. "
    "Rispondi solo con il riassunto, senza introduzioni."
)


class ContextStage(BaseStage):
    name = "context"

    def __init__(self, settings: Any) -> None:
        self.config = settings.context
        self.enabled = self.config.enabled

    async def before(self, ctx: RequestContext) -> None:
        info = model_info(ctx.model)
        window = info.context_window
        estimated = estimate_prompt_tokens(ctx.params)
        ctx.estimated_prompt_tokens = estimated
        ratio = estimated / window if window else 0.0

        if ratio < self.config.trigger_ratio:
            return

        self._apply_server_pruning(ctx, estimated, window)

        if self.config.local_compaction and ratio >= self.config.hard_ratio:
            await self._apply_local_summary(ctx, window)

    # -- potatura lato server ---------------------------------------------

    def _apply_server_pruning(self, ctx: RequestContext, estimated: int, window: int) -> None:
        edits: list[dict[str, Any]] = []
        if self.config.clear_tool_uses:
            edits.append({"type": "clear_tool_uses_20250919"})
        if self.config.clear_thinking:
            edits.append({"type": "clear_thinking_20251015"})
        if not edits:
            return

        ctx.params["context_management"] = {"edits": edits}
        ctx.use_beta(CONTEXT_MANAGEMENT_BETA)
        ctx.note(
            f"potatura lato server attiva: prompt stimato {estimated} token su "
            f"{window} di finestra"
        )

    # -- riassunto locale --------------------------------------------------

    async def _apply_local_summary(self, ctx: RequestContext, window: int) -> None:
        messages = ctx.params.get("messages") or []
        keep = self.config.keep_recent_messages
        # Serve materiale sufficiente perche' riassumere abbia senso.
        if len(messages) <= keep + 2:
            return

        cut = len(messages) - keep
        # Il taglio non puo' cadere in mezzo a una coppia tool_use/tool_result:
        # un tool_result orfano fa fallire la richiesta con un 400.
        cut = _safe_cut_point(messages, cut)
        if cut <= 1:
            return

        summary = None
        if ctx.session is not None:
            summary = await ctx.store.get_summary(ctx.session.id, cut)

        if summary is None:
            summary = await self._summarize(ctx, messages[:cut])
            if summary is None:
                return
            if ctx.session is not None:
                await ctx.store.put_summary(ctx.session.id, cut, summary)
            ctx.note(f"riassunti i primi {cut} messaggi con {self.config.summary_model}")
        else:
            ctx.note(f"riusato il riassunto dei primi {cut} messaggi: prefisso invariato")

        before_tokens = estimate_content_tokens(
            [message.get("content") for message in messages[:cut]]
        )
        replacement = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<riassunto-conversazione-precedente>\n{summary}\n"
                        "</riassunto-conversazione-precedente>",
                    }
                ],
            }
        ]
        ctx.params["messages"] = replacement + messages[cut:]
        after_tokens = estimate_content_tokens(replacement[0]["content"])
        ctx.note(
            f"compattazione: {before_tokens} token di cronologia sostituiti da {after_tokens}"
        )

    async def _summarize(self, ctx: RequestContext, messages: list[dict[str, Any]]) -> str | None:
        """Chiama il modello economico per riassumere la parte vecchia.

        E' una fork della richiesta principale e non riusa la cache del padre:
        gira su un altro modello, e le voci di cache sono legate al modello.
        E' accettabile perche' succede una volta per punto di taglio, non a
        ogni turno.
        """
        transcript = _render_transcript(messages)
        if not transcript.strip():
            return None
        model = resolve_model(self.config.summary_model)
        try:
            response = await ctx.client.messages.create(
                model=model,
                max_tokens=2_000,
                system=[{"type": "text", "text": SUMMARY_PROMPT}],
                messages=[{"role": "user", "content": [{"type": "text", "text": transcript}]}],
                output_config={"effort": "low"},
            )
        except Exception as error:  # la compattazione non deve far fallire la richiesta
            logger.warning("riassunto non riuscito, si prosegue senza: %s", error)
            ctx.note("riassunto non riuscito: la conversazione resta integrale")
            return None

        parts = [
            block.text
            for block in getattr(response, "content", []) or []
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts).strip() or None


def _safe_cut_point(messages: list[dict[str, Any]], cut: int) -> int:
    """Sposta il taglio in modo da non separare un tool_use dal suo risultato."""
    while cut > 1 and _has_orphan_tool_result(messages[cut:]):
        cut -= 1
    return cut


def _has_orphan_tool_result(tail: list[dict[str, Any]]) -> bool:
    """Vero se la coda inizia con risultati di tool la cui chiamata resta fuori."""
    if not tail:
        return False
    first = tail[0]
    content = first.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _render_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            pieces = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    pieces.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    pieces.append(f"[chiamata a {block.get('name')}]")
                elif block.get("type") == "tool_result":
                    pieces.append("[risultato di tool]")
            text = " ".join(pieces)
        else:
            text = ""
        if text.strip():
            lines.append(f"{role}: {text.strip()}")
    return "\n".join(lines)
