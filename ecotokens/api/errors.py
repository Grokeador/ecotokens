"""Traduzione degli errori dell'SDK nei codici che i client OpenAI capiscono."""

from __future__ import annotations

import logging

import anthropic
from fastapi.responses import JSONResponse

from .schemas import error_payload

logger = logging.getLogger("ecotokens.errors")


def error_response(error: Exception) -> JSONResponse:
    """Mappa un'eccezione su una risposta HTTP in formato OpenAI.

    La catena va dal caso specifico al generico: distinguere un 404 da un 429
    e' cio' che permette al client di sapere se ha senso riprovare.
    """
    # L'SDK segnala l'assenza di credenziali con un TypeError, non con una
    # AuthenticationError: senza questo caso l'utente riceve un 500 opaco al
    # posto di un messaggio che gli dice cosa fare.
    if isinstance(error, TypeError) and "Could not resolve authentication" in str(error):
        return _json(
            401,
            "Nessuna credenziale Anthropic trovata. Impostare ANTHROPIC_API_KEY, "
            "oppure autenticarsi con `ant auth login`, oppure indicare "
            "upstream.api_key nel file di configurazione.",
            "authentication_error",
        )
    if isinstance(error, anthropic.BadRequestError):
        return _json(400, error.message, "invalid_request_error")
    if isinstance(error, anthropic.AuthenticationError):
        return _json(
            401,
            "Credenziali Anthropic non valide o assenti. Impostare ANTHROPIC_API_KEY "
            "oppure autenticarsi con `ant auth login`.",
            "authentication_error",
        )
    if isinstance(error, anthropic.PermissionDeniedError):
        return _json(403, error.message, "permission_error")
    if isinstance(error, anthropic.NotFoundError):
        return _json(404, error.message, "not_found_error")
    if isinstance(error, anthropic.RateLimitError):
        return _json(429, error.message, "rate_limit_error")
    if isinstance(error, anthropic.APIStatusError):
        kind = "api_error" if error.status_code >= 500 else "invalid_request_error"
        return _json(error.status_code, error.message, kind)
    if isinstance(error, anthropic.APIConnectionError):
        return _json(
            502, "Impossibile raggiungere l'API Anthropic.", "api_connection_error"
        )
    logger.exception("errore non gestito nel gateway")
    return _json(500, f"Errore interno del gateway: {error}", "internal_error")


def _json(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_payload(message, kind))
