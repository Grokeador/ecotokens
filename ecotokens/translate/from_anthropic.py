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


class _Vista:
    """Accesso per attributi a una risposta gia' ridotta a dizionario.

    Serve a riusare ``to_openai_response`` su una risposta ripresa dalla cache:
    quella funzione legge attributi perche' nasce per gli oggetti dell'SDK, e
    duplicarla per i dizionari significherebbe mantenerne due copie che con il
    tempo divergono.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __getattr__(self, nome: str) -> Any:
        return self._payload.get(nome)


def openai_response_from_dict(
    payload: dict[str, Any],
    *,
    model: str,
    usage: Usage,
    ecotokens_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Converte una risposta Anthropic in forma di dizionario nel formato OpenAI."""
    return to_openai_response(
        _Vista(payload), model=model, usage=usage, ecotokens_meta=ecotokens_meta
    )


def native_response_copy(
    response: dict[str, Any], *, ecotokens_meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Ricopia una risposta nativa salvata in cache, dandole identita' nuova."""
    copy = json.loads(json.dumps(response))
    copy.pop("ecotokens", None)
    copy["id"] = f"msg_{uuid.uuid4().hex[:24]}"
    if ecotokens_meta:
        copy["ecotokens"] = ecotokens_meta
    return copy


def to_plain_dict(oggetto: Any) -> dict[str, Any]:
    """Riduce una risposta dell'SDK a JSON puro.

    Serve a due cose che devono restare d'accordo: cio' che si salva in cache e
    cio' che si restituisce a un client nativo. Il metodo esatto varia con la
    versione dell'SDK, quindi si prova quello che c'e' invece di fissarne uno.
    """
    if oggetto is None:
        return {}
    if isinstance(oggetto, dict):
        return oggetto
    for metodo in ("model_dump", "to_dict", "dict"):
        funzione = getattr(oggetto, metodo, None)
        if callable(funzione):
            try:
                return json.loads(json.dumps(funzione(mode="json"), default=str))
            except TypeError:
                return json.loads(json.dumps(funzione(), default=str))
    return json.loads(json.dumps(oggetto, default=str))
