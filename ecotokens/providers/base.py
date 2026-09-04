"""Ossatura comune degli adattatori: la faccia, la risposta, il conteggio.

Un adattatore ha un solo obbligo: essere **sostituibile** al client Anthropic
dell'SDK per quel poco che il gateway ne usa. Sono tre cose:

* ``client.messages.create(**parametri)`` -> un ``Message`` dell'SDK;
* ``client.messages.count_tokens(**parametri)`` -> un oggetto con
  ``input_tokens`` (vedi ``TokenCounter`` in ``tokens.py``);
* ``client.close()``, chiamato allo spegnimento del gateway.

Restituire un ``Message`` vero e non un dizionario e' la stessa scelta fatta in
``libreria.py`` per le risposte servite da cache, e per la stessa ragione: un
client sostituibile che restituisce una forma diversa funziona **tranne** in
alcuni casi, cioe' fallisce solo ogni tanto e per una ragione invisibile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..tokens import estimate_prompt_tokens, strip_cache_control


@dataclass(frozen=True)
class Conteggio:
    """Cio' che ``TokenCounter`` si aspetta da ``count_tokens``."""

    input_tokens: int


class ErroreProvider(RuntimeError):
    """Il provider a valle ha rifiutato o non ha risposto.

    Non e' un guasto di stadio: la richiesta non e' partita o non e' tornata, e
    la regola del degrado non si applica - non esiste una versione "senza
    questo stadio" di una chiamata all'API. Sale fino alla rotta, che la
    traduce in un errore per il client.
    """

    def __init__(self, messaggio: str, *, stato: int = 502) -> None:
        super().__init__(messaggio)
        self.stato = stato


class FacciaMessaggi:
    """La faccia ``client.messages``, uguale per tutti gli adattatori."""

    def __init__(self, adattatore: "AdattatoreBase") -> None:
        self._adattatore = adattatore

    async def create(self, **parametri: Any) -> Any:
        if parametri.pop("stream", False):
            # Dichiarato, non aggirato. Uno streaming finto - richiesta intera
            # e poi spezzettata in uscita - sembrerebbe funzionare e
            # cambierebbe la latenza percepita senza dirlo. Il percorso
            # Anthropic, che lo streaming ce l'ha davvero, non passa di qui.
            raise ErroreProvider(
                f"lo streaming non e' ancora implementato per il provider "
                f"'{self._adattatore.nome}': usare stream=false, oppure il "
                f"provider 'anthropic', che lo supporta",
                stato=400,
            )
        return await self._adattatore.crea(parametri)

    async def count_tokens(self, **parametri: Any) -> Conteggio:
        return await self._adattatore.conta(parametri)


class AdattatoreBase:
    """Base degli adattatori non-Anthropic.

    Il conteggio dei token e' **stimato**, non esatto: nessuno degli altri
    provider offre un endpoint che conti un prompt senza eseguirlo. La stima
    locale sbaglia, e sbaglia in modo diverso per ogni tokenizzatore; sta qui
    perche' ``TokenCounter`` ci ripiegherebbe comunque, ma dichiararlo evita di
    presentare come misurato cio' che e' congetturato.
    """

    nome = "base"

    def __init__(self) -> None:
        self.messages = FacciaMessaggi(self)

    async def crea(self, parametri: dict[str, Any]) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def conta(self, parametri: dict[str, Any]) -> Conteggio:
        return Conteggio(input_tokens=estimate_prompt_tokens(parametri))

    async def close(self) -> None:  # pragma: no cover - ridefinito dai figli
        return None


# --- traduzione: forma Anthropic -> forma comune ---------------------------


def testo_di_sistema(system: Any) -> str:
    """Riduce il ``system`` Anthropic a testo semplice.

    Puo' arrivare come stringa o come lista di blocchi con ``cache_control``:
    i marker si tolgono, perche' fuori da Anthropic non significano niente e
    lasciarli dentro li farebbe pagare come testo.
    """
    if not system:
        return ""
    if isinstance(system, str):
        return system
    pezzi: list[str] = []
    for blocco in system if isinstance(system, list) else [system]:
        if isinstance(blocco, dict):
            pezzi.append(str(blocco.get("text", "")))
        elif isinstance(blocco, str):
            pezzi.append(blocco)
    return "\n\n".join(pezzo for pezzo in pezzi if pezzo)


def blocchi_di(contenuto: Any) -> list[dict[str, Any]]:
    """Normalizza il contenuto di un messaggio a lista di blocchi."""
    if contenuto is None:
        return []
    if isinstance(contenuto, str):
        return [{"type": "text", "text": contenuto}]
    if isinstance(contenuto, dict):
        return [contenuto]
    return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in contenuto]


def testo_dei_blocchi(blocchi: list[dict[str, Any]]) -> str:
    """Il solo testo, per i provider che non conoscono i blocchi tipizzati."""
    return "\n".join(
        str(b.get("text", "")) for b in blocchi if b.get("type") == "text" and b.get("text")
    )


def contenuto_risultato(blocco: dict[str, Any]) -> str:
    """Il testo di un ``tool_result``, che l'API accetta in due forme.

    Puo' essere una stringa o una lista di blocchi. Un provider che si aspetta
    una stringa e ne riceve una lista non fallisce: serializza l'oggetto e lo
    manda come testo, cioe' fa pagare le graffe. Ridurlo qui una volta sola
    copre tutti gli adattatori.
    """
    contenuto = blocco.get("content")
    if isinstance(contenuto, str):
        return contenuto
    if isinstance(contenuto, list):
        testo = testo_dei_blocchi(blocchi_di(contenuto))
        if testo:
            return testo
    if contenuto is None:
        return ""
    return json.dumps(contenuto, ensure_ascii=False, default=str)


def nome_a_valle(modello: Any) -> str:
    """Il nome del modello come lo conosce il provider.

    Il prefisso `locale/` e' un'informazione per il gateway - dice che il
    modello gira in casa e quindi costa zero - e non per chi risponde: Ollama
    non ha nessun modello che si chiami cosi' e risponderebbe 404.
    """
    from ..pricing import PREFISSO_LOCALE

    nome = str(modello or "")
    if nome.lower().startswith(PREFISSO_LOCALE):
        return nome[len(PREFISSO_LOCALE):]
    return nome


def parametri_puliti(parametri: dict[str, Any]) -> dict[str, Any]:
    """I parametri senza i marker di cache Anthropic.

    Il pianificatore dei breakpoint gira anche quando a valle c'e' un altro
    provider - e' uno stadio, non sa chi risponde - e i suoi marker qui vanno
    tolti prima di partire: sono campi che gli altri provider non conoscono e
    che alcuni rifiutano con un 400.
    """
    return {chiave: strip_cache_control(valore) for chiave, valore in parametri.items()}


# --- costruzione della risposta --------------------------------------------


def costruisci_messaggio(
    *,
    modello: str,
    blocchi: list[dict[str, Any]],
    stop_reason: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    identificativo: str = "",
) -> Any:
    """Un ``Message`` dell'SDK Anthropic costruito da pezzi normalizzati.

    ``input_tokens`` segue la convenzione Anthropic: e' il **residuo non
    servito da cache**, non il totale del prompt. E' la trappola piu' facile
    del multi-provider, perche' gli altri contano al contrario - il loro totale
    include i token letti da cache - e sommarli come stanno gonfierebbe il
    prompt di ogni richiesta che colpisce la cache. La conversione la fa
    l'adattatore, prima di chiamare questa funzione.
    """
    import anthropic

    payload = {
        "id": identificativo or "msg_ecotokens",
        "type": "message",
        "role": "assistant",
        "model": modello,
        "content": blocchi or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(0, input_tokens),
            "output_tokens": max(0, output_tokens),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": max(0, cache_read_tokens),
        },
    }
    return anthropic.types.Message.model_validate(payload)
