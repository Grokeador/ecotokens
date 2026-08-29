"""Tetto di spesa.

Deve essere il primo stadio della catena: il suo scopo e' impedire una spesa,
e uno stadio che blocca dopo che la richiesta e' partita non serve a nulla.

Il controllo di base e' sulla spesa gia' registrata. Con ``precount`` attivo si
aggiunge un preventivo del costo di input di *questa* richiesta, calcolato con
``count_tokens``: costa una chiamata in piu' (non fatturata) ma evita di
sforare proprio sull'ultima richiesta, quella grossa.
"""

from __future__ import annotations

from typing import Any

from ..pricing import model_info
from .base import BaseStage, PipelineAbort, RequestContext


class BudgetStage(BaseStage):
    name = "budget"

    def __init__(self, settings: Any) -> None:
        self.config = settings.budget
        self.enabled = self.config.enabled

    def _tetto_del_client(self, nome: str) -> float:
        """Il tetto che vale per questo client, zero se non ne ha uno.

        Un client senza nome non ha tetto proprio: non si sa chi sia, e
        applicargli il tetto comune significherebbe metterlo in un mucchio con
        tutti gli altri anonimi, dove il primo che spende blocca gli altri.
        """
        if not nome:
            return 0.0
        return float(
            self.config.tetti_client.get(nome, self.config.client_daily_usd) or 0.0
        )

    async def before(self, ctx: RequestContext) -> None:
        # Il confronto e' >=, non >: un tetto raggiunto e' un tetto esaurito, e
        # un limite impostato a zero deve impedire ogni spesa.
        today, this_month = await ctx.store.current_spend()

        projected = 0.0
        if self.config.precount:
            info = model_info(ctx.model)
            tokens = await ctx.counter.count(ctx.model, ctx.params)
            ctx.estimated_prompt_tokens = tokens
            # Solo il costo di input: l'output non e' prevedibile prima della
            # risposta, e sovrastimarlo bloccherebbe richieste legittime.
            projected = tokens * info.input_per_mtok / 1_000_000

        if today + projected >= self.config.daily_usd:
            raise PipelineAbort(
                f"Tetto giornaliero superato: {today:.4f} USD gia' spesi su "
                f"{self.config.daily_usd:.2f} USD disponibili"
                + (f" (questa richiesta ne aggiungerebbe {projected:.4f})" if projected else "")
            )

        # Il tetto per client si controlla **oltre** a quello globale, mai al
        # suo posto: dieci client ciascuno sotto il proprio limite possono
        # sfondare il totale, e il globale resta l'ultima difesa.
        tetto_client = self._tetto_del_client(ctx.nome_client)
        if tetto_client:
            speso = await ctx.store.spesa_di_oggi_del_client(ctx.nome_client)
            if speso + projected >= tetto_client:
                raise PipelineAbort(
                    f"Tetto giornaliero del client '{ctx.nome_client}' superato: "
                    f"{speso:.4f} USD gia' spesi su {tetto_client:.2f} disponibili"
                )

        if this_month + projected >= self.config.monthly_usd:
            raise PipelineAbort(
                f"Tetto mensile superato: {this_month:.4f} USD gia' spesi su "
                f"{self.config.monthly_usd:.2f} USD disponibili"
            )

        remaining = self.config.daily_usd - today
        if remaining < self.config.daily_usd * 0.1:
            ctx.note(f"budget giornaliero quasi esaurito: restano {remaining:.4f} USD")
