"""Riconoscimento della sessione.

Un client OpenAI rispedisce l'intera cronologia a ogni turno e non conosce il
concetto di sessione: senza risolvere questo, memoria e compattazione sono
impossibili, perche' il gateway non saprebbe mai che due richieste
appartengono alla stessa conversazione.

Il riconoscimento avviene in due passaggi, e servono entrambi:

1. **L'incipit** (system piu' primo messaggio) individua una *famiglia* di
   conversazioni. Non basta da solo: due chat che iniziano con "ciao" hanno lo
   stesso incipit e non sono la stessa conversazione.
2. **Il confronto della cronologia** sceglie quale sessione della famiglia sta
   davvero continuando: quella la cui storia registrata e' un prefisso di
   quella appena arrivata.

Un'impronta calcolata sui primi N messaggi non funzionerebbe, ed e' un errore
facile da commettere: al primo turno la conversazione ha un solo messaggio e al
secondo ne ha tre, quindi le due impronte non coinciderebbero mai.

Chi puo' collaborare manda l'header ``X-EcoTokens-Session`` e salta del tutto
l'euristica.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .base import BaseStage, RequestContext

SESSION_HEADER = "x-ecotokens-session"


def normalize_text(value: Any) -> str:
    """Riduce un contenuto a testo con spaziatura normalizzata."""
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        if value.get("type") == "text":
            return " ".join(str(value.get("text", "")).split())
        return " ".join(
            json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).split()
        )
    if isinstance(value, list):
        return " ".join(part for part in (normalize_text(item) for item in value) if part)
    return " ".join(str(value).split())


def message_signatures(messages: list[dict[str, Any]]) -> list[str]:
    """Firma per messaggio: ruolo e testo normalizzato."""
    return [
        f"{message.get('role', '')}:{normalize_text(message.get('content'))}"
        for message in messages
    ]


def compute_fingerprint(params: dict[str, Any]) -> str:
    """Impronta dell'incipit: system piu' primo messaggio.

    Il modello non entra nell'impronta: il router puo' cambiarlo e la
    conversazione resta la stessa.
    """
    messages = params.get("messages") or []
    payload = {
        "system": normalize_text(params.get("system")),
        "first": message_signatures(messages[:1]),
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]


def count_history_turns(messages: list[dict[str, Any]]) -> int:
    """Turni assistant gia' presenti nella cronologia in arrivo."""
    return sum(1 for message in messages if message.get("role") == "assistant")


def is_prefix(stored: list[str], incoming: list[str]) -> bool:
    """Vero se la cronologia registrata e' l'inizio di quella in arrivo."""
    if not stored or len(stored) > len(incoming):
        return False
    return incoming[: len(stored)] == stored


class SessionStage(BaseStage):
    name = "session"

    def __init__(self, enabled: bool, depth: int, ttl_hours: int) -> None:
        self.enabled = enabled
        # Quante sessioni della stessa famiglia confrontare al massimo.
        self.max_candidates = max(4, depth * 4)
        self.ttl_hours = ttl_hours

    async def before(self, ctx: RequestContext) -> None:
        messages = ctx.params.get("messages") or []
        ctx.history_turns = count_history_turns(messages)
        ctx.incoming_signature = message_signatures(messages)

        explicit = _explicit_session_id(ctx)
        ctx.fingerprint = explicit or compute_fingerprint(ctx.params)

        session = await self._match_existing(ctx)
        if session is not None:
            ctx.session = session
            ctx.session_is_new = False
            return

        ctx.session = await ctx.store.create_session(ctx.fingerprint, ctx.model)
        ctx.session_is_new = True
        if ctx.history_turns > 0:
            # Conversazione gia' avviata che non corrisponde a nulla di noto:
            # capita quando il client riscrive la cronologia, per esempio dopo
            # la modifica di un messaggio precedente.
            ctx.note("nuova sessione: la cronologia non combacia con quelle registrate")

    async def _match_existing(self, ctx: RequestContext):
        candidates = await ctx.store.find_sessions(ctx.fingerprint, self.ttl_hours)
        best = None
        best_length = 0
        for candidate in candidates[: self.max_candidates]:
            stored = await ctx.store.message_signatures(candidate.id)
            if is_prefix(stored, ctx.incoming_signature) and len(stored) > best_length:
                best, best_length = candidate, len(stored)
        if best is not None:
            ctx.note(f"continuazione della sessione {best.id} ({best_length} messaggi noti)")
        return best

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        if ctx.session is None:
            return
        await ctx.store.touch_session(
            ctx.session.id,
            message_count=len(ctx.incoming_signature),
            locked_model=ctx.session.locked_model,
        )
        await ctx.store.save_messages(
            ctx.session.id,
            ctx.params.get("messages") or [],
            ctx.incoming_signature,
            store_content=ctx.settings.storage.store_message_content,
        )


def _explicit_session_id(ctx: RequestContext) -> str | None:
    """Legge l'header di sessione, se il client ne ha mandato uno."""
    value = ctx.headers.get(SESSION_HEADER)
    if value:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]
    return None
