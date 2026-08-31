"""EcoTokens come libreria: una riga dentro il tuo programma, invece di un programma.

Fino alla 0.3 EcoTokens esisteva in una forma sola - un processo separato a cui
si reindirizzava il traffico. E' la forma giusta per chi deve coprire
applicazioni che non ha scritto, o mettere un tetto di spesa comune a piu'
programmi. Ma chiede tre cose a chiunque: avviare un processo, tenerlo acceso,
e **fargli passare davanti la propria chiave API**.

L'ultima e' quella che pesa. La chiave Anthropic e' una carta di credito, e
«un programma di terzi la tiene» e' una frase che in un'azienda apre una
revisione di sicurezza. Intanto il risparmio piu' grande misurato - **+76,0%
dal vivo** su un ciclo agentico - riguarda proprio chi scrive il proprio
codice, cioe' chi una riga in piu' la aggiunge senza pensarci e un processo in
piu' non lo vuole. Si chiedeva lo sforzo maggiore a chi aveva il guadagno
maggiore.

Qui la chiave non si muove: il client Anthropic resta tuo, e questo modulo gli
si mette intorno.

    import anthropic
    from ecotokens import Economico

    client = Economico(anthropic.AsyncAnthropic())
    risposta = await client.messages.create(
        model="claude-opus-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": "ciao"}],
    )

`risposta` e' l'oggetto `Message` dell'SDK, identico a quello che avresti
ricevuto senza. Cambia il conto, non il codice.

**Cosa cambia rispetto al gateway, e va detto invece che scoperto.** Due dei
nove stadi vivono di stato condiviso fra processi: la cache esatta (rispondere
senza chiamare, quando la domanda e' identica a una gia' fatta) e il registro
della spesa. In memoria, come nascono qui, valgono solo dentro la vita del tuo
programma. Passando `memoria=` un percorso, tornano a valere fra un'esecuzione
e l'altra. Il pianificatore dei breakpoint - quello che vale il +76% - non
dipende da niente di condiviso e rende identico nelle due forme.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .pipeline.base import PipelineAbort, RequestContext
from .pricing import Usage

__all__ = ["Economico", "PipelineAbort"]

# In memoria e non su disco: una libreria che si crea un file al primo import
# e' una libreria che sorprende. Chi vuole che la cache e la contabilita'
# sopravvivano al processo lo chiede, e sa dove finiscono.
SENZA_FILE = ":memory:"


class _Messaggi:
    """La faccia `client.messages`, per essere sostituibile all'originale."""

    def __init__(self, proprietario: "Economico") -> None:
        self._proprietario = proprietario

    async def create(self, **parametri: Any) -> Any:
        return await self._proprietario._crea(parametri)

    def stream(self, **parametri: Any):
        raise NotImplementedError(
            "Lo streaming non passa ancora dagli stadi di EcoTokens. Fino ad "
            "allora: `client.originale.messages.stream(...)` lo esegue senza "
            "ottimizzazioni, ed e' meglio di uno streaming che dice di "
            "risparmiare senza farlo."
        )

    async def count_tokens(self, **parametri: Any) -> Any:
        """Passa dritto: contare i token non consuma e non va ottimizzato."""
        return await self._proprietario.originale.messages.count_tokens(**parametri)


class Economico:
    """Un client Anthropic che spende meno, con la stessa firma di quello vero.

    Args:
        client: il tuo `anthropic.AsyncAnthropic`. Se manca, ne costruisce uno
            con le regole dell'SDK - cioe' leggendo `ANTHROPIC_API_KEY`
            dall'ambiente. In nessun caso questa libreria legge, scrive o
            trasporta la chiave: la tiene il client, che e' tuo.
        config: percorso di un `ecotokens.toml`. Senza, valgono i valori
            predefiniti, che sono quelli prudenti: nessuno stadio che possa
            cambiare il contenuto di una risposta e' acceso.
        memoria: dove tenere cache esatta e contabilita' fra un'esecuzione e
            l'altra. Senza, restano in memoria e muoiono col processo.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        config: str | Path | None = None,
        memoria: str | Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        # Importazione tardiva: `server` tira dentro FastAPI, che a una
        # libreria non serve. Farlo pagare a chi importa `ecotokens` era il
        # difetto piu' facile da introdurre qui.
        from .server import Gateway

        impostazioni = settings if settings is not None else load_settings(config)
        impostazioni.storage.path = str(memoria) if memoria else SENZA_FILE

        self._gateway = Gateway(impostazioni)
        if client is not None:
            self._gateway.client = client
        self._avviato = False
        self.messages = _Messaggi(self)

    # --- ciclo di vita ----------------------------------------------------

    @property
    def originale(self) -> Any:
        """Il client Anthropic sotto. Utile per cio' che non passa da qui."""
        return self._gateway.client

    @property
    def impostazioni(self) -> Settings:
        return self._gateway.settings

    async def _assicura_avvio(self) -> None:
        if not self._avviato:
            await self._gateway.startup()
            self._avviato = True

    async def aclose(self) -> None:
        """Chiude database e client. Idempotente.

        Chiude **anche** il client Anthropic, compreso quello passato da fuori:
        e' la scelta che sorprende meno chi usa `async with`, ed e' scritta qui
        perche' chi vuole tenerlo aperto non deve scoprirlo per tentativi.
        """
        if self._avviato:
            await self._gateway.shutdown()
            self._avviato = False

    async def __aenter__(self) -> "Economico":
        await self._assicura_avvio()
        return self

    async def __aexit__(self, *_) -> None:
        await self.aclose()

    # --- la richiesta -----------------------------------------------------

    async def _crea(self, parametri: dict[str, Any]) -> Any:
        await self._assicura_avvio()
        ctx = self._gateway.make_native_context(dict(parametri), {})
        await self._gateway.pipeline.before(ctx)

        if ctx.short_circuit is not None:
            return await self._da_cache(ctx)

        risorsa, params = self._gateway.messages_resource(ctx)
        params.pop("stream", None)
        messaggio = await risorsa.create(**params)

        ctx.usage = Usage.from_api(getattr(messaggio, "usage", None))
        payload = _in_dizionario(messaggio)
        ctx.client_response = payload
        ctx.upstream_response = payload
        await self._gateway.pipeline.after(ctx, messaggio)
        self.ultimo = ctx.meta()
        return messaggio

    async def _da_cache(self, ctx: RequestContext) -> Any:
        """Una risposta servita senza chiamare l'API, ricostruita come Message.

        Restituire un dizionario qui sarebbe il difetto peggiore possibile in
        una libreria che promette di essere sostituibile all'originale: il
        codice di chi la usa funzionerebbe **tranne** quando la cache colpisce,
        cioe' fallirebbe solo ogni tanto e per una ragione invisibile.
        """
        import anthropic

        ctx.client_response = ctx.short_circuit
        ctx.upstream_response = ctx.short_circuit
        await self._gateway.pipeline.after(ctx, None)
        self.ultimo = ctx.meta()
        payload = {k: v for k, v in ctx.short_circuit.items() if k != "ecotokens"}
        return anthropic.types.Message.model_validate(payload)

    # --- quanto si e' risparmiato ----------------------------------------

    ultimo: dict[str, Any] = {}

    async def statistiche(self) -> dict[str, Any]:
        """Totali di spesa e risparmio, come li mostrerebbe `ecotokens stats`."""
        await self._assicura_avvio()
        return await self._gateway.store.stats()


def _in_dizionario(messaggio: Any) -> dict[str, Any]:
    if hasattr(messaggio, "model_dump"):
        return messaggio.model_dump(mode="json")
    return dict(messaggio)
