"""Adattatore per Google Gemini (API ``generateContent``).

Gemini non parla il dialetto OpenAI e cambia tre cose che contano: il ruolo
dell'assistente si chiama ``model``, il system prompt sta in un campo suo
(``systemInstruction``) invece che fra i messaggi, e i tool viaggiano come
dichiarazioni di funzione dentro un solo oggetto ``tools``.

La chiave va nell'intestazione ``x-goog-api-key`` e **non** nell'URL: una
chiave in query string finisce nei log del server, nei proxy e nella
cronologia di chiunque copi l'indirizzo.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2

from .base import (
    AdattatoreBase,
    ErroreProvider,
    blocchi_di,
    contenuto_risultato,
    costruisci_messaggio,
    nome_a_valle,
    parametri_puliti,
    testo_dei_blocchi,
    testo_di_sistema,
)

BASE_PREDEFINITA = "https://generativelanguage.googleapis.com/v1beta"

MOTIVI = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "PROHIBITED_CONTENT": "refusal",
}


class AdattatoreGoogle(AdattatoreBase):
    nome = "google"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
    ) -> None:
        super().__init__()
        intestazioni = {"content-type": "application/json"}
        if api_key:
            intestazioni["x-goog-api-key"] = api_key
        self._client = httpx2.AsyncClient(
            base_url=(base_url or BASE_PREDEFINITA).rstrip("/"),
            headers=intestazioni,
            timeout=timeout,
        )
        self._max_retries = max_retries

    async def close(self) -> None:
        await self._client.aclose()

    def _richiesta(self, parametri: dict[str, Any]) -> dict[str, Any]:
        puliti = parametri_puliti(parametri)
        contenuti: list[dict[str, Any]] = []
        for messaggio in puliti.get("messages") or []:
            voce = _messaggio_in_google(messaggio)
            if voce["parts"]:
                contenuti.append(voce)

        corpo: dict[str, Any] = {"contents": contenuti}

        sistema = testo_di_sistema(puliti.get("system"))
        if sistema:
            corpo["systemInstruction"] = {"parts": [{"text": sistema}]}

        configurazione: dict[str, Any] = {}
        if puliti.get("max_tokens"):
            configurazione["maxOutputTokens"] = puliti["max_tokens"]
        if puliti.get("temperature") is not None:
            configurazione["temperature"] = puliti["temperature"]
        if puliti.get("top_p") is not None:
            configurazione["topP"] = puliti["top_p"]
        if puliti.get("stop_sequences"):
            configurazione["stopSequences"] = puliti["stop_sequences"]
        if configurazione:
            corpo["generationConfig"] = configurazione

        strumenti = puliti.get("tools")
        if strumenti:
            corpo["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": s.get("name"),
                            "description": s.get("description") or "",
                            "parameters": _schema_per_google(
                                s.get("input_schema") or {"type": "object"}
                            ),
                        }
                        for s in strumenti
                    ]
                }
            ]
        return corpo

    async def crea(self, parametri: dict[str, Any]) -> Any:
        modello = nome_a_valle(parametri.get("model"))
        corpo = self._richiesta(parametri)
        dati = await self._invia(modello, corpo)

        candidati = dati.get("candidates") or []
        if not candidati:
            # Gemini risponde 200 con zero candidati quando un filtro blocca
            # tutto: senza questo controllo diventerebbe una risposta vuota
            # servita come se fosse valida, e finirebbe pure in cache.
            motivo = (dati.get("promptFeedback") or {}).get("blockReason") or "sconosciuto"
            raise ErroreProvider(f"google: nessun candidato (motivo: {motivo})")

        blocchi: list[dict[str, Any]] = []
        for parte in (candidati[0].get("content") or {}).get("parts") or []:
            if parte.get("text"):
                blocchi.append({"type": "text", "text": parte["text"]})
            chiamata = parte.get("functionCall")
            if chiamata:
                blocchi.append(
                    {
                        "type": "tool_use",
                        # Gemini non assegna un identificativo alle chiamate:
                        # lo costruiamo dal nome, che e' l'unica cosa con cui
                        # il risultato potra' essere riappaiato al ritorno.
                        "id": f"call_{chiamata.get('name')}_{len(blocchi)}",
                        "name": str(chiamata.get("name") or ""),
                        "input": chiamata.get("args") or {},
                    }
                )

        uso = dati.get("usageMetadata") or {}
        totale_prompt = int(uso.get("promptTokenCount") or 0)
        letti_da_cache = int(uso.get("cachedContentTokenCount") or 0)

        return costruisci_messaggio(
            modello=modello,
            blocchi=blocchi,
            stop_reason=MOTIVI.get(str(candidati[0].get("finishReason") or ""), "end_turn"),
            # Come per OpenAI: il conto di Google include i token da cache.
            input_tokens=totale_prompt - letti_da_cache,
            output_tokens=int(uso.get("candidatesTokenCount") or 0),
            cache_read_tokens=letti_da_cache,
        )

    async def _invia(self, modello: str, corpo: dict[str, Any]) -> dict[str, Any]:
        percorso = f"/models/{modello}:generateContent"
        ultimo: Exception | None = None
        for tentativo in range(self._max_retries + 1):
            try:
                risposta = await self._client.post(percorso, json=corpo)
            except Exception as errore:
                ultimo = errore
                continue
            if risposta.status_code < 400:
                return risposta.json()
            if risposta.status_code < 500 and risposta.status_code != 429:
                raise ErroreProvider(
                    f"google: {risposta.status_code} {risposta.text[:200]}",
                    stato=risposta.status_code,
                )
            ultimo = ErroreProvider(
                f"google: {risposta.status_code} {risposta.text[:200]}",
                stato=risposta.status_code,
            )
            if tentativo == self._max_retries:
                raise ultimo
        raise ErroreProvider(f"google: nessuna risposta ({ultimo})")


def _messaggio_in_google(messaggio: dict[str, Any]) -> dict[str, Any]:
    ruolo = "model" if messaggio.get("role") == "assistant" else "user"
    blocchi = blocchi_di(messaggio.get("content"))
    parti: list[dict[str, Any]] = []

    for blocco in blocchi:
        tipo = blocco.get("type")
        if tipo == "text" and blocco.get("text"):
            parti.append({"text": blocco["text"]})
        elif tipo == "tool_use":
            parti.append(
                {
                    "functionCall": {
                        "name": blocco.get("name"),
                        "args": blocco.get("input") or {},
                    }
                }
            )
        elif tipo == "tool_result":
            parti.append(
                {
                    "functionResponse": {
                        # Il nome della funzione qui non c'e': Anthropic lo
                        # tiene nel `tool_use` corrispondente. Si passa
                        # l'identificativo, che e' l'unica cosa disponibile.
                        "name": str(blocco.get("tool_use_id") or "tool"),
                        "response": {"content": contenuto_risultato(blocco)},
                    }
                }
            )

    if not parti:
        testo = testo_dei_blocchi(blocchi)
        if testo:
            parti.append({"text": testo})
    return {"role": ruolo, "parts": parti}


def _schema_per_google(schema: Any) -> Any:
    """Toglie le parole di JSON Schema che Gemini rifiuta.

    L'API accetta un sottoinsieme di OpenAPI e risponde 400 - non ignora - se
    trova ``additionalProperties`` o ``$schema``. Uno strumento perfettamente
    valido altrove fa fallire l'intera richiesta, e l'errore non dice quale
    campo: e' un pomeriggio perso, quindi si toglie qui.
    """
    if isinstance(schema, dict):
        return {
            chiave: _schema_per_google(valore)
            for chiave, valore in schema.items()
            if chiave not in ("additionalProperties", "$schema", "default", "examples")
        }
    if isinstance(schema, list):
        return [_schema_per_google(v) for v in schema]
    return schema


def _serializza(valore: Any) -> str:  # pragma: no cover - utilita' di diagnosi
    return json.dumps(valore, ensure_ascii=False, default=str)
