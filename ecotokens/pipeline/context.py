"""Gestione della finestra di contesto.

Due meccanismi, applicati in quest'ordine perche' hanno costi molto diversi.

**Potatura lato server** (``context_management``): elimina dalla richiesta i
vecchi ``tool_result`` e, se richiesto, i blocchi di pensiero. Non costa
chiamate aggiuntive, non riscrive nulla di quello che l'utente ha detto ed e'
quindi il primo strumento da usare. In un ciclo agentico i tool result sono
quasi sempre la voce di spesa piu' grossa del prompt.

**Riassunto locale**: quando neanche la potatura basta, la parte vecchia della
conversazione viene sostituita da un riassunto prodotto da un modello
economico.

Il vincolo che governa tutto questo stadio e' che comprimere e mettere in cache
tirano in direzioni opposte. Il prompt caching e' un match di prefisso: se la
compattazione riscrive l'inizio del prompt a ogni turno, ogni turno manca la
cache e si paga il prezzo pieno su tutto. Comprimere il 40% dei token e poi
pagarli 10 volte tanto e' una perdita. Da qui le cinque regole applicate qui:

1. **Il punto di taglio avanza a scatti**, non insegue la coda della
   conversazione. Un taglio che si sposta di due messaggi a ogni turno produce
   un riassunto diverso a ogni turno; a scatti di ``recompute_every_messages``
   lo stesso riassunto vale per molti turni di fila e il prefisso resta fermo.
2. **Il riassunto e' incrementale**: quando il taglio avanza si riparte da
   quello precedente e si leggono solo i messaggi aggiunti, invece di rileggere
   tutta la cronologia.
3. **Il riassunto ha un tetto rigido.** Questi token si pagano una volta in
   output e poi a ogni turno in input: un riassunto prolisso e' un costo
   ricorrente.
4. **La trascrizione data al riassuntore e' troncata**: per registrare che un
   file e' stato letto non serve rispedire il file.
5. **Non si comprime se non conviene**: sotto ``min_gain_tokens`` la chiamata
   di riassunto costa piu' di quanto la compressione fa risparmiare.

Nota sulla compattazione server-side dell'API (``compact_20260112``): non e'
usata qui perche' richiede di riaccodare i blocchi di compattazione a ogni
turno successivo, mentre un client OpenAI rispedisce la propria cronologia e
quei blocchi andrebbero persi al primo giro.
"""

from __future__ import annotations

import logging
from typing import Any

from ..pricing import Usage, cost_usd, model_info, resolve_model
from ..wording import (
    MERGE_RULES,
    NEW_CLOSE,
    NEW_OPEN,
    NOTES_CLOSE,
    NOTES_OPEN,
    SUMMARY_CLOSE,
    SUMMARY_OPEN,
    SUMMARY_RULES,
    TOOL_CALL,
    TOOL_RESULT,
    wrap,
)
from ..tokens import estimate_content_tokens, estimate_prompt_tokens, estimate_tokens
from .base import BaseStage, RequestContext

logger = logging.getLogger("ecotokens.context")

CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"

# Il riassuntore e' l'unico posto del gateway dove scriviamo un prompt per un
# modello, quindi e' l'unico posto dove possiamo essere prolissi per sbaglio.
# Queste istruzioni sono scritte per ottenere appunti, non prosa: la prosa
# ricostruisce il contesto con frasi complete, e le frasi complete sono i token
# che stiamo cercando di togliere.
# Rapporto usato per tradurre il tetto in token in un tetto in righe. Il modello
# rispetta molto meglio un limite espresso in righe che uno espresso in token.
TOKEN_PER_RIGA = 25


class ContextStage(BaseStage):
    name = "context"

    def __init__(self, settings: Any) -> None:
        self.config = settings.context
        self.enabled = self.config.enabled

    async def before(self, ctx: RequestContext) -> None:
        info = model_info(ctx.model)
        window = info.context_window
        estimated = estimate_prompt_tokens(ctx.params)
        ctx.estimated_prompt_tokens = estimated
        ratio = estimated / window if window else 0.0

        # Due condizioni indipendenti, perche' rispondono a due domande diverse.
        # La frazione della finestra risponde a "sono in pericolo di sforare".
        # La quantita' di materiale potabile risponde a "conviene potare", che
        # non dipende dalla finestra del modello - e le finestre vanno da 200k a
        # un milione, quindi la stessa frazione significa cose molto diverse.
        in_pericolo = ratio >= self.config.trigger_ratio
        conviene = self._prunable_tokens(ctx) >= self.config.prune_min_prunable_tokens
        if not (in_pericolo or conviene):
            return

        self._apply_server_pruning(ctx, estimated, window)

        if self.config.local_compaction and ratio >= self.config.hard_ratio:
            await self._apply_local_summary(ctx)

    # -- potatura lato server ---------------------------------------------

    def _apply_server_pruning(self, ctx: RequestContext, estimated: int, window: int) -> None:
        edits: list[dict[str, Any]] = []
        if self.config.clear_tool_uses:
            edits.append(self._clear_tool_uses_edit(ctx))
        if self.config.clear_thinking:
            edits.append({"type": "clear_thinking_20251015"})
        if not edits:
            return

        ctx.params["context_management"] = {"edits": edits}
        ctx.use_beta(CONTEXT_MANAGEMENT_BETA)
        ctx.note(
            f"potatura lato server attiva: prompt stimato {estimated} token su "
            f"{window} di finestra"
        )

    def _prunable_tokens(self, ctx: RequestContext) -> int:
        """Token di risultati di tool che si potrebbero svuotare.

        E' la misura di quanto la potatura *renderebbe*, indipendente dalla
        finestra del modello. Conta solo cio' che sta oltre i risultati recenti
        conservati: quelli non si toccano mai.
        """
        blocchi = [
            blocco
            for messaggio in (ctx.params.get("messages") or [])
            for blocco in (messaggio.get("content") or [])
            if isinstance(blocco, dict) and blocco.get("type") == "tool_result"
        ]
        potabili = blocchi[: max(0, len(blocchi) - max(0, self.config.prune_keep_tool_uses))]
        return sum(estimate_content_tokens(blocco.get("content")) for blocco in potabili)

    def _clear_tool_uses_edit(self, ctx: RequestContext) -> dict[str, Any]:
        """Costruisce l'edit, decidendo *quanti* risultati conservare.

        Il parametro ``keep`` dello schema ufficiale dice quanti risultati
        recenti restano interi. Lasciarlo al valore predefinito del server
        sembra la scelta neutra e invece e' quella che rompe la cache: con un
        ``keep`` fisso il confine di potatura sta sempre a N dal fondo, quindi
        si sposta in avanti di un risultato a ogni turno, e l'insieme dei
        blocchi svuotati e' diverso a ogni richiesta. Il prefisso cambia
        sempre, e la cache non trova mai niente.

        Qui si ragiona al contrario: si sceglie **quanti potarne dall'inizio**,
        a scatti, e da quello si ricava ``keep``. Fra uno scatto e l'altro
        vengono svuotati esattamente gli stessi blocchi, quindi il prefisso
        resta fermo e la cache regge. E' la stessa correzione gia' applicata al
        punto di taglio della compattazione, tradotta in un parametro dell'API.
        """
        edit: dict[str, Any] = {"type": "clear_tool_uses_20250919"}

        totale = _conta_tool_result(ctx.params.get("messages") or [])
        minimo = max(0, self.config.prune_keep_tool_uses)
        if totale <= minimo:
            return edit

        # Lo scatto si misura in *turni*, non in risultati, ed e' la differenza
        # fra funzionare e non funzionare. Un ciclo agentico con sei chiamate
        # per turno e uno che ne fa una consumano lo stesso scatto a velocita'
        # diverse di sei volte: contato in risultati, lo stesso numero produce
        # otto turni di stabilita' in un caso e nemmeno due nell'altro.
        # Misurato: contato in risultati i due carichi vogliono valori opposti,
        # contato in turni ne vogliono uno solo.
        per_turno = totale / max(1, ctx.history_turns)
        step = max(1, round(per_turno * self.config.prune_step_turns))
        potabili = totale - minimo
        potati = (potabili // step) * step
        if potati <= 0:
            # Non c'e' ancora abbastanza materiale per uno scatto intero: si
            # lascia il contesto intatto invece di potare una sfoglia e
            # buttare via la cache per pochi token.
            ctx.note(
                f"potatura rinviata: {potabili} risultati potabili, meno di uno "
                f"scatto da {step} ({self.config.prune_step_turns} turni)"
            )
            return edit | {"keep": {"type": "tool_uses", "value": totale}}

        edit["keep"] = {"type": "tool_uses", "value": totale - potati}
        if self.config.prune_clear_at_least_tokens:
            edit["clear_at_least"] = {
                "type": "input_tokens",
                "value": self.config.prune_clear_at_least_tokens,
            }
        if self.config.prune_exclude_tools:
            edit["exclude_tools"] = list(self.config.prune_exclude_tools)
        ctx.note(
            f"potatura a scatti: {potati} risultati svuotati su {totale}, "
            f"gli stessi per i prossimi {self.config.prune_step_turns} turni"
        )
        return edit

    # -- riassunto locale --------------------------------------------------

    async def _apply_local_summary(self, ctx: RequestContext) -> None:
        messages = ctx.params.get("messages") or []
        cut = self._cut_point(ctx, messages)
        if cut <= 1:
            return

        before_tokens = estimate_content_tokens(
            [message.get("content") for message in messages[:cut]]
        )
        if before_tokens < self.config.min_gain_tokens:
            ctx.note(
                f"compattazione saltata: i {cut} messaggi vecchi valgono {before_tokens} "
                f"token, sotto i {self.config.min_gain_tokens} che ripagano la chiamata"
            )
            return

        summary = None
        if ctx.session is not None:
            summary = await ctx.store.get_summary(ctx.session.id, cut)

        if summary is None:
            summary = await self._produce_summary(ctx, messages, cut)
            if summary is None:
                return
            if ctx.session is not None:
                await ctx.store.put_summary(ctx.session.id, cut, summary)
        else:
            ctx.note(f"riusato il riassunto dei primi {cut} messaggi: prefisso invariato")

        replacement = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<riassunto-conversazione-precedente>\n{summary}\n"
                        "</riassunto-conversazione-precedente>",
                    }
                ],
            }
        ]
        ctx.params["messages"] = replacement + messages[cut:]
        ctx.overhead_tokens += estimate_tokens(SUMMARY_OPEN) + estimate_tokens(SUMMARY_CLOSE)
        after_tokens = estimate_content_tokens(replacement[0]["content"])
        ctx.note(
            f"compattazione: {before_tokens} token di cronologia sostituiti da "
            f"{after_tokens} ({1 - after_tokens / before_tokens:.0%} in meno)"
        )

    def _cut_point(self, ctx: RequestContext, messages: list[dict[str, Any]]) -> int:
        """Dove finisce la parte riassunta e comincia quella integrale.

        Il taglio avanza a scatti di ``recompute_every_messages``. E' la regola
        che rende la compattazione compatibile con il prompt caching: seguendo
        la coda della conversazione il taglio si sposterebbe a ogni turno,
        quindi il riassunto sarebbe sempre nuovo, quindi il prefisso sarebbe
        sempre diverso e la cache mancherebbe a ogni richiesta. A scatti, lo
        stesso riassunto vale per molti turni consecutivi.
        """
        grezzo = len(messages) - self.config.keep_recent_messages
        if grezzo <= 1:
            return 0

        step = max(1, self.config.recompute_every_messages)
        cut = (grezzo // step) * step
        if cut <= 1:
            # La conversazione non ha ancora riempito uno scatto intero ma la
            # soglia dura e' gia' stata superata: qui il rischio e' sforare la
            # finestra, e non sforare conta piu' della stabilita' del prefisso.
            cut = grezzo
            ctx.note(
                "taglio non allineato allo scatto: si comprime comunque per non "
                "sforare la finestra, il prefisso cambiera'"
            )

        # Il taglio non puo' cadere in mezzo a una coppia tool_use/tool_result:
        # un tool_result orfano fa fallire la richiesta con un 400.
        return _safe_cut_point(messages, cut)

    async def _produce_summary(
        self, ctx: RequestContext, messages: list[dict[str, Any]], cut: int
    ) -> str | None:
        """Calcola il riassunto dei primi ``cut`` messaggi.

        Se esiste gia' un riassunto per un taglio precedente si riparte da
        quello e si leggono solo i messaggi aggiunti nel frattempo: su una
        conversazione lunga e' la differenza fra rileggere tutta la cronologia
        a ogni scatto e rileggerne una fetta.
        """
        precedente = None
        if self.config.incremental_summary and ctx.session is not None:
            precedente = await ctx.store.get_previous_summary(ctx.session.id, cut)

        righe = max(5, self.config.summary_max_tokens // TOKEN_PER_RIGA)

        if precedente is not None:
            base_upto, base_text = precedente
            nuovi = self._render_transcript(messages[base_upto:cut])
            if not nuovi.strip():
                return base_text
            istruzioni = MERGE_RULES.format(righe=righe)
            corpo = (
                f"<appunti-finora>\n{base_text}\n</appunti-finora>\n\n"
                f"<messaggi-nuovi>\n{nuovi}\n</messaggi-nuovi>"
            )
            origine = f"riassunto esteso da {base_upto} a {cut} messaggi"
        else:
            corpo = self._render_transcript(messages[:cut])
            if not corpo.strip():
                return None
            istruzioni = SUMMARY_RULES.format(righe=righe)
            origine = f"riassunti i primi {cut} messaggi"

        summary = await self._call_summarizer(ctx, istruzioni, corpo)
        if summary is None:
            return None
        ctx.note(f"{origine} con {self.config.summary_model}")
        return summary

    async def _call_summarizer(
        self, ctx: RequestContext, istruzioni: str, corpo: str
    ) -> str | None:
        """Chiama il modello economico e mette la spesa a carico della richiesta.

        E' una fork della richiesta principale e non riusa la cache del padre:
        gira su un altro modello, e le voci di cache sono legate al modello.
        E' accettabile perche' succede una volta per scatto, non a ogni turno.
        """
        model = resolve_model(self.config.summary_model)
        try:
            response = await ctx.client.messages.create(
                model=model,
                max_tokens=self.config.summary_max_tokens,
                system=[{"type": "text", "text": istruzioni}],
                messages=[{"role": "user", "content": [{"type": "text", "text": corpo}]}],
                output_config={"effort": "low"},
            )
        except Exception as error:  # la compattazione non deve far fallire la richiesta
            logger.warning("riassunto non riuscito, si prosegue senza: %s", error)
            ctx.note("riassunto non riuscito: la conversazione resta integrale")
            return None

        self._charge(ctx, model, response)

        parts = [
            block.text
            for block in getattr(response, "content", []) or []
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts).strip() or None

    @staticmethod
    def _charge(ctx: RequestContext, model: str, response: Any) -> None:
        """Registra la spesa del riassuntore sul contesto della richiesta.

        Senza questo la compattazione risulterebbe gratuita in ogni misura, e
        uno stadio che sembra gratuito viene acceso quando non conviene.
        """
        usage = Usage.from_api(getattr(response, "usage", None))
        speso = cost_usd(model, usage, "5m")
        ctx.aux_usage = Usage(
            input_tokens=ctx.aux_usage.input_tokens + usage.input_tokens,
            output_tokens=ctx.aux_usage.output_tokens + usage.output_tokens,
            cache_creation_tokens=ctx.aux_usage.cache_creation_tokens
            + usage.cache_creation_tokens,
            cache_read_tokens=ctx.aux_usage.cache_read_tokens + usage.cache_read_tokens,
        )
        ctx.aux_cost_usd += speso
        ctx.note(
            f"il riassunto e' costato {usage.total_prompt_tokens} token di input e "
            f"{usage.output_tokens} di output su {model} ({speso:.6f} USD)"
        )

    def _render_transcript(self, messages: list[dict[str, Any]]) -> str:
        """Trascrizione compatta della parte da riassumere.

        Ogni blocco viene troncato al centro: l'inizio e la fine di un testo
        dicono quasi sempre di cosa si trattava, e per il riassuntore basta
        quello. Rispedire per intero un file letto o un risultato di tool
        significa pagare due volte gli stessi token, una all'andata e una nel
        riassunto.
        """
        limite = max(80, self.config.transcript_block_chars)
        lines: list[str] = []
        for message in messages:
            role = message.get("role", "?")
            content = message.get("content")
            if isinstance(content, str):
                text = _tronca(content, limite)
            elif isinstance(content, list):
                pieces = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        pieces.append(_tronca(str(block.get("text", "")), limite))
                    elif block.get("type") == "tool_use":
                        pieces.append(TOOL_CALL.format(name=block.get("name")))
                    elif block.get("type") == "tool_result":
                        # Il contenuto di un tool result e' gia' stato usato dal
                        # modello quando e' arrivato: al riassunto serve sapere
                        # che c'e' stato, non cosa diceva.
                        pieces.append(TOOL_RESULT)
                text = " ".join(pieces)
            else:
                text = ""
            if text.strip():
                lines.append(f"{role}: {text.strip()}")
        return "\n".join(lines)


def _tronca(text: str, limite: int) -> str:
    """Tronca al centro, conservando inizio e fine."""
    if len(text) <= limite:
        return text
    meta = limite // 2
    return f"{text[:meta]} […] {text[-meta:]}"


def _safe_cut_point(messages: list[dict[str, Any]], cut: int) -> int:
    """Sposta il taglio in modo da non separare un tool_use dal suo risultato."""
    while cut > 1 and _has_orphan_tool_result(messages[cut:]):
        cut -= 1
    return cut


def _has_orphan_tool_result(tail: list[dict[str, Any]]) -> bool:
    """Vero se la coda inizia con risultati di tool la cui chiamata resta fuori."""
    if not tail:
        return False
    first = tail[0]
    content = first.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _conta_tool_result(messaggi: list[dict[str, Any]]) -> int:
    """Quanti risultati di tool ci sono nella conversazione."""
    return sum(
        1
        for messaggio in messaggi
        for blocco in (messaggio.get("content") or [])
        if isinstance(blocco, dict) and blocco.get("type") == "tool_result"
    )
