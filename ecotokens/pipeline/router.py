"""Scelta di modello ed effort.

Qui c'e' il conflitto piu' interessante del progetto. Le voci di cache sono
legate al modello: cambiare modello a meta' conversazione azzera il prompt
caching, e su una conversazione lunga la cache persa costa piu' di quanto si
risparmi con il modello economico. In piu', scendere a Haiku 4.5 alza la soglia
minima di cache da 512 a 4096 token, disattivandola in silenzio sui prompt medi.

Per questo il router lavora su due livelli, e il primo e' quello che conta:

1. **Abbassare l'effort.** Taglia i token di ragionamento senza cambiare
   modello e senza toccare il prefisso: la cache resta intatta. E' il
   risparmio sicuro, ed e' attivo di default.
2. **Cambiare modello.** Disattivato di default. Quando e' attivo, la scelta si
   fa una volta per sessione e resta ferma, cosi' la cache di quella
   conversazione non viene mai invalidata a meta'.
"""

from __future__ import annotations

from typing import Any

from ..pricing import model_info, resolve_model
from ..tokens import estimate_prompt_tokens, estimate_tokens
from .base import BaseStage, RequestContext

# Indizi che la richiesta non e' banale, anche se e' corta.
COMPLEXITY_HINTS = (
    "spiega perche",
    "dimostra",
    "analizza",
    "progetta",
    "refactor",
    "debug",
    "ottimizza",
    "confronta",
    "passo per passo",
    "step by step",
    "explain why",
    "prove",
    "analyze",
    "design",
    "optimize",
)


class RouterStage(BaseStage):
    name = "router"
    riscrive = True  # Cambia `model` e i parametri di effort.

    def __init__(self, settings: Any) -> None:
        self.config = settings.router
        self.enabled = self.config.enabled

    async def before(self, ctx: RequestContext) -> None:
        self._maybe_downgrade_model(ctx)
        self._maybe_downshift_effort(ctx)

    # -- livello 1: effort -------------------------------------------------

    def _maybe_downshift_effort(self, ctx: RequestContext) -> None:
        if not self.config.effort_downshift:
            return
        # Un effort chiesto esplicitamente dal client non si tocca.
        if ctx.client_effort is not None:
            return
        sempre = self.config.effort_policy == "sempre_basso"
        if not sempre and not self._question_looks_simple(ctx):
            return

        if not sempre and self._tools_in_play(ctx):
            target = self.config.effort_with_tools
            if target == "off":
                ctx.note("effort invariato: la richiesta puo' chiamare un tool")
                return
            motivo = "domanda semplice ma con tool disponibili: si scende a meta' strada"
        else:
            target = self.config.simple_effort
            motivo = (
                "effort_policy = sempre_basso"
                if sempre
                else "richiesta semplice, la cache resta valida"
            )

        output_config = ctx.params.setdefault("output_config", {})
        previous = output_config.get("effort")
        if previous == target:
            return
        output_config["effort"] = target
        ctx.note(f"effort abbassato da {previous} a {target}: {motivo}")

    @staticmethod
    def _tools_in_play(ctx: RequestContext) -> bool:
        """Vero se il modello potrebbe dover scegliere un tool.

        La distinzione che conta non e' "ci sono tool dichiarati" ma "il
        modello deve decidere se e quale usarne". Con ``tool_choice: none`` i
        tool sono dichiarati e inutilizzabili: non c'e' nessuna scelta da fare,
        e trattare quel caso come agentico costava effort senza motivo.
        """
        if not ctx.params.get("tools"):
            return False
        scelta = ctx.params.get("tool_choice")
        if isinstance(scelta, dict) and scelta.get("type") == "none":
            return False
        return True

    def _question_looks_simple(self, ctx: RequestContext) -> bool:
        """Euristica volutamente prudente: nel dubbio, non si abbassa nulla.

        Si guarda la domanda, non il prompt intero. Un system prompt lungo o
        una conversazione ricca non rendono difficile la richiesta: misurare
        tutto il prompt significava non abbassare mai l'effort, perche'
        qualunque contesto reale supera la soglia. E' emerso misurando, non
        leggendo il codice: l'ablazione attribuiva a questo stadio un
        risparmio esattamente pari a zero.
        """
        ctx.estimated_prompt_tokens = ctx.estimated_prompt_tokens or estimate_prompt_tokens(
            ctx.params
        )
        text = _last_user_text(ctx.params)
        if estimate_tokens(text) > self.config.simple_max_question_tokens:
            return False
        if any(hint in text.lower() for hint in COMPLEXITY_HINTS):
            return False
        return True

    def _looks_simple(self, ctx: RequestContext) -> bool:
        """Criterio stretto, usato per la scelta del modello.

        Cambiare modello e' molto piu' invasivo che abbassare l'effort: qui il
        veto in blocco sui tool resta, perche' un modello diverso sceglie i
        tool in modo diverso e l'errore non si paga in token ma in tentativi.
        """
        if self._tools_in_play(ctx):
            return False
        return self._question_looks_simple(ctx)

    # -- livello 2: modello ------------------------------------------------

    def _maybe_downgrade_model(self, ctx: RequestContext) -> None:
        if not self.config.model_downgrade:
            return

        session = ctx.session
        # Una sessione con un modello gia' fissato lo mantiene: e' proprio il
        # punto: cambiarlo a meta' butterebbe via la cache accumulata.
        if session is not None and session.locked_model:
            if session.locked_model != ctx.model:
                self._apply_model(ctx, session.locked_model, "modello fissato per la sessione")
            return

        if self.config.model_locked_per_session and ctx.history_turns > 0:
            ctx.note("modello invariato: conversazione gia' avviata, la cache e' legata al modello")
            return

        target = resolve_model(self.config.downgrade_target)
        if target == ctx.model:
            return
        if self.config.downgrade_policy != "sempre" and not self._looks_simple(ctx):
            return

        current = model_info(ctx.model)
        candidate = model_info(target)
        if candidate.input_per_mtok >= current.input_per_mtok:
            return

        motivo = (
            "declassamento incondizionato (downgrade_policy = sempre)"
            if self.config.downgrade_policy == "sempre"
            else "richiesta semplice all'inizio della sessione"
        )
        self._apply_model(ctx, target, motivo)

    def _apply_model(self, ctx: RequestContext, target: str, reason: str) -> None:
        previous = ctx.model
        ctx.model = target
        ctx.params["model"] = target

        info = model_info(target)
        max_tokens = ctx.params.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > info.max_output:
            ctx.params["max_tokens"] = info.max_output

        ctx.note(f"modello {previous} -> {target}: {reason}")
        if info.cache_min_tokens > model_info(previous).cache_min_tokens:
            ctx.note(
                f"attenzione: {target} richiede almeno {info.cache_min_tokens} token "
                "di prefisso per usare la cache"
            )
        if ctx.session is not None and self.config.model_locked_per_session:
            ctx.session.locked_model = target


def _last_user_text(params: dict[str, Any]) -> str:
    for message in reversed(params.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return ""
