"""Schemi delle richieste in formato OpenAI.

I modelli sono volutamente permissivi (``extra="allow"``): i client OpenAI in
circolazione mandano campi che nascono e muoiono in fretta, e un gateway che
rifiuta una richiesta per un campo sconosciuto smette di essere un drop-in
replacement. I campi che l'API Claude non accetta piu' vengono scartati piu'
avanti, nel traduttore, dove la decisione e' visibile e loggata.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = ""
    arguments: str = "{}"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = ""
    type: str = "function"
    function: FunctionCall = Field(default_factory=FunctionCall)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    # Puo' essere una stringa oppure una lista di parti multimodali.
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class FunctionDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = None


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "function"
    function: FunctionDefinition


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict[str, Any] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model: str | None = None
    # Obbligatorio, e con almeno un messaggio. Con un valore predefinito
    # pydantic non lo valida affatto - `validate_default` e' spento - quindi
    # un corpo vuoto passava come conversazione vuota e arrivava fino
    # all'API. Rifiutarlo qui risparmia un giro di rete e da' un errore che
    # parla della richiesta scritta invece che di quella tradotta.
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None

    max_tokens: int | None = None
    max_completion_tokens: int | None = None

    # Accettati e poi scartati: rimossi dai modelli Claude attuali.
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    n: int | None = None
    seed: int | None = None
    logprobs: bool | None = None

    stop: str | list[str] | None = None
    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    functions: list[FunctionDefinition] | None = None
    function_call: str | dict[str, Any] | None = None
    response_format: ResponseFormat | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None

    def resolved_max_tokens(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens

    def wants_usage_in_stream(self) -> bool:
        return bool(self.stream_options and self.stream_options.include_usage)

    def has_tools(self) -> bool:
        return bool(self.tools or self.functions)


def error_payload(message: str, err_type: str, code: str | None = None) -> dict[str, Any]:
    """Errore nel formato che i client OpenAI si aspettano."""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": None,
            "code": code,
        }
    }
