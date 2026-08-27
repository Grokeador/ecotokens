"""Memoria a lungo termine.

L'idea non e' "ricordare tutto": e' ricordare poco e iniettarlo solo quando
serve. Rimandare l'intera cronologia a ogni turno costa; rimandare otto fatti
pertinenti no.

Due dettagli che decidono se questo stadio fa risparmiare o sprecare:

* i fatti si iniettano **in coda**, dopo l'ultimo breakpoint di cache, mai nel
  system: metterli in testa cambierebbe il prefisso a ogni turno e farebbe
  mancare la cache di tutta la conversazione;
* l'estrazione dei fatti avviene **dopo** aver risposto al client, in
  background, cosi' non aggiunge latenza alla richiesta dell'utente.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..pricing import model_info, resolve_model
from ..tokens import estimate_tokens
from ..wording import EXTRACTION_RULES, MEMORY_CLOSE, MEMORY_OPEN, wrap
from .base import BaseStage, RequestContext

logger = logging.getLogger("ecotokens.memory")

class MemoryStage(BaseStage):
    name = "memory"

    def __init__(self, settings: Any) -> None:
        self.config = settings.memory
        self.enabled = self.config.enabled
        self._tasks: set[asyncio.Task] = set()

    async def before(self, ctx: RequestContext) -> None:
        if ctx.session is None:
            return
        query = _last_user_text(ctx.params)
        if not query.strip():
            return

        facts = await ctx.store.search_facts(
            ctx.session.id, query, self.config.max_facts_injected
        )
        if not facts:
            return

        block = "\n".join(f"- {fact[: self.config.max_fact_chars]}" for fact in facts)
        text = f"<memoria-rilevante>\n{block}\n</memoria-rilevante>"
        _append_in_tail(ctx, text)
        ctx.note(f"{len(facts)} fatti di memoria iniettati in coda")

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        if message is None or ctx.session is None or ctx.source != "api":
            return
        # L'estrazione non deve rallentare la risposta gia' pronta per il client.
        task = asyncio.create_task(self._extract(ctx, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _extract(self, ctx: RequestContext, message: Any) -> None:
        try:
            answer = "".join(
                block.text
                for block in getattr(message, "content", []) or []
                if getattr(block, "type", None) == "text"
            )
            question = _last_user_text(ctx.params)
            if not question.strip() and not answer.strip():
                return

            model = resolve_model(self.config.extraction_model)
            response = await ctx.client.messages.create(
                model=model,
                max_tokens=1_000,
                system=[{"type": "text", "text": EXTRACTION_RULES}],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Utente: {question}\n\nAssistente: {answer}",
                            }
                        ],
                    }
                ],
                output_config={
                    "effort": "low",
                    "format": {
                        "type": "json_schema",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "facts": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["facts"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            facts = _parse_facts(response)
            if not facts:
                return
            known = await ctx.store.existing_facts(ctx.session.id)
            fresh = [fact for fact in facts if fact and fact not in known]
            if fresh:
                await ctx.store.add_facts(ctx.session.id, fresh)
                logger.info("memoria: %d nuovi fatti per la sessione %s", len(fresh), ctx.session.id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Un fallimento qui non deve mai avere effetti sull'utente: la
            # risposta e' gia' stata consegnata.
            logger.warning("estrazione della memoria non riuscita: %s", error)


def _parse_facts(response: Any) -> list[str]:
    text = "".join(
        block.text
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = payload.get("facts", [])
    if not isinstance(payload, list):
        return []
    return [str(item).strip() for item in payload if str(item).strip()]


def _append_in_tail(ctx: RequestContext, text: str) -> None:
    """Aggiunge il testo alla fine della richiesta, senza toccare il prefisso.

    Sui modelli che li supportano si usa un messaggio ``role: "system"``: sta
    dopo la cronologia in cache e non la invalida.
    """
    messages = ctx.params.get("messages") or []
    if not messages:
        return
    if model_info(ctx.model).supports_mid_conversation_system and messages[-1].get("role") == "user":
        messages.append({"role": "system", "content": text})
        return
    last = messages[-1]
    if last.get("role") == "user" and isinstance(last.get("content"), list):
        last["content"].append({"type": "text", "text": text})
    else:
        messages.append({"role": "user", "content": [{"type": "text", "text": text}]})


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
