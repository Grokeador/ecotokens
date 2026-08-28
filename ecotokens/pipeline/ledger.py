"""Contabilita': l'unico stadio che non ottimizza nulla, e il piu' importante.

Senza numeri affidabili non si sa se le ottimizzazioni funzionano, e un
gateway che crede di risparmiare mentre paga scritture di cache mai rilette e'
peggio di nessun gateway.

Il costo si calcola da ``response.usage``, mai da stime: ``input_tokens`` e' il
solo residuo non servito da cache, quindi il prompt reale e' la somma dei tre
contatori di input. Confondere i due e' l'errore che fa sembrare enorme un
risparmio inesistente.
"""

from __future__ import annotations

import logging
from typing import Any

from ..pricing import Usage, baseline_cost_usd, cost_usd
from .base import SOURCE_API, BaseStage, RequestContext

logger = logging.getLogger("ecotokens.ledger")


class LedgerStage(BaseStage):
    name = "ledger"

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        # La pipeline attribuisce a ogni stadio le note comparse mentre girava,
        # ma lo fa **dopo** che lo stadio e' tornato: la contabilita' scrive la
        # riga prima di quel momento, quindi le proprie note se le attribuisce
        # da sola. E' l'unico stadio che ha questo problema, ed e' per una
        # ragione strutturale: e' quello che scrive il registro.
        prima_delle_proprie = len(ctx.notes)
        if ctx.source == SOURCE_API:
            if message is None:
                return
            usage = ctx.usage if ctx.usage.total_prompt_tokens else Usage.from_api(
                getattr(message, "usage", None)
            )
            ctx.usage = usage
            ctx.cost_usd = cost_usd(ctx.model, usage, ctx.cache_ttl)
            # Sul modello **chiesto**, non su quello effettivamente usato.
            # Con il declassamento acceso i due differiscono, e prezzare la
            # baseline sul modello economico significa confrontare il gateway
            # con se stesso: il risparmio del declassamento sparisce, e una
            # scrittura di cache non ancora ripagata basta a far risultare il
            # gateway dannoso. E' successo, ed e' quello che ha fatto scoprire
            # questa riga.
            #
            # I token di prompt sono gli stessi in entrambi i casi, quindi
            # quella meta' del conto e' esatta. Quelli generati no: un modello
            # diverso avrebbe scritto una risposta di lunghezza diversa, e qui
            # si prezza la lunghezza osservata alla tariffa dell'altro. E' la
            # sola approssimazione del conto, ed e' dichiarata in pagina.
            baseline = baseline_cost_usd(ctx.requested_model, usage)
            # Le chiamate che il gateway ha fatto per conto proprio - il
            # riassunto di compattazione - le paga comunque l'utente: entrano
            # nel conto, altrimenti uno stadio che chiama un modello sembra
            # gratuito e viene acceso quando non conviene.
            ctx.saved_usd = baseline - ctx.total_cost_usd
            if ctx.aux_cost_usd:
                ctx.note(
                    f"chiamate interne del gateway: {ctx.aux_cost_usd:.6f} USD "
                    "conteggiati nel risparmio"
                )
            if ctx.saved_usd < 0:
                # Succede quando si e' pagata una scrittura di cache che nessuna
                # richiesta successiva ha riletto. Va reso visibile, non nascosto.
                ctx.note(
                    f"costo superiore alla baseline di {abs(ctx.saved_usd):.6f} USD: "
                    "scrittura di cache non ancora ripagata"
                )
        else:
            # Hit di cache: nessun token speso, il risparmio l'ha gia' calcolato
            # lo stadio che ha servito la risposta.
            baseline = ctx.saved_usd
            ctx.cost_usd = 0.0

        ctx.attribuisci(self.name, ctx.notes[prima_delle_proprie:])
        await ctx.store.record_usage(
            session_id=ctx.session_id,
            model=ctx.model,
            source=ctx.source,
            usage=ctx.usage,
            cost_usd=ctx.total_cost_usd,
            baseline_cost_usd=baseline,
            saved_usd=ctx.saved_usd,
            cache_ttl=ctx.cache_ttl,
            latency_ms=ctx.elapsed_ms,
            notes=ctx.notes,
            stage_notes=ctx.stage_notes,
            stages_enabled=ctx.stages_enabled,
            overhead_tokens=ctx.overhead_tokens,
            aux_cost_usd=ctx.aux_cost_usd,
            client_format=ctx.client_format,
        )

        logger.info(
            "%s | %s | prompt=%d (cache_read=%d, cache_write=%d) output=%d | "
            "costo=%.6f USD risparmio=%.6f USD | %.0f ms",
            ctx.source,
            ctx.model,
            ctx.usage.total_prompt_tokens,
            ctx.usage.cache_read_tokens,
            ctx.usage.cache_creation_tokens,
            ctx.usage.output_tokens,
            ctx.cost_usd,
            ctx.saved_usd,
            ctx.elapsed_ms,
        )
