"""Riscrittura del prompt prima dell'invio.

Le trasformazioni vivono in ``ecotokens.prompt_opt``, che non conosce la
pipeline ed e' verificabile da sola. Questo stadio decide soltanto *dove*
applicarle e *quanto* e' valso.

Due decisioni meritano di essere spiegate, perche' vanno contro l'istinto.

**Non si toccano i messaggi assistant ne' i tool result.** Sono le parole che il
modello ha effettivamente prodotto e i dati che il mondo esterno ha restituito.
Riscriverli non e' un'ottimizzazione, e' falsificare il verbale: al turno dopo
il modello leggerebbe una versione di se' stesso che non ha mai detto.

**Dove si risparmia conta piu' di quanto si risparmia.** Il prompt di sistema e'
la parte piu' grossa e la piu' facile da accorciare, ma e' anche quella che il
prompt caching serve a un decimo del prezzo: toglierle un token vale un decimo
di toglierlo alla coda non servita da cache. Chi guarda solo i caratteri tolti
si convince del contrario. La nota che questo stadio lascia distingue le due
zone apposta.
"""

from __future__ import annotations

from typing import Any

from ..prompt_opt import OptimizerConfig, optimize_text
from ..tokens import estimate_tokens
from .base import BaseStage, RequestContext


class PromptOptimizerStage(BaseStage):
    name = "prompt"
    riscrive = True  # Riscrive il testo **dentro** i messaggi, in posto.

    def __init__(self, settings: Any) -> None:
        self.config = settings.prompt
        self.enabled = self.config.enabled
        self._verified: frozenset[str] | None = None

    async def before(self, ctx: RequestContext) -> None:
        opzioni = OptimizerConfig(
            normalize_text=self.config.normalize,
            strip_filler=self.config.strip_filler,
            substitute=self.config.substitute,
            verified=await self._verified_set(ctx),
            only_verified=self.config.only_verified,
        )
        if not (opzioni.normalize_text or opzioni.strip_filler or opzioni.substitute):
            return

        bersagli = set(self.config.targets)
        tolti_prefisso = 0
        tolti_coda = 0
        tecniche: set[str] = set()

        if "system" in bersagli:
            tolti_prefisso += self._ottimizza_blocchi(
                ctx.params.get("system"), opzioni, tecniche
            )

        if "user" in bersagli:
            messaggi = ctx.params.get("messages") or []
            for indice, messaggio in enumerate(messaggi):
                if messaggio.get("role") != "user":
                    continue
                tolti = self._ottimizza_messaggio(messaggio, opzioni, tecniche)
                # L'ultimo messaggio e' l'unico che nessuna cache ha ancora
                # visto: li' i token tolti si pagano a prezzo pieno.
                if indice == len(messaggi) - 1:
                    tolti_coda += tolti
                else:
                    tolti_prefisso += tolti

        totale = tolti_prefisso + tolti_coda
        if not totale:
            return

        ctx.prompt_tokens_removed = totale
        ctx.prompt_tokens_removed_uncached = tolti_coda
        ctx.note(
            f"prompt riscritto ({', '.join(sorted(tecniche))}): {totale} token stimati in "
            f"meno, di cui {tolti_coda} fuori dal prefisso in cache"
        )

    # -- applicazione ------------------------------------------------------

    def _ottimizza_messaggio(
        self, messaggio: dict[str, Any], opzioni: OptimizerConfig, tecniche: set[str]
    ) -> int:
        content = messaggio.get("content")
        if isinstance(content, str):
            nuovo, tolti, applicati = self._ottimizza_stringa(content, opzioni)
            if tolti:
                messaggio["content"] = nuovo
                tecniche.update(applicati)
            return tolti
        return self._ottimizza_blocchi(content, opzioni, tecniche)

    def _ottimizza_blocchi(
        self, content: Any, opzioni: OptimizerConfig, tecniche: set[str]
    ) -> int:
        if not isinstance(content, list):
            return 0
        tolti = 0
        for blocco in content:
            # Solo blocchi di testo: un tool_result e' un'osservazione, non
            # una nostra istruzione da riformulare.
            if not isinstance(blocco, dict) or blocco.get("type") != "text":
                continue
            nuovo, risparmio, applicati = self._ottimizza_stringa(
                str(blocco.get("text", "")), opzioni
            )
            if risparmio:
                blocco["text"] = nuovo
                tecniche.update(applicati)
                tolti += risparmio
        return tolti

    def _ottimizza_stringa(
        self, testo: str, opzioni: OptimizerConfig
    ) -> tuple[str, int, list[str]]:
        if len(testo) < self.config.min_chars:
            return testo, 0, []
        esito = optimize_text(testo, opzioni)
        if not esito.changed:
            return testo, 0, []
        risparmio = estimate_tokens(testo) - estimate_tokens(esito.text)
        if risparmio <= 0:
            # La stima non vede un guadagno: si lascia il testo originale
            # invece di cambiarlo per niente, perche' ogni cambiamento e' un
            # rischio e un'invalidazione di cache in piu'.
            return testo, 0, []
        return esito.text, risparmio, esito.applied

    async def _verified_set(self, ctx: RequestContext) -> frozenset[str]:
        """Sostituzioni confermate dal conteggio vero, lette una volta sola."""
        if self._verified is None:
            if ctx.store is None:
                self._verified = frozenset()
            else:
                self._verified = frozenset(await ctx.store.verified_substitutions())
        return self._verified
