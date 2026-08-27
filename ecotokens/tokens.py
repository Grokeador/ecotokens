"""Conteggio dei token.

Due percorsi distinti, con scopi diversi:

* ``estimate_tokens`` e' una stima locale euristica, gratuita e istantanea,
  usata solo per decisioni interne (soglie di cache, trigger di compattazione,
  classificazione della difficolta').
* ``TokenCounter.count`` chiama ``messages.count_tokens``, che e' esatto e
  specifico per modello. Serve per i preventivi di budget, dove sbagliare
  costa davvero.

Non viene usato ``tiktoken``: e' il tokenizer di OpenAI e sottostima Claude del
15-20% sul testo comune, molto di piu' su codice o lingue non inglesi. Una
stima sbagliata qui significa superare la finestra di contesto senza accorgersene.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

# Caratteri per token, ricavati come approssimazione prudente del tokenizer
# Claude. Prudente = tende a sovrastimare, cosi' le soglie scattano prima.
_CHARS_PER_TOKEN = 3.6
# Ogni blocco di contenuto e ogni messaggio portano un piccolo overhead fisso.
_BLOCK_OVERHEAD = 4
_MESSAGE_OVERHEAD = 8
# Le immagini si stimano dalla dimensione del base64, non dai caratteri.
_IMAGE_BASE64_CHARS_PER_TOKEN = 750


def estimate_tokens(text: str) -> int:
    """Stima locale dei token di una stringa. Approssimata per eccesso."""
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN) + 1)


def estimate_content_tokens(content: Any) -> int:
    """Stima di un contenuto Anthropic: stringa o lista di blocchi."""
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, dict):
        return _estimate_block(content)
    if isinstance(content, list):
        return sum(estimate_content_tokens(item) for item in content)
    return estimate_tokens(str(content))


def _estimate_block(block: dict[str, Any]) -> int:
    block_type = block.get("type")
    if block_type == "text":
        return estimate_tokens(str(block.get("text", ""))) + _BLOCK_OVERHEAD
    if block_type == "image":
        source = block.get("source") or {}
        data = source.get("data")
        if isinstance(data, str):
            return int(len(data) / _IMAGE_BASE64_CHARS_PER_TOKEN) + _BLOCK_OVERHEAD
        # Immagine per URL: non conoscibile in anticipo, stima prudente.
        return 1_600
    if block_type == "tool_use":
        payload = json.dumps(block.get("input", {}), ensure_ascii=False, sort_keys=True)
        return estimate_tokens(str(block.get("name", "")) + payload) + _BLOCK_OVERHEAD
    if block_type == "tool_result":
        return estimate_content_tokens(block.get("content")) + _BLOCK_OVERHEAD
    if block_type == "thinking":
        return estimate_tokens(str(block.get("thinking", ""))) + _BLOCK_OVERHEAD
    if block_type == "document":
        source = block.get("source") or {}
        data = source.get("data")
        if isinstance(data, str):
            return int(len(data) / _IMAGE_BASE64_CHARS_PER_TOKEN) + _BLOCK_OVERHEAD
        return _BLOCK_OVERHEAD
    # Blocco sconosciuto: si stima la sua serializzazione.
    return estimate_tokens(json.dumps(block, ensure_ascii=False, sort_keys=True))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(
        estimate_content_tokens(message.get("content")) + _MESSAGE_OVERHEAD
        for message in messages
    )


def estimate_tools_tokens(tools: list[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, ensure_ascii=False, sort_keys=True))


def estimate_prompt_tokens(params: dict[str, Any]) -> int:
    """Stima dell'intero prompt: tools + system + messages, nell'ordine di render."""
    return (
        estimate_tools_tokens(params.get("tools"))
        + estimate_content_tokens(params.get("system"))
        + estimate_messages_tokens(params.get("messages") or [])
    )


def prefix_tokens_upto(params: dict[str, Any], message_index: int) -> int:
    """Token del prefisso fino al messaggio indicato (escluso).

    Serve al cache planner per sapere se un breakpoint supera la soglia minima
    del modello: sotto soglia la cache non si crea e l'API tace.
    """
    messages = params.get("messages") or []
    return (
        estimate_tools_tokens(params.get("tools"))
        + estimate_content_tokens(params.get("system"))
        + estimate_messages_tokens(messages[:message_index])
    )


class TokenCounter:
    """Conteggio esatto via API, con memoizzazione dei prompt gia' visti."""

    def __init__(self, client: Any, max_entries: int = 512) -> None:
        self._client = client
        self._cache: OrderedDict[str, int] = OrderedDict()
        self._max_entries = max_entries

    @staticmethod
    def _key(model: str, params: dict[str, Any]) -> str:
        payload = json.dumps(
            {
                "model": model,
                "system": params.get("system"),
                "tools": params.get("tools"),
                "messages": params.get("messages"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def count(self, model: str, params: dict[str, Any]) -> int:
        """Token di input esatti. Ripiega sulla stima locale se l'API fallisce."""
        key = self._key(model, params)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        request: dict[str, Any] = {
            "model": model,
            "messages": strip_cache_control(params.get("messages") or []),
        }
        system = params.get("system")
        if system:
            request["system"] = strip_cache_control(system)
        tools = params.get("tools")
        if tools:
            request["tools"] = strip_cache_control(tools)

        try:
            response = await self._client.messages.count_tokens(**request)
            total = int(response.input_tokens)
        except Exception:
            total = estimate_prompt_tokens(params)

        self._cache[key] = total
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return total


def strip_cache_control(value: Any) -> Any:
    """Rimuove i marker cache_control da una struttura annidata.

    ``count_tokens`` non deve vedere i breakpoint: contano i token, non la
    strategia di cache, e includerli cambierebbe la chiave di memoizzazione a
    ogni riposizionamento dei marker.
    """
    if isinstance(value, dict):
        return {
            key: strip_cache_control(item)
            for key, item in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [strip_cache_control(item) for item in value]
    return value
