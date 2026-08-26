"""Traduzione OpenAI -> Anthropic, con sanificazione dei parametri.

Questo e' il file piu' delicato del gateway. I client OpenAI mandano parametri
che i modelli Claude attuali **rifiutano con un 400** (``temperature``,
``top_p``, il prefill dell'ultimo messaggio assistant): se non vengono
rimossi qui, ogni richiesta fallisce.

C'e' anche un secondo livello di attenzione, invisibile e piu' insidioso: il
prompt caching e' un match di prefisso, e i ``tools`` renderizzano in posizione
0. Serializzarli in ordine non deterministico invalida la cache di ogni
richiesta senza che nulla segnali il problema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..api.schemas import ChatCompletionRequest, ChatMessage
from ..config import Settings
from ..pricing import model_info, resolve_model

# Parametri di campionamento rimossi dai modelli Claude attuali: inviarli
# produce un 400. Vengono scartati e registrati.
UNSUPPORTED_SAMPLING = (
    "temperature",
    "top_p",
    "top_k",
    "frequency_penalty",
    "presence_penalty",
    "logprobs",
    "seed",
)

SYSTEM_ROLES = {"system", "developer"}


@dataclass
class Translation:
    """Risultato della traduzione."""

    model: str
    params: dict[str, Any]
    # Decisioni prese, mostrate in /admin/stats e nei log a livello debug.
    notes: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    # Nomi dei tool nell'ordine originale del client: serve a ricostruire
    # la risposta senza sorprendere il chiamante.
    tool_names: list[str] = field(default_factory=list)


def build_anthropic_params(
    request: ChatCompletionRequest, settings: Settings
) -> Translation:
    """Costruisce i parametri per ``messages.create`` da una richiesta OpenAI."""
    model = resolve_model(request.model, settings.upstream.default_model)
    info = model_info(model)
    result = Translation(model=model, params={})

    system_blocks, messages = _split_messages(request.messages, model, result)
    messages = _drop_trailing_prefill(messages, result)
    messages = _ensure_leading_user(messages, result)

    params: dict[str, Any] = {"model": model, "messages": messages}
    if system_blocks:
        params["system"] = system_blocks

    # max_tokens: mai lasciarlo indefinito, mai oltre il tetto del modello.
    default_max = (
        settings.upstream.default_max_tokens_stream
        if request.stream
        else settings.upstream.default_max_tokens
    )
    max_tokens = request.resolved_max_tokens() or default_max
    if max_tokens > info.max_output:
        result.notes.append(
            f"max_tokens ridotto da {max_tokens} a {info.max_output} (tetto del modello)"
        )
        max_tokens = info.max_output
    params["max_tokens"] = max_tokens

    if settings.upstream.adaptive_thinking:
        params["thinking"] = {
            "type": "adaptive",
            "display": settings.upstream.thinking_display,
        }

    output_config: dict[str, Any] = {
        "effort": request.reasoning_effort or settings.upstream.default_effort
    }
    response_format = _translate_response_format(request, messages, result)
    if response_format is not None:
        output_config["format"] = response_format
    params["output_config"] = output_config

    tools = _translate_tools(request, result)
    if tools:
        params["tools"] = tools
    tool_choice = _translate_tool_choice(request, bool(tools), result)
    if tool_choice is not None:
        params["tool_choice"] = tool_choice

    if request.stop:
        params["stop_sequences"] = (
            [request.stop] if isinstance(request.stop, str) else list(request.stop)
        )

    if request.user:
        # metadata non fa parte del prefisso del prompt: nessun effetto sulla cache.
        params["metadata"] = {"user_id": str(request.user)}

    _record_dropped_params(request, result)
    result.params = params
    return result


# --- messaggi ------------------------------------------------------------


def _split_messages(
    messages: list[ChatMessage], model: str, result: Translation
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separa i system iniziali dal resto e converte ogni messaggio."""
    system_blocks: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    supports_mid_system = model_info(model).supports_mid_conversation_system
    index = 0

    # I system in testa diventano il campo `system` top-level: e' l'inizio del
    # prefisso, la parte che deve restare identica byte per byte.
    while index < len(messages) and messages[index].role in SYSTEM_ROLES:
        text = _content_to_text(messages[index].content)
        if text:
            system_blocks.append({"type": "text", "text": text})
        index += 1

    for message in messages[index:]:
        role = message.role
        if role in SYSTEM_ROLES:
            _append_mid_system(converted, message, supports_mid_system, result)
        elif role in {"tool", "function"}:
            _append_tool_result(converted, message)
        elif role == "assistant":
            converted.append(_assistant_message(message))
        else:
            converted.append({"role": "user", "content": _content_to_blocks(message.content)})

    return system_blocks, converted


def _append_mid_system(
    converted: list[dict[str, Any]],
    message: ChatMessage,
    supports_mid_system: bool,
    result: Translation,
) -> None:
    """Istruzione operativa a meta' conversazione.

    Sui modelli che la supportano resta un messaggio ``role: "system"`` dentro
    ``messages[]``: sta dopo la cronologia gia' in cache, quindi non invalida
    nulla, ed e' un canale non falsificabile. Altrove degrada a testo marcato
    dentro un turno user, che ha lo stesso profilo di cache ma nessuna autorita'.
    """
    text = _content_to_text(message.content)
    if not text:
        return
    # Non puo' essere il primo elemento e deve seguire un turno user.
    if supports_mid_system and converted and converted[-1].get("role") == "user":
        converted.append({"role": "system", "content": text})
        return
    if supports_mid_system and not converted:
        result.notes.append(
            "system a meta' conversazione spostato in un turno user: non puo' essere messages[0]"
        )
    elif not supports_mid_system:
        result.notes.append(
            "system a meta' conversazione degradato a testo: modello senza supporto"
        )
    converted.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"<operator-instruction>\n{text}\n</operator-instruction>"}
            ],
        }
    )


def _append_tool_result(converted: list[dict[str, Any]], message: ChatMessage) -> None:
    """I tool result vanno in un turno user, e piu' risultati vanno accorpati.

    Spezzarli su piu' messaggi insegna al modello a non emettere piu' chiamate
    in parallelo, che e' esattamente il contrario di quello che si vuole.
    """
    block = {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id or message.name or "unknown",
        "content": _content_to_blocks(message.content) or [{"type": "text", "text": ""}],
    }
    if converted and converted[-1].get("role") == "user":
        previous = converted[-1]["content"]
        if isinstance(previous, list) and all(
            isinstance(item, dict) and item.get("type") == "tool_result" for item in previous
        ):
            previous.append(block)
            return
    converted.append({"role": "user", "content": [block]})


def _assistant_message(message: ChatMessage) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    text = _content_to_text(message.content)
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.tool_calls or []:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id or f"toolu_{call.function.name}",
                "name": call.function.name,
                "input": _parse_arguments(call.function.arguments),
            }
        )
    return {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}


def _drop_trailing_prefill(
    messages: list[dict[str, Any]], result: Translation
) -> list[dict[str, Any]]:
    """Rimuove un eventuale prefill in coda.

    Un messaggio assistant come ultimo elemento e' un prefill, e il prefill e'
    rimosso su tutta la famiglia 4.6+: lasciarlo significa un 400 secco.
    """
    if messages and messages[-1].get("role") == "assistant":
        has_tool_use = any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in messages[-1].get("content", [])
        )
        if not has_tool_use:
            result.notes.append("prefill assistant finale rimosso: non supportato dai modelli attuali")
            return messages[:-1]
    return messages


def _ensure_leading_user(
    messages: list[dict[str, Any]], result: Translation
) -> list[dict[str, Any]]:
    """L'API richiede che il primo messaggio sia di ruolo user."""
    if not messages:
        result.notes.append("nessun messaggio utile: inserito un turno user vuoto")
        return [{"role": "user", "content": [{"type": "text", "text": "(vuoto)"}]}]
    if messages[0].get("role") != "user":
        result.notes.append("anteposto un turno user: il primo messaggio non lo era")
        return [
            {"role": "user", "content": [{"type": "text", "text": "(continua)"}]},
            *messages,
        ]
    return messages


# --- contenuti -----------------------------------------------------------


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    """Converte il contenuto OpenAI in blocchi Anthropic."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            blocks.append({"type": "text", "text": str(part)})
            continue
        part_type = part.get("type")
        if part_type in {"text", "input_text"}:
            text = part.get("text", "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif part_type in {"image_url", "input_image"}:
            image = _image_block(part)
            if image:
                blocks.append(image)
        elif part_type == "tool_result":
            blocks.append(part)
        # Gli altri tipi (audio, file) non hanno equivalente diretto e vengono
        # ignorati: e' preferibile a inviare un blocco che l'API rifiuta.
    return blocks


def _image_block(part: dict[str, Any]) -> dict[str, Any] | None:
    raw = part.get("image_url") or part.get("image") or {}
    url = raw.get("url") if isinstance(raw, dict) else raw
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_to_text(content: Any) -> str:
    blocks = _content_to_blocks(content)
    return "\n".join(
        block["text"] for block in blocks if block.get("type") == "text" and block.get("text")
    )


def _parse_arguments(arguments: str | None) -> dict[str, Any]:
    """Gli argomenti di un tool call arrivano come stringa JSON.

    Vanno sempre passati da un parser: i modelli attuali possono produrre
    escaping diverso (unicode, slash), e il confronto su stringa serializzata
    e' inaffidabile.
    """
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {"_raw_arguments": arguments}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


# --- tool e formato ------------------------------------------------------


def _translate_tools(
    request: ChatCompletionRequest, result: Translation
) -> list[dict[str, Any]]:
    """Converte tools/functions, in ordine deterministico.

    L'ordinamento per nome non e' un vezzo: i tool renderizzano in posizione 0
    del prompt, prima di system e messages. Un ordine che cambia tra due
    richieste invalida l'intera cache, e nessun errore lo segnala.
    """
    definitions = []
    if request.tools:
        definitions = [tool.function for tool in request.tools if tool.function]
    elif request.functions:
        definitions = list(request.functions)
    if not definitions:
        return []

    result.tool_names = [definition.name for definition in definitions]
    tools: list[dict[str, Any]] = []
    for definition in definitions:
        schema = definition.parameters or {"type": "object", "properties": {}}
        tool: dict[str, Any] = {
            "name": definition.name,
            "description": definition.description or "",
            "input_schema": schema,
        }
        if definition.strict:
            tool["strict"] = True
        tools.append(tool)

    tools.sort(key=lambda item: item["name"])
    if [tool["name"] for tool in tools] != result.tool_names:
        result.notes.append("tool riordinati per nome: stabilizza il prefisso di cache")
    return tools


def _translate_tool_choice(
    request: ChatCompletionRequest, has_tools: bool, result: Translation
) -> dict[str, Any] | None:
    choice = request.tool_choice if request.tool_choice is not None else request.function_call
    if choice is None or not has_tools:
        return None
    if isinstance(choice, str):
        mapping = {
            "auto": {"type": "auto"},
            "required": {"type": "any"},
            "any": {"type": "any"},
            "none": {"type": "none"},
        }
        return mapping.get(choice, {"type": "auto"})
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        if name:
            return {"type": "tool", "name": name}
    result.notes.append(f"tool_choice non riconosciuto ({choice!r}): impostato auto")
    return {"type": "auto"}


def _translate_response_format(
    request: ChatCompletionRequest,
    messages: list[dict[str, Any]],
    result: Translation,
) -> dict[str, Any] | None:
    """Traduce ``response_format`` in ``output_config.format``.

    ``json_schema`` ha una corrispondenza diretta. ``json_object`` no: non
    esiste una modalita' JSON senza schema, quindi diventa un'istruzione in
    coda ai messaggi, dove non tocca il prefisso in cache.
    """
    response_format = request.response_format
    if response_format is None or response_format.type == "text":
        return None

    if response_format.type == "json_schema":
        payload = response_format.json_schema or {}
        schema = payload.get("schema") or payload
        return {"type": "json_schema", "schema": schema}

    if messages:
        instruction = "Rispondi esclusivamente con un singolo oggetto JSON valido, senza testo attorno."
        last = messages[-1]
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            last["content"].append({"type": "text", "text": instruction})
        else:
            messages.append(
                {"role": "user", "content": [{"type": "text", "text": instruction}]}
            )
        result.notes.append("json_object tradotto in istruzione testuale in coda")
    return None


def _record_dropped_params(request: ChatCompletionRequest, result: Translation) -> None:
    for name in UNSUPPORTED_SAMPLING:
        if getattr(request, name, None) is not None:
            result.dropped.append(name)
    if request.n and request.n > 1:
        result.dropped.append("n")
        result.notes.append("n>1 ignorato: l'API Claude restituisce una sola risposta")
    if result.dropped:
        result.notes.append(
            "parametri scartati perche' rimossi dai modelli Claude attuali: "
            + ", ".join(sorted(set(result.dropped)))
        )
