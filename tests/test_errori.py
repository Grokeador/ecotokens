"""Test della traduzione degli errori verso i client.

E' il codice che gira **solo quando le cose vanno male**, cioe' quando un bug
costa di piu': un 429 mappato male fa riprovare all'infinito un client che
avrebbe dovuto aspettare, e un 400 travestito da 500 lo fa riprovare su una
richiesta che sara' sempre rifiutata.

Era coperto al 29%. Non perche' fosse difficile da provare - come si vede qui
sotto non lo e' - ma perche' i test seguono di solito il percorso felice, ed e'
esattamente il motivo per cui questo percorso merita di essere scritto per
primo la prossima volta.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from ecotokens.api.errors import error_response


def risposta(stato: int) -> httpx2.Response:
    return httpx2.Response(stato, request=httpx2.Request("POST", "https://api.anthropic.com"))


def letta(errore: Exception) -> tuple[int, dict]:
    esito = error_response(errore)
    return esito.status_code, json.loads(bytes(esito.body))


# --- la catena, dal caso specifico al generico -----------------------------


@pytest.mark.parametrize(
    "errore, atteso_stato, atteso_tipo",
    [
        (
            anthropic.BadRequestError("temperature non supportata", response=risposta(400), body=None),
            400,
            "invalid_request_error",
        ),
        (
            anthropic.AuthenticationError("chiave non valida", response=risposta(401), body=None),
            401,
            "authentication_error",
        ),
        (
            anthropic.PermissionDeniedError("niente accesso", response=risposta(403), body=None),
            403,
            "permission_error",
        ),
        (
            anthropic.NotFoundError("modello inesistente", response=risposta(404), body=None),
            404,
            "not_found_error",
        ),
        (
            anthropic.RateLimitError("troppe richieste", response=risposta(429), body=None),
            429,
            "rate_limit_error",
        ),
    ],
)
def test_ogni_errore_dell_sdk_ha_il_suo_codice(errore, atteso_stato, atteso_tipo):
    stato, corpo = letta(errore)
    assert stato == atteso_stato
    assert corpo["error"]["type"] == atteso_tipo


def test_un_529_resta_un_errore_del_server_non_della_richiesta():
    """La distinzione che dice al client se ha senso riprovare.

    529 e' "overloaded": la richiesta era valida e riprovare ha senso.
    Classificarla come `invalid_request_error` direbbe al client di
    rinunciare su un errore che sarebbe passato al secondo tentativo.
    """
    errore = anthropic.APIStatusError("sovraccarico", response=risposta(529), body=None)
    stato, corpo = letta(errore)
    assert stato == 529
    assert corpo["error"]["type"] == "api_error"


def test_un_422_resta_un_errore_della_richiesta():
    errore = anthropic.APIStatusError("non elaborabile", response=risposta(422), body=None)
    stato, corpo = letta(errore)
    assert stato == 422
    assert corpo["error"]["type"] == "invalid_request_error"


def test_una_rete_irraggiungibile_diventa_502_non_500():
    """502 dice "il guasto sta a monte"; 500 direbbe "il guasto sta nel gateway".

    Chi legge i log deve poter distinguere un problema proprio da un problema
    di Anthropic senza aprire una traccia di stack.
    """
    errore = anthropic.APIConnectionError(
        message="connessione fallita",
        request=httpx2.Request("POST", "https://api.anthropic.com"),
    )
    stato, corpo = letta(errore)
    assert stato == 502
    assert corpo["error"]["type"] == "api_connection_error"


# --- i due casi che non somigliano a errori dell'SDK -----------------------


def test_le_credenziali_mancanti_non_sono_un_errore_interno():
    """L'SDK le segnala con un TypeError, che senza questo caso darebbe 500.

    E' il primo errore che incontra chiunque provi il gateway: riceverne un
    500 opaco invece di "imposta ANTHROPIC_API_KEY" e' la differenza fra
    trenta secondi e mezz'ora.
    """
    errore = TypeError("Could not resolve authentication method. Expected api_key")
    stato, corpo = letta(errore)
    assert stato == 401
    assert corpo["error"]["type"] == "authentication_error"
    assert "ANTHROPIC_API_KEY" in corpo["error"]["message"]


def test_un_typeerror_qualunque_resta_un_errore_interno():
    """Il riconoscimento e' su un messaggio: non deve diventare una rete a strascico.

    Un bug del gateway che solleva TypeError non va travestito da problema di
    credenziali dell'utente, che si metterebbe a cercare una chiave a posto.
    """
    stato, corpo = letta(TypeError("unsupported operand type(s)"))
    assert stato == 500
    assert corpo["error"]["type"] == "internal_error"


def test_un_errore_sconosciuto_non_perde_il_messaggio():
    stato, corpo = letta(RuntimeError("qualcosa di inatteso"))
    assert stato == 500
    assert "qualcosa di inatteso" in corpo["error"]["message"]


def test_la_risposta_ha_sempre_la_forma_che_un_client_openai_si_aspetta():
    """Un client che non trova `error.message` fallisce nel proprio parser.

    A quel punto l'utente vede un errore del suo SDK invece del nostro, e la
    causa vera sparisce.
    """
    for errore in (
        anthropic.RateLimitError("x", response=risposta(429), body=None),
        RuntimeError("y"),
        TypeError("Could not resolve authentication method"),
    ):
        _, corpo = letta(errore)
        assert set(corpo) == {"error"}
        assert isinstance(corpo["error"].get("message"), str)
        assert isinstance(corpo["error"].get("type"), str)
