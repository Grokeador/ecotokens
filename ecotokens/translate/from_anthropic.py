"""Traduzione Anthropic -> OpenAI per le risposte non in streaming."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..pricing import Usage

# I motivi di arresto dell'API Claude, mappati sui valori che i client OpenAI
# sanno interpretare.
FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def finish_reason(stop_reason: str | None) -> str:
    return FINISH_REASONS.get(stop_reason or "", "stop")


def usage_payload(usage: Usage, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Blocco ``usage`` in formato OpenAI.

    ``prompt_tokens`` e' la dimensione reale del prompt, cioe' la somma dei tre
    contatori di input: usare il solo ``input_tokens`` dell'API Claude
    riporterebbe numeri assurdamente bassi appena la cache entra in funzione,
    perche' quel campo e' il residuo non servito da cache.
    """
    payload: dict[str, Any] = {
        "prompt_tokens": usage.total_prompt_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.total_prompt_tokens + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cache_read_tokens},
    }
    if extra:
        payload.update(extra)
    return payload


def extract_blocks(content: list[Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Estrae (testo, ragionamento, tool_calls) dai blocchi di una risposta."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in content or []:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type == "text":
            text_parts.append(_get(block, "text") or "")
        elif block_type == "thinking":
            thinking_parts.append(_get(block, "thinking") or "")
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": _get(block, "id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": _get(block, "name") or "",
                        "arguments": json.dumps(
                            _get(block, "input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
    return "".join(text_parts), "".join(thinking_parts), tool_calls


def _get(block: Any, attribute: str) -> Any:
    if isinstance(block, dict):
        return block.get(attribute)
    return getattr(block, attribute, None)


def to_openai_response(
    message: Any,
    *,
    model: str,
    usage: Usage,
    completion_id: str | None = None,
    ecotokens_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Converte un ``Message`` dell'SDK in una chat completion OpenAI."""
    text, thinking, tool_calls = extract_blocks(getattr(message, "content", []) or [])
    stop_reason = getattr(message, "stop_reason", None)

    chat_message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        chat_message["tool_calls"] = tool_calls
    if thinking:
        # Campo non standard, ignorato dai client che non lo conoscono e usato
        # da quelli che sanno mostrare il ragionamento.
        chat_message["reasoning_content"] = thinking

    response: dict[str, Any] = {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        # Si dichiara il modello davvero usato, non quello richiesto: se il
        # router ha cambiato modello, il chiamante deve poterlo vedere.
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": chat_message,
                "finish_reason": finish_reason(stop_reason),
                "logprobs": None,
            }
        ],
        "usage": usage_payload(usage),
    }

    stop_details = getattr(message, "stop_details", None)
    if stop_reason == "refusal" and stop_details is not None:
        response["choices"][0]["ecotokens_refusal"] = {
            "category": _get(stop_details, "category"),
            "explanation": _get(stop_details, "explanation"),
        }
    if ecotokens_meta:
        response["ecotokens"] = ecotokens_meta
    return response


def cached_response_copy(
    response: dict[str, Any], *, ecotokens_meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Ricopia una risposta salvata in cache dandole identita' e ora nuove."""
    copy = json.loads(json.dumps(response))
    copy["id"] = new_completion_id()
    copy["created"] = int(time.time())
    if ecotokens_meta:
        copy["ecotokens"] = ecotokens_meta
    return copy
