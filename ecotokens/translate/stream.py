"""Traduzione dello streaming: eventi Anthropic -> chunk SSE OpenAI.

Gli eventi dell'API Claude sono per blocco di contenuto; lo streaming OpenAI e'
per delta di messaggio. La differenza che conta e' nei tool call: Claude apre un
blocco ``tool_use`` e poi ne riempie l'input a pezzi di JSON, mentre OpenAI
vuole un indice progressivo dentro l'array ``tool_calls``. I due indici non
coincidono e vanno mappati.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from ..pricing import Usage
from .from_anthropic import finish_reason, usage_payload

DONE = "data: [DONE]\n\n"


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class StreamTranslator:
    """Converte gli eventi di un turno in chunk OpenAI, accumulando l'usage."""

    def __init__(self, *, completion_id: str, model: str, include_usage: bool) -> None:
        self.completion_id = completion_id
        self.model = model
        self.include_usage = include_usage
        self.created = int(time.time())
        self.usage = Usage()
        self.stop_reason: str | None = None
        # blocco di contenuto -> indice nell'array tool_calls di OpenAI
        self._tool_slots: dict[int, int] = {}
        self._next_tool_slot = 0
        self._role_sent = False

    # -- costruzione dei chunk -------------------------------------------

    def _chunk(self, delta: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
        return {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }

    def open_chunk(self) -> str:
        self._role_sent = True
        return sse(self._chunk({"role": "assistant", "content": ""}))

    def final_chunk(self) -> str:
        return sse(self._chunk({}, finish_reason(self.stop_reason)))

    def usage_chunk(self, meta: dict[str, Any] | None = None) -> str:
        payload = {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [],
            "usage": usage_payload(self.usage),
        }
        if meta:
            payload["ecotokens"] = meta
        return sse(payload)

    # -- gestione degli eventi -------------------------------------------

    def handle(self, event: Any) -> list[str]:
        """Traduce un evento dell'SDK in zero o piu' chunk SSE."""
        event_type = getattr(event, "type", None)
        if event_type == "message_start":
            self._absorb_usage(getattr(getattr(event, "message", None), "usage", None))
            return []
        if event_type == "content_block_start":
            return self._on_block_start(event)
        if event_type == "content_block_delta":
            return self._on_block_delta(event)
        if event_type == "message_delta":
            reason = getattr(getattr(event, "delta", None), "stop_reason", None)
            if reason:
                self.stop_reason = reason
            self._absorb_usage(getattr(event, "usage", None))
            return []
        return []

    def _on_block_start(self, event: Any) -> list[str]:
        block = getattr(event, "content_block", None)
        if getattr(block, "type", None) != "tool_use":
            return []
        block_index = int(getattr(event, "index", 0))
        slot = self._next_tool_slot
        self._tool_slots[block_index] = slot
        self._next_tool_slot += 1
        return [
            sse(
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": slot,
                                "id": getattr(block, "id", None) or f"call_{slot}",
                                "type": "function",
                                "function": {
                                    "name": getattr(block, "name", "") or "",
                                    "arguments": "",
                                },
                            }
                        ]
                    }
                )
            )
        ]

    def _on_block_delta(self, event: Any) -> list[str]:
        delta = getattr(event, "delta", None)
        delta_type = getattr(delta, "type", None)

        if delta_type == "text_delta":
            text = getattr(delta, "text", "") or ""
            return [sse(self._chunk({"content": text}))] if text else []

        if delta_type == "thinking_delta":
            thinking = getattr(delta, "thinking", "") or ""
            return [sse(self._chunk({"reasoning_content": thinking}))] if thinking else []

        if delta_type == "input_json_delta":
            partial = getattr(delta, "partial_json", "") or ""
            if not partial:
                return []
            slot = self._tool_slots.get(int(getattr(event, "index", 0)), 0)
            return [
                sse(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": slot,
                                    "function": {"arguments": partial},
                                }
                            ]
                        }
                    )
                )
            ]
        return []

    def _absorb_usage(self, usage: Any) -> None:
        """Accumula l'usage: arriva spezzato tra message_start e message_delta."""
        if usage is None:
            return

        def value(attribute: str, current: int) -> int:
            raw = getattr(usage, attribute, None)
            return int(raw) if raw else current

        self.usage = Usage(
            input_tokens=value("input_tokens", self.usage.input_tokens),
            output_tokens=value("output_tokens", self.usage.output_tokens),
            cache_creation_tokens=value(
                "cache_creation_input_tokens", self.usage.cache_creation_tokens
            ),
            cache_read_tokens=value(
                "cache_read_input_tokens", self.usage.cache_read_tokens
            ),
        )


async def replay_response_as_stream(
    response: dict[str, Any], *, include_usage: bool, chunk_size: int = 24
) -> AsyncIterator[str]:
    """Rende in streaming una risposta gia' completa (hit di cache).

    Un client che ha chiesto ``stream: true`` deve ricevere uno stream anche
    quando la risposta arriva dalla cache, altrimenti la richiesta fallisce nel
    parser del client invece che nel gateway.
    """
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    completion_id = response.get("id", "chatcmpl-cached")
    model = response.get("model", "")
    created = response.get("created", int(time.time()))

    def chunk(delta: dict[str, Any], reason: str | None = None) -> str:
        return sse(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
        )

    yield chunk({"role": "assistant", "content": ""})

    content = message.get("content") or ""
    for start in range(0, len(content), chunk_size):
        yield chunk({"content": content[start : start + chunk_size]})

    for slot, call in enumerate(message.get("tool_calls") or []):
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": slot,
                        "id": call.get("id"),
                        "type": "function",
                        "function": {
                            "name": (call.get("function") or {}).get("name", ""),
                            "arguments": (call.get("function") or {}).get("arguments", ""),
                        },
                    }
                ]
            }
        )

    yield chunk({}, choice.get("finish_reason") or "stop")

    if include_usage:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": response.get("usage", {}),
        }
        if "ecotokens" in response:
            payload["ecotokens"] = response["ecotokens"]
        yield sse(payload)

    yield DONE
