"""Chi costruire, letto dalla configurazione.

Un punto solo. Prima ce n'era uno anche prima - ``Gateway._build_client`` - e
la scelta di tenerne uno mentre i provider diventano quattro e' la ragione per
cui gli stadi non hanno dovuto cambiare: nessuno di loro sa chi risponde.

Il percorso Anthropic resta **il client vero dell'SDK**, non un adattatore che
lo imita. E' l'unico su cui i numeri del README sono misurati, ed e' anche
l'unico che supporta lo streaming, il conteggio esatto dei token e i beta
header: farlo passare da una traduzione in piu' avrebbe rischiato quei numeri
per una simmetria che non serve a nessuno.
"""

from __future__ import annotations

from typing import Any

# Nome in configurazione -> come raggiungerlo. Gli ultimi tre parlano tutti il
# dialetto OpenAI e cambiano solo indirizzo: e' il motivo per cui un adattatore
# solo li copre tutti.
PROVIDER_NOTI: dict[str, str] = {
    "anthropic": "SDK Anthropic nativo (streaming, conteggio esatto, beta)",
    "openai": "https://api.openai.com/v1",
    "google": "Gemini generateContent",
    "ollama": "http://localhost:11434/v1 (locale, costo zero)",
    "lmstudio": "http://localhost:1234/v1 (locale, costo zero)",
    "openrouter": "https://openrouter.ai/api/v1",
    "compatibile": "qualunque endpoint /v1/chat/completions (base_url obbligatorio)",
}

# Indirizzi predefiniti di chi ha un indirizzo fisso o convenzionale.
INDIRIZZI: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

# Chi gira sulla macchina di chi lo usa. Non e' un dettaglio di comodo: da qui
# discendono il costo zero nel registro e il fatto che il contenuto della
# richiesta non lasci il computer, che e' l'unica garanzia di riservatezza che
# un gateway possa dare davvero.
LOCALI = frozenset({"ollama", "lmstudio"})


class ProviderIgnoto(ValueError):
    """Nome di provider che non esiste. Si ferma all'avvio, non alla prima
    richiesta: un errore di configurazione scoperto sotto carico costa piu' di
    uno scoperto al lancio."""


def e_locale(provider: str) -> bool:
    return (provider or "").strip().lower() in LOCALI


def costruisci_client(settings: Any) -> Any:
    """Il client verso il provider configurato.

    Restituisce il client dell'SDK per Anthropic, un adattatore per tutti gli
    altri. In entrambi i casi il gateway lo usa allo stesso modo.
    """
    upstream = settings.upstream
    provider = (getattr(upstream, "provider", None) or "anthropic").strip().lower()

    if provider not in PROVIDER_NOTI:
        noti = ", ".join(sorted(PROVIDER_NOTI))
        raise ProviderIgnoto(f"provider '{provider}' sconosciuto; noti: {noti}")

    if provider == "anthropic":
        return _client_anthropic(settings)

    if provider == "google":
        from .google_ import AdattatoreGoogle

        return AdattatoreGoogle(
            api_key=upstream.api_key,
            base_url=upstream.base_url,
            timeout=upstream.timeout_seconds,
            max_retries=upstream.max_retries,
        )

    from .openai_compat import AdattatoreOpenAI

    base_url = upstream.base_url or INDIRIZZI.get(provider)
    if not base_url:
        raise ProviderIgnoto(
            f"il provider '{provider}' non ha un indirizzo predefinito: "
            f"impostare upstream.base_url"
        )
    return AdattatoreOpenAI(
        base_url=base_url,
        api_key=upstream.api_key,
        timeout=upstream.timeout_seconds,
        max_retries=upstream.max_retries,
        nome=provider,
    )


def _client_anthropic(settings: Any) -> Any:
    import anthropic

    from ..config import intestazioni_upstream

    upstream = settings.upstream
    kwargs: dict[str, Any] = {
        "timeout": upstream.timeout_seconds,
        "max_retries": upstream.max_retries,
    }
    # Se la chiave non e' in configurazione, l'SDK la risolve da solo:
    # ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN o un profilo `ant auth login`.
    if upstream.api_key:
        kwargs["api_key"] = upstream.api_key
    if upstream.base_url:
        kwargs["base_url"] = upstream.base_url
    intestazioni = intestazioni_upstream(upstream)
    if intestazioni:
        kwargs["default_headers"] = intestazioni
    return anthropic.AsyncAnthropic(**kwargs)
