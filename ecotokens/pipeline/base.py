"""Ossatura della pipeline: contesto condiviso e protocollo degli stadi.

Ogni stadio vede lo stesso ``RequestContext`` e puo':

* riscrivere ``ctx.params`` prima dell'invio (compattazione, memoria, cache planner);
* interrompere la catena valorizzando ``ctx.short_circuit`` (hit di cache);
* rifiutare la richiesta sollevando ``PipelineAbort`` (budget esaurito).

``before`` gira nell'ordine dichiarato, ``after`` in ordine inverso: uno stadio
che ha modificato la richiesta e' quindi il primo a poter osservare l'esito.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..api.schemas import ChatCompletionRequest
from ..config import Settings
from ..pricing import Usage
from ..store.repos import Session, Store
from ..tokens import TokenCounter

# Fonti possibili di una risposta, per la contabilita'.
SOURCE_API = "api"
SOURCE_EXACT_CACHE = "exact_cache"
SOURCE_SEMANTIC_CACHE = "semantic_cache"

# Forma della porta da cui la richiesta e' entrata. Il gateway parla con un
# solo provider - Anthropic - ma accetta due dialetti: quello OpenAI, per le
# applicazioni che gia' esistono, e quello nativo di Claude, per i client che
# lo parlano gia' e non hanno bisogno di nessuna traduzione.
FORMAT_OPENAI = "openai"
FORMAT_ANTHROPIC = "anthropic"

logger = logging.getLogger("ecotokens.pipeline")


class PipelineAbort(Exception):
    """Interrompe la richiesta prima di spendere token."""

    def __init__(self, message: str, *, status_code: int = 429, error_type: str = "budget_exceeded") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


@dataclass
class RequestContext:
    # Assente per le richieste native: quelle non passano da nessuna
    # traduzione, quindi non esiste una versione OpenAI da conservare.
    request: ChatCompletionRequest | None
    settings: Settings
    store: Store
    client: Any
    counter: TokenCounter
    completion_id: str

    model: str
    params: dict[str, Any]
    stream: bool
    # Il modello che il client aveva chiesto. Il router puo' riscrivere
    # `model`, e da quel momento non esiste piu' nessun posto da cui risalire
    # a cosa sarebbe successo senza gateway: e' la definizione stessa di
    # baseline che se ne va. Si valorizza da sola in __post_init__, cosi'
    # nessun costruttore puo' dimenticarsene.
    requested_model: str = ""
    # Il nome **come il client lo ha scritto**, prima di qualunque
    # normalizzazione. `requested_model` non basta: `resolve_model` ripiega sul
    # default quando non riconosce un nome, quindi a valle un `llama-3.3-70b`
    # e' gia' diventato `claude-opus-5` e nessuno puo' piu' accorgersi che il
    # costo sta per essere calcolato con le tariffe di un altro modello.
    nome_richiesto_grezzo: str = ""
    # Dialetto della richiesta in arrivo. Gli stadi non devono guardarlo:
    # lavorano tutti sui parametri Anthropic, che sono gli stessi in
    # entrambi i casi. Serve alle rotte, per sapere come rispondere.
    client_format: str = FORMAT_OPENAI
    # Effort chiesto esplicitamente dal client, se l'ha chiesto. Il router
    # non lo tocca: e' una scelta di chi ha scritto l'applicazione.
    client_effort: str | None = None
    # Header HTTP della richiesta, in minuscolo.
    headers: dict[str, str] = field(default_factory=dict)

    notes: list[str] = field(default_factory=list)
    # Le stesse note, ma attribuite allo stadio che le ha prodotte. Non e' una
    # duplicazione oziosa: `notes` contiene anche cio' che nasce fuori dalla
    # pipeline - le note della traduzione, per esempio - e attribuire quelle a
    # uno stadio sarebbe falso. L'attribuzione la fa la pipeline osservando
    # cosa e' comparso durante la chiamata, non lo stadio dichiarandola: uno
    # stadio che smette di fare qualcosa smette di essere contato, senza che
    # nessuno debba ricordarsi di aggiornare un contatore.
    stage_notes: dict[str, list[str]] = field(default_factory=dict)
    # Gli stadi accesi per questa richiesta. Serve a distinguere "non ha fatto
    # niente" da "era spento": senza il denominatore, un contatore a zero non
    # dice quale delle due.
    stages_enabled: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    betas: list[str] = field(default_factory=list)

    session: Session | None = None
    session_is_new: bool = True
    fingerprint: str | None = None
    # Turni assistant gia' presenti nella cronologia in arrivo. E' il segnale
    # piu' affidabile per sapere se esiste gia' un prefisso in cache: non
    # dipende da cosa il gateway ha registrato in passato.
    history_turns: int = 0
    # Firma normalizzata dei messaggi cosi' come sono arrivati dal client,
    # prima di ogni riscrittura interna: e' il metro del riconoscimento di
    # sessione, e deve restare la vista del client, non la nostra.
    incoming_signature: list[str] = field(default_factory=list)

    cache_key: str | None = None
    short_circuit: dict[str, Any] | None = None
    source: str = SOURCE_API
    cache_ttl: str = "5m"

    estimated_prompt_tokens: int = 0
    # Token di `tools` + `system` **come li ha mandati il client**, prima che
    # qualunque stadio li tocchi. E' il prefisso che un client senza gateway
    # metterebbe in cache da solo con un `cache_control`, e serve a prezzare la
    # baseline realistica: senza, il gateway si prende il merito di uno sconto
    # che Anthropic fa comunque a chiunque.
    stable_prefix_tokens: int = 0
    # Impronta dello stesso prefisso. Serve a sapere se qualcun altro lo ha
    # gia' fatto passare di qui: e' cosi' che si stabilisce se il concorrente
    # senza gateway lo avrebbe avuto in cache, senza dedurlo da cio' che
    # abbiamo deciso noi.
    stable_prefix_hash: str = ""
    # Token tolti dalla riscrittura del prompt, e quanti di quelli stavano
    # fuori dal prefisso servito da cache. La distinzione conta: un token tolto
    # al prefisso in cache vale un decimo di uno tolto alla coda.
    prompt_tokens_removed: int = 0
    prompt_tokens_removed_uncached: int = 0
    # Token che il gateway aggiunge di suo al prompt: delimitatori attorno al
    # riassunto, blocco della memoria, istruzioni di formato. L'utente li paga
    # senza averli scritti, quindi vanno contati come tutto il resto.
    overhead_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    # Compilati dallo stadio di contabilita' dopo la risposta.
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    # Cosa avrebbe pagato un client senza gateway che usa la cache da se'.
    # Fra questa e `baseline_cost_usd` sta lo sconto che Anthropic fa a
    # chiunque; fra questa e `cost_usd` sta quello che aggiunge EcoTokens.
    baseline_ingenua_usd: float = 0.0
    # Spesa delle chiamate che il gateway fa per conto proprio: riassunti di
    # compattazione, estrazione dei fatti da ricordare. Non compare in
    # `response.usage` perche' non appartiene alla risposta dell'utente, ma
    # l'utente la paga. Tenerla fuori dal conto farebbe sembrare gratuito ogni
    # stadio che chiama un modello.
    aux_usage: Usage = field(default_factory=Usage)
    aux_cost_usd: float = 0.0
    # Risposta finale nel formato atteso dal client, valorizzata dalla rotta
    # prima di eseguire gli stadi `after`: gli stadi di cache la salvano da
    # qui. Il formato e' quello della porta d'ingresso, non quello interno.
    client_response: dict[str, Any] | None = None
    # Vero quando la risposta e' arrivata **tagliata**: lo stream si e' chiuso
    # senza dire di essere finito. Il prompt e' gia' stato pagato per intero,
    # quindi la spesa va contata; ma la risposta non va ne' messa in cache ne'
    # usata per estrarre fatti da ricordare, o un guasto momentaneo di rete
    # diventerebbe permanente - servito uguale a ogni richiesta successiva.
    risposta_incompleta: bool = False
    # La stessa risposta in formato Anthropic: e' questa che finisce in
    # cache. Il formato interno del gateway e' uno solo, e deve esserlo
    # anche quello salvato, altrimenti un hit servito a un client dell'altro
    # dialetto restituirebbe una risposta della forma sbagliata.
    upstream_response: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.requested_model:
            self.requested_model = self.model
        if not self.stable_prefix_tokens:
            # Qui, non piu' tardi: fra un istante gli stadi cominciano a
            # riscrivere, e il prefisso di cui si vuole il peso e' quello che
            # il client ha scritto, non quello che il gateway ha prodotto.
            from ..tokens import estimate_content_tokens, estimate_tools_tokens

            self.stable_prefix_tokens = estimate_tools_tokens(
                self.params.get("tools")
            ) + estimate_content_tokens(self.params.get("system"))
            impronta = hashlib.sha256(
                json.dumps(
                    [self.params.get("tools"), self.params.get("system")],
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            )
            self.stable_prefix_hash = impronta.hexdigest()[:32]

    @property
    def session_id(self) -> str | None:
        return self.session.id if self.session else None

    @property
    def total_cost_usd(self) -> float:
        """Quanto e' costata davvero la richiesta, chiamate interne comprese."""
        return self.cost_usd + self.aux_cost_usd

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000

    def has_tools(self) -> bool:
        """Vero se la richiesta dichiara dei tool.

        Si legge dai parametri, non dalla richiesta in arrivo: cosi' la
        risposta e' la stessa da qualunque porta sia entrata.
        """
        return bool(self.params.get("tools"))

    def note(self, message: str) -> None:
        self.notes.append(message)

    def attribuisci(self, stadio: str, note: list[str]) -> None:
        """Assegna a uno stadio le note comparse mentre girava."""
        if note:
            self.stage_notes.setdefault(stadio, []).extend(note)

    def use_beta(self, flag: str) -> None:
        """Segna un beta header necessario: la chiamata passera' da client.beta."""
        if flag not in self.betas:
            self.betas.append(flag)

    def meta(self) -> dict[str, Any]:
        """Blocco diagnostico allegato alla risposta come campo ``ecotokens``."""
        blocco = {
            "source": self.source,
            "model": self.model,
            "session_id": self.session_id,
            "cost_usd": round(self.cost_usd, 6),
            "saved_usd": round(self.saved_usd, 6),
            "cached_prompt_tokens": self.usage.cache_read_tokens,
            "notes": list(self.notes),
        }
        if self.aux_cost_usd:
            blocco["aux_cost_usd"] = round(self.aux_cost_usd, 6)
        return blocco


class Stage(Protocol):
    """Uno stadio della pipeline."""

    name: str

    async def before(self, ctx: RequestContext) -> None: ...

    async def after(self, ctx: RequestContext, message: Any | None) -> None: ...


class BaseStage:
    """Implementazione vuota: gli stadi ridefiniscono solo cio' che serve."""

    name = "stage"
    enabled = True
    # Se lo stadio **riscrive** ``ctx.params`` invece di limitarsi a leggerli.
    #
    # Governa il salvataggio che permette di annullare il lavoro di uno stadio
    # che si rompe a meta'. Copiare i parametri costa da 0,03 ms a 0,89 ms
    # secondo la lunghezza della conversazione (misurato, copia ricorsiva:
    # `copy.deepcopy` costa quattro volte tanto), e farlo attorno a ogni
    # stadio invece che una volta sola vorrebbe dire pagarlo otto volte per
    # una richiesta - un quarto del tempo di CPU dell'intero gateway.
    #
    # La dichiarazione non e' una promessa sulla parola: `test_guasti.py`
    # esegue ogni stadio e verifica che chi dichiara di non riscrivere non
    # riscriva.
    riscrive = False

    async def before(self, ctx: RequestContext) -> None:  # pragma: no cover - default
        return None

    async def after(self, ctx: RequestContext, message: Any | None) -> None:  # pragma: no cover
        return None


# Quanto puo' essere annidata una richiesta perche' il gateway la sappia
# salvare. Un prompt vero non supera i cinque o sei livelli: cento e' gia'
# molto oltre il ragionevole, e sta molto sotto il limite di ricorsione
# dell'interprete, che a 500 livelli si arrende con un RecursionError.
PROFONDITA_MASSIMA = 100


class TroppoAnnidato(ValueError):
    """La richiesta e' annidata piu' a fondo di quanto si possa copiare."""


def copia_parametri(valore: Any, profondita: int = 0) -> Any:
    """Copia i parametri isolando le strutture ma condividendo le stringhe.

    I parametri di una richiesta sono dati in forma JSON - dizionari, liste,
    stringhe, numeri - e per quella forma questa ricorsione costa **un quarto**
    di ``copy.deepcopy``, che deve occuparsi di oggetti arbitrari, riferimenti
    ciclici e tabelle di memoizzazione che qui non servono a niente.

    Le stringhe non si copiano: sono immutabili, e sono quasi tutto il peso.

    Il limite di profondita' non e' una cautela generica. Una ricorsione su
    dati che arrivano da fuori e' una via di guasto **aperta da chi la
    scrive**: un client che manda un contenuto annidato cinquecento volte
    esaurisce lo stack, e il RecursionError arriverebbe da un punto in cui non
    c'e' niente da fare. Contare mentre si copia costa un confronto per nodo
    e trasforma un errore dell'interprete in una decisione del gateway.
    """
    if profondita > PROFONDITA_MASSIMA:
        raise TroppoAnnidato(
            f"richiesta annidata oltre {PROFONDITA_MASSIMA} livelli"
        )
    if type(valore) is dict:
        return {
            chiave: copia_parametri(v, profondita + 1) for chiave, v in valore.items()
        }
    if type(valore) is list:
        return [copia_parametri(v, profondita + 1) for v in valore]
    return valore


# Quanti guasti di fila prima di spegnere uno stadio. Uno solo sarebbe troppo
# poco - un errore isolato non dice che lo stadio sia rotto - ma riprovare
# all'infinito uno stadio che fallisce sempre paga il salvataggio dei
# parametri a ogni richiesta senza mai ottenere niente in cambio.
GUASTI_PRIMA_DI_SPEGNERE = 3


class Pipeline:
    """Gli stadi, in ordine, con la regola che li governa quando si rompono.

    **Un guasto interno degrada, non abbatte.** Il gateway sta in mezzo fra
    un'applicazione e l'API: se un suo stadio ha un bug, la richiesta deve
    partire come sarebbe partita senza quello stadio - piu' cara, non fallita.
    Un ottimizzatore che puo' far fallire cio' che ottimizza non e' un
    ottimizzatore rischioso: e' un guasto in piu' che prima non c'era.

    L'unica eccezione e' ``PipelineAbort``, che non e' un guasto ma una
    decisione: e' cosi' che il tetto di spesa dice di no, e deve continuare a
    poterlo dire.
    """

    def __init__(
        self,
        stages: list[BaseStage],
        *,
        guasti_prima_di_spegnere: int = GUASTI_PRIMA_DI_SPEGNERE,
    ) -> None:
        self.stages = stages
        self.guasti_prima_di_spegnere = guasti_prima_di_spegnere
        # Nome dello stadio -> cosa gli e' successo. Degradare in silenzio e'
        # l'altro modo di sbagliare: uno stadio spento da un bug continuerebbe
        # a risultare acceso, e il risparmio mancante verrebbe attribuito a
        # chissa' che cosa.
        self.guasti: dict[str, dict[str, Any]] = {}

    # -- guasti -----------------------------------------------------------

    def _registra_guasto(
        self, stage: BaseStage, errore: Exception, ctx: RequestContext, dove: str
    ) -> None:
        voce = self.guasti.setdefault(
            stage.name,
            {"conteggio": 0, "consecutivi": 0, "ultimo": "", "spento": False, "dove": dove},
        )
        voce["conteggio"] += 1
        # I due agganci si contano separati. Uno stadio rotto in `before` ha
        # quasi sempre un `after` che non fa niente e quindi riesce: contarli
        # insieme lascerebbe il conteggio consecutivo a zero per sempre, e lo
        # stadio rotto non verrebbe mai spento. E' il test dello spegnimento
        # ad averlo trovato, non la lettura del codice.
        if voce["dove"] != dove:
            voce["dove"] = dove
            voce["consecutivi"] = 0
        voce["consecutivi"] += 1
        voce["ultimo"] = f"{type(errore).__name__}: {errore}"
        logger.warning(
            "stadio %s: %s (guasto n. %d)", stage.name, voce["ultimo"], voce["conteggio"]
        )
        if voce["consecutivi"] >= self.guasti_prima_di_spegnere and not voce["spento"]:
            stage.enabled = False
            voce["spento"] = True
            logger.error(
                "stadio %s disattivato dopo %d guasti consecutivi",
                stage.name,
                voce["consecutivi"],
            )
        # Al client arriva **che** lo stadio non ha lavorato, non perche': il
        # testo di un'eccezione interna puo' contenere percorsi, query e
        # frammenti di configurazione, e la risposta esce dal gateway. Il
        # dettaglio resta nel log e in `pipeline.guasti`.
        ctx.note(f"stadio {stage.name} non applicato: guasto interno")

    def _successo(self, stage: BaseStage, dove: str) -> None:
        """Azzera la serie, ma solo per l'aggancio che e' andato a buon fine."""
        voce = self.guasti.get(stage.name)
        if voce and voce["consecutivi"] and voce["dove"] == dove:
            voce["consecutivi"] = 0

    # -- salvataggio e ripristino ------------------------------------------

    def _istantanea(self, ctx: RequestContext) -> dict[str, Any]:
        return {
            "params": copia_parametri(ctx.params),
            "model": ctx.model,
            "cache_ttl": ctx.cache_ttl,
            "betas": list(ctx.betas),
        }

    def _ripristina(self, ctx: RequestContext, istantanea: dict[str, Any]) -> None:
        ctx.params = copia_parametri(istantanea["params"])
        ctx.model = istantanea["model"]
        ctx.cache_ttl = istantanea["cache_ttl"]
        ctx.betas = list(istantanea["betas"])
        # Uno stadio puo' essersi rotto **dopo** aver deciso di servire da
        # cache: quella decisione va annullata come tutto il resto, o si
        # restituirebbe una risposta scelta da un'esecuzione mai finita.
        ctx.short_circuit = None

    # -- esecuzione --------------------------------------------------------

    async def before(self, ctx: RequestContext) -> None:
        ctx.stages_enabled = [
            stage.name for stage in self.stages if getattr(stage, "enabled", True)
        ]
        for stage in self.stages:
            if not getattr(stage, "enabled", True):
                continue
            # Una copia per **ogni** stadio che riscrive, non una per
            # richiesta: cosi' uno stadio che si rompe perde soltanto il
            # proprio lavoro, e la compattazione non viene buttata perche' il
            # pianificatore di cache, tre righe dopo, ha un bug.
            #
            # La prima versione ne faceva una sola, per un conto a tavolino
            # che dava il costo per stadio troppo alto. Misurato: la
            # differenza fra protetto e non protetto sta **sotto il rumore**
            # dello strumento a 0, 10 e 40 turni, perche' il conto rapportava
            # la copia al tempo di CPU di una richiesta corta invece che a
            # quello di una richiesta lunga, cioe' proprio quella in cui la
            # copia costa. Gli stadi che leggono soltanto non la pagano.
            prima = len(ctx.notes)
            istantanea = None
            try:
                # Dentro il `try`, non prima: anche il salvataggio puo'
                # fallire - su una richiesta annidata oltre ogni limite - e un
                # gateway che muore mentre prepara la propria rete di
                # sicurezza sarebbe il modo piu' ironico di rompersi.
                istantanea = (
                    self._istantanea(ctx) if getattr(stage, "riscrive", False) else None
                )
                await stage.before(ctx)
            except PipelineAbort:
                # Non e' un guasto: e' uno stadio che fa il suo mestiere.
                raise
            except Exception as errore:
                # Le note gia' scritte dallo stadio descrivono un lavoro che
                # sta per essere annullato: tenerle significherebbe dichiarare
                # al client un'ottimizzazione che non c'e'.
                del ctx.notes[prima:]
                # Resta `None` se a fallire e' stato il salvataggio stesso:
                # in quel caso lo stadio non e' nemmeno partito, e non c'e'
                # niente da annullare.
                if istantanea is not None:
                    self._ripristina(ctx, istantanea)
                else:
                    ctx.short_circuit = None
                self._registra_guasto(stage, errore, ctx, "before")
                ctx.attribuisci(stage.name, ctx.notes[prima:])
                continue

            self._successo(stage, "before")
            ctx.attribuisci(stage.name, ctx.notes[prima:])
            if ctx.short_circuit is not None:
                # Un hit di cache rende inutile tutto il resto della catena.
                return

    async def after(self, ctx: RequestContext, message: Any | None) -> None:
        # Qui nessun salvataggio: la richiesta e' gia' partita e i parametri
        # non servono piu' a nessuno. Uno stadio che si rompe dopo la risposta
        # perde il proprio lavoro - una riga di registro, un fatto da
        # ricordare - senza poter danneggiare la risposta gia' pagata.
        for stage in reversed(self.stages):
            if not getattr(stage, "enabled", True):
                continue
            prima = len(ctx.notes)
            try:
                await stage.after(ctx, message)
            except PipelineAbort:
                raise
            except Exception as errore:
                del ctx.notes[prima:]
                self._registra_guasto(stage, errore, ctx, "after")
            else:
                self._successo(stage, "after")
            ctx.attribuisci(stage.name, ctx.notes[prima:])
