"""L'header di workspace, e il 400 che non conferma niente.

Due difetti trovati nello stesso minuto, al primo contatto con l'API vera.

Il primo e' del **gateway**: una chiave legata a un'identita' pretende
`anthropic-workspace-id`, e senza quell'header l'API risponde 400 a ogni
richiesta. Il gateway non lo mandava, quindi con quel tipo di chiave - che e'
quello che si ottiene creandola dalla propria utenza - non funzionava affatto.

Il secondo e' dello **strumento**, ed e' il piu' caro dei due: `verifica`
concludeva «assunzione confermata» su qualunque `BadRequestError`. Ha ricevuto
il 400 del workspace e ha dichiarato verificate due assunzioni che non aveva
nemmeno sfiorato. Un controllo che passa per la ragione sbagliata non si
corregge da se': si crede.
"""

from __future__ import annotations

import anthropic
import pytest

from ecotokens.config import Settings, intestazioni_upstream
from ecotokens.server import Gateway
from ecotokens.verifica import _quattrocento_estraneo

INTESTAZIONE = "anthropic-workspace-id"
WORKSPACE = "wrkspc_finto"


# --- l'header ---------------------------------------------------------------


def test_senza_configurazione_e_senza_ambiente_non_si_manda_niente(monkeypatch):
    """Le chiavi di workspace non ne hanno bisogno: mandare un header vuoto
    romperebbe il caso che oggi funziona."""
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert intestazioni_upstream(Settings().upstream) == {}
    assert intestazioni_upstream() == {}


def test_l_ambiente_basta(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", WORKSPACE)
    assert intestazioni_upstream() == {INTESTAZIONE: WORKSPACE}


def test_la_configurazione_vince_sull_ambiente(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "quello-dell-ambiente")
    settings = Settings()
    settings.upstream.workspace_id = WORKSPACE
    assert intestazioni_upstream(settings.upstream) == {INTESTAZIONE: WORKSPACE}


@pytest.mark.parametrize("valore", ["", "   ", "\n"])
def test_un_valore_vuoto_vale_come_assente(monkeypatch, valore):
    """Stessa trappola della credenziale lunga un carattere: la variabile
    esiste, il contenuto no. Un header vuoto darebbe un 400 piu' oscuro di
    quello che si voleva evitare."""
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", valore)
    assert intestazioni_upstream() == {}


def test_il_gateway_lo_mette_davvero_nel_client(monkeypatch):
    """Il posto in cui serve: quattro moduli costruiscono un client, e il
    controllo che conta e' che quello che inoltra le richieste ce l'abbia."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-finta-ma-lunga-abbastanza")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", WORKSPACE)
    settings = Settings()
    settings.storage.path = ":memory:"

    client = Gateway(settings).client
    assert client.default_headers[INTESTAZIONE] == WORKSPACE


# --- il 400 che non risponde alla domanda ----------------------------------


def _errore(messaggio: str) -> anthropic.BadRequestError:
    """Un `BadRequestError` non si costruisce a mano senza una risposta vera:
    quello che il controllo guarda e' il testo, e basta quello."""

    class Finto(Exception):
        def __str__(self) -> str:
            return messaggio

    return Finto()


def test_il_quattrocento_del_workspace_non_conferma_temperature():
    """Il caso reale, alla lettera: e' cio' che l'API ha risposto quando la
    domanda era un'altra."""
    errore = _errore(
        "Error code: 400 - {'type': 'invalid_request_error', 'message': "
        "'anthropic-workspace-id is required when authenticating with an "
        "identity-linked API key'}"
    )
    assert _quattrocento_estraneo(errore, "temperature")
    assert _quattrocento_estraneo(errore, "cache_control")


def test_il_quattrocento_atteso_conferma():
    atteso = _errore("400: temperature: Extra inputs are not permitted")
    assert _quattrocento_estraneo(atteso, "temperature") == ""


def test_il_confronto_ignora_le_maiuscole():
    assert _quattrocento_estraneo(_errore("Cache_Control limit"), "cache_control") == ""


def test_bastano_una_delle_parole():
    errore = _errore("too many cache control blocks")
    assert _quattrocento_estraneo(errore, "cache_control", "cache control") == ""
