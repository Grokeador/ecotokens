"""Adattatore per il dialetto OpenAI, e per tutti quelli che lo parlano.

Un adattatore solo copre OpenAI, Ollama, LM Studio, OpenRouter, vLLM e
chiunque altro esponga ``/v1/chat/completions``: cambiano l'indirizzo e la
chiave, non la forma. Tenerne uno invece di cinque non e' pigrizia - cinque
copie dello stesso codice divergono, e la traduzione dei tool e' la parte in
cui divergere fa danno silenzioso.

Il verso della traduzione e' l'opposto di quello in ``translate/``: li si
riceve OpenAI e si produce Anthropic, qui si riceve Anthropic - il formato
interno della pipeline - e si produce OpenAI.
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

# Da finish_reason OpenAI a stop_reason Anthropic. Un motivo ignoto diventa
# "end_turn": e' l'unico valore che non induce il chiamante a riprovare, e in
# un ciclo agentico un motivo di arresto tradotto male produce un giro in piu'
# pagato per intero.
MOTIVI = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


class AdattatoreOpenAI(AdattatoreBase):
    nome = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float,
        max_retries: int,
        headers: dict[str, str] | None = None,
        nome: str | None = None,
    ) -> None:
        super().__init__()
        if nome:
            self.nome = nome
        intestazioni = {"content-type": "application/json"}
        # Senza chiave si parte lo stesso: e' il caso dei modelli locali, dove
        # non esiste nessuna chiave da mandare e mandarne una vuota fa
        # rispondere 401 a implementazioni che la controllano comunque.
        if api_key:
            intestazioni["authorization"] = f"Bearer {api_key}"
        intestazioni.update(headers or {})
        self._client = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=intestazioni,
            timeout=timeout,
        )
        self._max_retries = max_retries

    async def close(self) -> None:
        await self._client.aclose()

    # -- andata -------------------------------------------------------------

    def _richiesta(self, parametri: dict[str, Any]) -> dict[str, Any]:
        puliti = parametri_puliti(parametri)
        messaggi: list[dict[str, Any]] = []

        sistema = testo_di_sistema(puliti.get("system"))
        if sistema:
            messaggi.append({"role": "system", "content": sistema})

        for messaggio in puliti.get("messages") or []:
            messaggi.extend(_messaggio_in_openai(messaggio))

        richiesta: dict[str, Any] = {
            "model": nome_a_valle(puliti.get("model")),
            "messages": messaggi,
        }
        # `max_tokens` e' obbligatorio in Anthropic e facoltativo qui, ma
        # ometterlo lascerebbe decidere al provider quanto produrre: il tetto
        # che l'utente ha scritto e' anche il suo tetto di spesa.
        if puliti.get("max_tokens"):
            richiesta["max_completion_tokens"] = puliti["max_tokens"]
        for chiave in ("temperature", "top_p"):
            if puliti.get(chiave) is not None:
                richiesta[chiave] = puliti[chiave]
        if puliti.get("stop_sequences"):
            richiesta["stop"] = puliti["stop_sequences"]

        strumenti = puliti.get("tools")
        if strumenti:
            richiesta["tools"] = [_strumento_in_openai(s) for s in strumenti]
            scelta = _scelta_in_openai(puliti.get("tool_choice"))
            if scelta is not None:
                richiesta["tool_choice"] = scelta
        return richiesta

    # -- ritorno ------------------------------------------------------------

    async def crea(self, parametri: dict[str, Any]) -> Any:
        corpo = self._richiesta(parametri)
        dati = await self._invia(corpo)

        scelte = dati.get("choices") or []
        if not scelte:
            raise ErroreProvider(f"{self.nome}: risposta senza 'choices'")
        messaggio = scelte[0].get("message") or {}

        blocchi: list[dict[str, Any]] = []
        testo = messaggio.get("content")
        if isinstance(testo, str) and testo:
            blocchi.append({"type": "text", "text": testo})
        for chiamata in messaggio.get("tool_calls") or []:
            blocchi.append(_chiamata_in_anthropic(chiamata))

        uso = dati.get("usage") or {}
        prompt_tokens = int(uso.get("prompt_tokens") or 0)
        dettagli = uso.get("prompt_tokens_details") or {}
        letti_da_cache = int(dettagli.get("cached_tokens") or 0)

        return costruisci_messaggio(
            modello=str(dati.get("model") or corpo.get("model") or ""),
            blocchi=blocchi,
            stop_reason=MOTIVI.get(str(scelte[0].get("finish_reason") or ""), "end_turn"),
            # Il totale OpenAI **comprende** i token letti da cache; quello
            # Anthropic li tiene fuori. Senza questa sottrazione ogni hit di
            # cache verrebbe contato due volte, e il registro mostrerebbe un
            # prompt che cresce proprio dove il gateway sta risparmiando.
            input_tokens=prompt_tokens - letti_da_cache,
            output_tokens=int(uso.get("completion_tokens") or 0),
            cache_read_tokens=letti_da_cache,
            identificativo=str(dati.get("id") or ""),
        )

    async def _invia(self, corpo: dict[str, Any]) -> dict[str, Any]:
        ultimo: Exception | None = None
        for tentativo in range(self._max_retries + 1):
            try:
                risposta = await self._client.post("/chat/completions", json=corpo)
            except Exception as errore:  # rete: vale la pena riprovare
                ultimo = errore
                continue
            if risposta.status_code < 400:
                return risposta.json()
            # 4xx: la richiesta e' sbagliata e lo sara' anche al prossimo giro.
            # Riprovarla spende latenza per ottenere lo stesso rifiuto.
            if risposta.status_code < 500 and risposta.status_code != 429:
                raise ErroreProvider(
                    f"{self.nome}: {risposta.status_code} {_dettaglio(risposta)}",
                    stato=risposta.status_code,
                )
            ultimo = ErroreProvider(
                f"{self.nome}: {risposta.status_code} {_dettaglio(risposta)}",
                stato=risposta.status_code,
            )
            if tentativo == self._max_retries:
                raise ultimo
        raise ErroreProvider(f"{self.nome}: nessuna risposta ({ultimo})")


def _dettaglio(risposta: Any) -> str:
    """Il messaggio d'errore del provider, corto: finisce in un log."""
    try:
        corpo = risposta.json()
    except Exception:
        return risposta.text[:200]
    errore = corpo.get("error") if isinstance(corpo, dict) else None
    if isinstance(errore, dict):
        return str(errore.get("message") or errore)[:200]
    return str(errore or corpo)[:200]


def _messaggio_in_openai(messaggio: dict[str, Any]) -> list[dict[str, Any]]:
    """Un messaggio Anthropic diventa **uno o piu'** messaggi OpenAI.

    Non e' una corrispondenza uno a uno: Anthropic mette i risultati dei tool
    dentro un messaggio ``user`` con piu' blocchi, OpenAI vuole un messaggio
    ``tool`` separato per ciascuno. Un ciclo agentico con tre tool in parallelo
    - la forma che si vuole incoraggiare, perche' costa un round-trip invece di
    tre - qui diventa tre messaggi.
    """
    ruolo = messaggio.get("role", "user")
    blocchi = blocchi_di(messaggio.get("content"))

    risultati = [b for b in blocchi if b.get("type") == "tool_result"]
    if risultati:
        prodotti = [
            {
                "role": "tool",
                "tool_call_id": str(b.get("tool_use_id") or ""),
                "content": contenuto_risultato(b),
            }
            for b in risultati
        ]
        # Il testo che accompagna i risultati non si perde: diventa un
        # messaggio a se'. Buttarlo sarebbe una perdita invisibile - il modello
        # riceverebbe meno di quanto l'utente ha scritto, senza nessun segno.
        testo = testo_dei_blocchi(blocchi)
        if testo:
            prodotti.append({"role": "user", "content": testo})
        return prodotti

    usi = [b for b in blocchi if b.get("type") == "tool_use"]
    if usi and ruolo == "assistant":
        return [
            {
                "role": "assistant",
                "content": testo_dei_blocchi(blocchi) or None,
                "tool_calls": [
                    {
                        "id": str(b.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(b.get("name") or ""),
                            "arguments": json.dumps(
                                b.get("input") or {}, ensure_ascii=False
                            ),
                        },
                    }
                    for b in usi
                ],
            }
        ]

    return [{"role": ruolo, "content": testo_dei_blocchi(blocchi)}]


def _strumento_in_openai(strumento: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": strumento.get("name"),
            "description": strumento.get("description") or "",
            "parameters": strumento.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


def _scelta_in_openai(scelta: Any) -> Any:
    if not isinstance(scelta, dict):
        return None
    tipo = scelta.get("type")
    if tipo == "auto":
        return "auto"
    if tipo == "any":
        return "required"
    if tipo == "none":
        return "none"
    if tipo == "tool" and scelta.get("name"):
        return {"type": "function", "function": {"name": scelta["name"]}}
    return None


def _chiamata_in_anthropic(chiamata: dict[str, Any]) -> dict[str, Any]:
    funzione = chiamata.get("function") or {}
    grezzi = funzione.get("arguments")
    try:
        argomenti = json.loads(grezzi) if isinstance(grezzi, str) else (grezzi or {})
    except json.JSONDecodeError:
        # Un modello che produce JSON rotto e' un fatto, non un'eccezione da
        # propagare: l'errore va al modello dentro il risultato del tool, dove
        # puo' correggersi, invece di abbattere il giro. Il ciclo agentico se
        # ne accorge perche' gli argomenti sono vuoti e il grezzo e' li'.
        argomenti = {"_argomenti_non_validi": grezzi}
    return {
        "type": "tool_use",
        "id": str(chiamata.get("id") or ""),
        "name": str(funzione.get("name") or ""),
        "input": argomenti if isinstance(argomenti, dict) else {"valore": argomenti},
    }
