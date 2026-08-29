"""Banco di misura: quanto costa lo stesso lavoro con e senza il gateway.

Il metodo e' un A/B onesto. Lo stesso identico carico di richieste viene
eseguito due volte, sullo stesso codice e sulla stessa strada usata dalle
richieste vere (``Gateway.complete``), cambiando una cosa sola: gli stadi di
ottimizzazione accesi o spenti.

* **senza gateway** - tutti gli stadi spenti. Resta soltanto la traduzione
  OpenAI verso Anthropic, che non e' un'ottimizzazione ma una necessita': senza
  di essa la richiesta verrebbe rifiutata dall'API. E' la definizione onesta di
  "prima".
* **con gateway** - la configurazione in esame.

Ogni combinazione scenario/variante parte da un database vuoto e da un
simulatore nuovo: senza questo, la cache riempita da una misura falserebbe la
successiva, che e' il modo piu' facile di misurare un risparmio che non esiste.

L'**ablazione** accende gli stadi uno alla volta, in modo cumulativo: la
differenza fra due gradini e' il contributo di quello stadio, misurato invece
che dichiarato.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic
import httpx2

from .api.schemas import ChatCompletionRequest
from .cache_audit import CacheEvent, CacheWriteAudit, audit_cache_writes
from .config import Settings
from .pipeline.base import SOURCE_API
from .simulator import create_stub
from .store.db import Database
from .store.repos import Store
from .varianti import (
    BASELINE_VARIANT,
    FULL_VARIANT,
    NOMI_ABLAZIONE,
    RIFERIMENTO_MODERNO,
    ULTIMO_GRADINO,
    ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA,
)
from .workloads import Scenario, all_scenarios, corpus_fingerprint


@dataclass
class Measurement:
    """Esito di uno scenario sotto una variante di configurazione."""

    scenario: str
    variant: str
    requests: int = 0
    upstream_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    full_price_tokens: int = 0
    cost_usd: float = 0.0
    # Quota di `cost_usd` spesa dalle chiamate che il gateway fa per conto
    # proprio (il riassunto di compattazione). Tenuta separata perche' e' il
    # prezzo dell'ottimizzazione, non della richiesta dell'utente.
    aux_cost_usd: float = 0.0
    latency_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def cache_ratio(self) -> float:
        return self.cache_read_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


@dataclass
class Comparison:
    """Confronto fra la variante di riferimento e quella in esame."""

    scenario: str
    before: Measurement
    after: Measurement

    @property
    def saved_usd(self) -> float:
        return self.before.cost_usd - self.after.cost_usd

    @property
    def saved_ratio(self) -> float:
        return self.saved_usd / self.before.cost_usd if self.before.cost_usd else 0.0

    @property
    def tokens_avoided(self) -> int:
        """Token di prompt che non sono stati pagati a prezzo pieno."""
        return self.before.full_price_tokens - self.after.full_price_tokens


@dataclass
class BenchRun:
    id: str
    label: str
    mode: str
    created_at: float
    # Impronta del contenuto del corpus. Due misure con impronte diverse non
    # sono confrontabili, anche quando l'elenco degli scenari coincide.
    fingerprint: str = ""
    measurements: list[Measurement] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)

    def totals(self, variant: str, *, scenario: str | None = None) -> Measurement:
        """Somma delle misure di una variante, o di un suo solo scenario.

        Il filtro per scenario serve a non perdere la forbice: l'aggregato di
        cinque carichi diversi nasconde che su uno il guadagno e' dell'82% e su
        un altro del 6%, che e' l'informazione con cui si decide se il gateway
        serve al proprio caso.
        """
        aggregato = Measurement(scenario=scenario or "tutti", variant=variant)
        for misura in self.measurements:
            if misura.variant != variant:
                continue
            if scenario is not None and misura.scenario != scenario:
                continue
            aggregato.requests += misura.requests
            aggregato.upstream_calls += misura.upstream_calls
            aggregato.prompt_tokens += misura.prompt_tokens
            aggregato.output_tokens += misura.output_tokens
            aggregato.cache_read_tokens += misura.cache_read_tokens
            aggregato.cache_write_tokens += misura.cache_write_tokens
            aggregato.full_price_tokens += misura.full_price_tokens
            aggregato.cost_usd += misura.cost_usd
            aggregato.aux_cost_usd += misura.aux_cost_usd
            aggregato.latency_ms += misura.latency_ms
        return aggregato


# --- varianti di configurazione ------------------------------------------

# Versione del corpus di scenari. Cambia quando si aggiunge o si toglie uno
# scenario: i numeri di due corpus diversi non sono confrontabili, e la
# sezione dei progressi deve poterlo sapere invece di sommare mele e pere.
CORPUS_VERSION = "v2"


def _spegni_tutto(settings: Settings) -> None:
    settings.cache_planner.enabled = False
    settings.exact_cache.enabled = False
    settings.semantic_cache.enabled = False
    settings.context.enabled = False
    settings.prompt.enabled = False
    settings.router.enabled = False
    settings.memory.enabled = False
    settings.budget.enabled = False
    # Il profilo predefinito accende le leve che cambiano il contenuto delle
    # risposte. La scala dell'ablazione deve pero' accenderle una alla volta e
    # in ordine, altrimenti il guadagno del cambio di modello finirebbe
    # attribuito allo stadio che capita di essere acceso per primo.
    settings.applica_profilo_prudente()
    settings.cache_planner.mode = "manuale"


def make_settings(apply: Callable[[Settings], None] | None = None) -> Settings:
    """Configurazione di misura: tutto spento, poi si accende cio' che serve."""
    settings = Settings()
    settings.storage.path = ":memory:"
    # Il contenuto dei messaggi non serve alla misura e rallenterebbe soltanto.
    settings.storage.store_message_content = False
    _spegni_tutto(settings)
    if apply is not None:
        apply(settings)
    return settings


def _abilita_cache_automatica(settings: Settings) -> None:
    """Cio' che si ottiene oggi senza il gateway: un campo in cima e basta.

    E' il riferimento onesto per il pianificatore. Finche' Anthropic non
    offriva il caching automatico, "senza gateway" voleva dire "nessuna cache"
    e il confronto era leale. Adesso quel gradino e' gratis per chiunque, e
    attribuirlo al gateway sarebbe misurare quanto costava non usare una
    funzione predefinita.
    """
    settings.cache_planner.enabled = True
    settings.cache_planner.mode = "automatico"


def _abilita_cache_planner(settings: Settings) -> None:
    settings.cache_planner.enabled = True
    settings.cache_planner.mode = "manuale"


def _abilita_contesto(settings: Settings) -> None:
    _abilita_cache_planner(settings)
    settings.context.enabled = True


def _abilita_cache_esatta(settings: Settings) -> None:
    _abilita_contesto(settings)
    settings.exact_cache.enabled = True


def _abilita_router(settings: Settings) -> None:
    _abilita_cache_esatta(settings)
    settings.router.enabled = True


def _abilita_prompt(settings: Settings) -> None:
    _abilita_router(settings)
    settings.prompt.enabled = True
    settings.prompt.normalize = True
    settings.prompt.strip_filler = True
    # Le sostituzioni lessicali restano fuori dall'ablazione: il loro effetto
    # dipende dal tokenizer vero, che qui non c'e'. Misurarle col simulatore
    # darebbe un numero che conferma solo l'assunzione con cui e' calcolato.
    settings.prompt.substitute = False


def _abilita_effort_minimo(settings: Settings) -> None:
    """Effort al minimo su tutto, senza giudicare la difficolta'."""
    _abilita_prompt(settings)
    settings.router.effort_policy = "sempre_basso"


def _abilita_modello_economico(settings: Settings) -> None:
    """L'ultima leva, e la piu' grossa: ogni sessione al modello meno caro.

    Sta in fondo alla scala perche' e' quella che scambia di piu'. Il guadagno
    e' misurato; cio' che si da' in cambio - la qualita' della risposta - non
    lo e', e il banco non ha modo di diventarlo.
    """
    _abilita_effort_minimo(settings)
    settings.router.model_downgrade = True
    settings.router.downgrade_policy = "sempre"


# Gradini cumulativi dell'ablazione: la differenza fra due gradini consecutivi
# e' il contributo dello stadio appena acceso.
# I nomi vengono da `varianti`, che non importa niente: qui si accoppiano alle
# funzioni che li realizzano. Cosi' non esistono due elenchi che possano
# divergere - e il quadro puo' leggere i nomi senza tirarsi dietro l'SDK.
_REALIZZA: dict[str, Callable[[Settings], None] | None] = {
    BASELINE_VARIANT: None,
    RIFERIMENTO_MODERNO: _abilita_cache_automatica,
    "+ pianificatore EcoTokens": _abilita_cache_planner,
    "+ potatura contesto": _abilita_contesto,
    "+ cache esatta": _abilita_cache_esatta,
    "+ effort adattivo": _abilita_router,
    ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA: _abilita_prompt,
    "+ effort sempre basso": _abilita_effort_minimo,
    ULTIMO_GRADINO: _abilita_modello_economico,
}

ABLATION_STEPS: list[tuple[str, Callable[[Settings], None] | None]] = [
    (nome, _REALIZZA[nome]) for nome in NOMI_ABLAZIONE
]


# --- esecuzione -----------------------------------------------------------


async def _run_scenario(
    scenario: Scenario,
    settings: Settings,
    variant: str,
    *,
    live: bool,
    raccolta: list[CacheEvent] | None = None,
) -> Measurement:
    """Esegue uno scenario su un gateway appena creato e ne misura l'esito.

    Con ``raccolta`` si porta via anche la sequenza dei contatori di cache di
    ogni richiesta, che serve a ricostruire quali scritture sono state poi
    rilette (vedi ``cache_audit``). E' una lista in ordine cronologico: i
    conti di quel modulo dipendono dall'ordine.
    """
    from .server import Gateway  # importazione locale: evita un ciclo di import

    gateway = Gateway(settings)
    if not live:
        stub_app, _ = create_stub()
        gateway.client = anthropic.AsyncAnthropic(
            api_key="bench",
            base_url="http://simulatore",
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.ASGITransport(app=stub_app)
            ),
        )
    await gateway.startup()

    misura = Measurement(scenario=scenario.name, variant=variant)
    try:
        for payload in scenario.requests:
            request = ChatCompletionRequest.model_validate(payload)
            inizio = time.monotonic()
            _, ctx = await gateway.complete(request)
            misura.latency_ms += (time.monotonic() - inizio) * 1000

            misura.requests += 1
            if ctx.source == SOURCE_API:
                misura.upstream_calls += 1
            misura.prompt_tokens += ctx.usage.total_prompt_tokens
            misura.output_tokens += ctx.usage.output_tokens
            misura.cache_read_tokens += ctx.usage.cache_read_tokens
            misura.cache_write_tokens += ctx.usage.cache_creation_tokens
            misura.full_price_tokens += ctx.usage.input_tokens
            misura.cost_usd += ctx.total_cost_usd
            misura.aux_cost_usd += ctx.aux_cost_usd

            if raccolta is not None and ctx.source == SOURCE_API:
                # Solo le richieste arrivate davvero all'API: un hit della
                # cache locale non tocca la cache di Anthropic.
                raccolta.append(
                    CacheEvent(
                        session_id=ctx.session_id or f"{scenario.name}:{variant}",
                        read_tokens=ctx.usage.cache_read_tokens,
                        write_tokens=ctx.usage.cache_creation_tokens,
                        model=ctx.model,
                        cache_ttl=ctx.cache_ttl,
                    )
                )
    finally:
        await gateway.shutdown()

    return misura


async def run_benchmark(
    *,
    scenarios: list[Scenario] | None = None,
    label: str = "misura",
    live: bool = False,
    project_root: Path | None = None,
    variant_name: str = FULL_VARIANT,
    variant_apply: Callable[[Settings], None] | None = None,
) -> BenchRun:
    """Confronta "senza gateway" e "con gateway" su ogni scenario."""
    scenarios = scenarios or all_scenarios(project_root)
    if variant_apply is None:
        # La configurazione completa, cioe' il profilo predefinito.
        variant_apply = _abilita_modello_economico

    run = BenchRun(
        id=uuid.uuid4().hex[:12],
        label=label,
        mode="live" if live else "simulato",
        created_at=time.time(),
        fingerprint=corpus_fingerprint(scenarios),
    )

    for scenario in scenarios:
        prima = await _run_scenario(
            scenario, make_settings(None), BASELINE_VARIANT, live=live
        )
        dopo = await _run_scenario(
            scenario, make_settings(variant_apply), variant_name, live=live
        )
        run.measurements.extend([prima, dopo])
        run.comparisons.append(Comparison(scenario=scenario.name, before=prima, after=dopo))

    return run


async def run_ablation(
    *,
    scenarios: list[Scenario] | None = None,
    label: str = "ablazione",
    live: bool = False,
    project_root: Path | None = None,
) -> BenchRun:
    """Accende gli stadi uno alla volta e attribuisce il risparmio a ciascuno."""
    scenarios = scenarios or all_scenarios(project_root)
    run = BenchRun(
        id=uuid.uuid4().hex[:12],
        label=label,
        mode="live" if live else "simulato",
        created_at=time.time(),
        fingerprint=corpus_fingerprint(scenarios),
    )

    for nome, applica in ABLATION_STEPS:
        for scenario in scenarios:
            misura = await _run_scenario(
                scenario, make_settings(applica), nome, live=live
            )
            run.measurements.append(misura)

    riferimento = run.totals(BASELINE_VARIANT)
    for nome, _ in ABLATION_STEPS[1:]:
        run.comparisons.append(
            Comparison(scenario="tutti", before=riferimento, after=run.totals(nome))
        )
    return run


def stage_contributions(run: BenchRun) -> list[dict[str, Any]]:
    """Contributo incrementale di ogni stadio, in dollari e in percentuale.

    E' la differenza fra un gradino dell'ablazione e il precedente: cio' che
    quello stadio ha aggiunto, misurato e non dichiarato.
    """
    contributi: list[dict[str, Any]] = []
    riferimento = run.totals(BASELINE_VARIANT).cost_usd
    precedente = riferimento

    for nome, _ in ABLATION_STEPS[1:]:
        corrente = run.totals(nome).cost_usd
        delta = precedente - corrente
        contributi.append(
            {
                "stage": nome.removeprefix("+ ").strip(),
                "saved_usd": delta,
                "saved_ratio": delta / riferimento if riferimento else 0.0,
                "cumulative_usd": riferimento - corrente,
                "cumulative_ratio": (riferimento - corrente) / riferimento if riferimento else 0.0,
            }
        )
        precedente = corrente
    return contributi


def guadagno_sul_caching_automatico(run: BenchRun) -> dict[str, Any]:
    """Quanto aggiunge il gateway a chi usa **gia'** il caching automatico.

    E' la domanda che si fa chi sta decidendo se installarlo, ed e' diversa da
    quella a cui risponde il totale dell'ablazione. Il totale confronta con
    "nessuna cache": un riferimento che descriveva il mondo di prima, quando
    ottenere il prompt caching richiedeva lavoro. Oggi basta un campo, quindi
    quel confronto attribuisce al gateway un merito che non e' suo e prepara
    una delusione a chi legge la percentuale e installa.

    Le due cifre restituite vanno lette insieme e mai una sola:

    * ``senza_cambiare_la_risposta`` - il guadagno che si ottiene lasciando
      intatto il contenuto. E' l'unico numero che si possa promettere;
    * ``cambiando_la_risposta`` - dove si arriva accendendo declassamento di
      modello ed effort minimo. Il banco misura quanto e' **lunga** una
      risposta, non se e' **giusta**: quella differenza e' interamente
      misurata e il suo costo interamente no.

    Per scenario, perche' l'aggregato nasconde una forbice che e' l'informazione
    piu' utile del progetto: dove molte richieste condividono un prefisso il
    guadagno e' grande, su una conversazione sola che cresce e' piccolo, perche'
    li' il caching automatico fa gia' quasi tutto da solo.
    """
    riferimento = run.totals(RIFERIMENTO_MODERNO).cost_usd
    prudente = run.totals(ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA).cost_usd
    completo = run.totals(ABLATION_STEPS[-1][0]).cost_usd

    def quota(prima: float, dopo: float) -> float:
        return (prima - dopo) / prima if prima else 0.0

    per_scenario = []
    for scenario in sorted({m.scenario for m in run.measurements}):
        base = run.totals(RIFERIMENTO_MODERNO, scenario=scenario).cost_usd
        dopo = run.totals(ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA, scenario=scenario).cost_usd
        per_scenario.append(
            {
                "scenario": scenario,
                "reference_usd": base,
                "cost_usd": dopo,
                "saved_ratio": quota(base, dopo),
            }
        )
    per_scenario.sort(key=lambda voce: -voce["saved_ratio"])

    return {
        "reference_usd": riferimento,
        "senza_cambiare_la_risposta": {
            "cost_usd": prudente,
            "saved_ratio": quota(riferimento, prudente),
        },
        "cambiando_la_risposta": {
            "cost_usd": completo,
            "saved_ratio": quota(riferimento, completo),
        },
        "by_scenario": per_scenario,
    }


# --- persistenza ----------------------------------------------------------


async def save_run(store: Store, run: BenchRun, *, corpus: str = "", notes: str = "") -> None:
    """Registra la misura, cosi' il miglioramento resta visibile nel tempo."""
    await store.db.execute(
        """INSERT INTO bench_runs (id, created_at, label, mode, corpus, fingerprint, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run.id, run.created_at, run.label, run.mode, corpus, run.fingerprint, notes),
    )
    await store.db.executemany(
        """INSERT INTO bench_results
           (run_id, scenario, variant, requests, upstream_calls, prompt_tokens,
            output_tokens, cache_read_tokens, cache_write_tokens, full_price_tokens,
            cost_usd, latency_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                run.id,
                misura.scenario,
                misura.variant,
                misura.requests,
                misura.upstream_calls,
                misura.prompt_tokens,
                misura.output_tokens,
                misura.cache_read_tokens,
                misura.cache_write_tokens,
                misura.full_price_tokens,
                misura.cost_usd,
                misura.latency_ms,
            )
            for misura in run.measurements
        ],
    )


async def load_runs(store: Store, limit: int = 20) -> list[dict[str, Any]]:
    """Storico delle misure. La lettura vive nel deposito; qui resta il nome."""
    return await store.load_runs(limit)


def run_to_dict(run: BenchRun) -> dict[str, Any]:
    """Rappresentazione serializzabile, usata dalla dashboard."""
    return {
        "id": run.id,
        "label": run.label,
        "mode": run.mode,
        "created_at": run.created_at,
        "measurements": [asdict(misura) for misura in run.measurements],
        "comparisons": [
            {
                "scenario": confronto.scenario,
                "before": asdict(confronto.before),
                "after": asdict(confronto.after),
                "saved_usd": confronto.saved_usd,
                "saved_ratio": confronto.saved_ratio,
                "tokens_avoided": confronto.tokens_avoided,
            }
            for confronto in run.comparisons
        ],
    }


def open_results_store(path: str) -> tuple[Database, Store]:
    database = Database(path)
    database.connect()
    return database, Store(database)


# --- ricerca della configurazione migliore --------------------------------

# Candidati provati da ``ecotokens optimize``. Sono ipotesi, non certezze: il
# senso del comando e' proprio scegliere in base alla misura invece che
# all'intuizione, perche' su questo terreno l'intuizione sbaglia spesso.
SWEEP_CANDIDATES: list[tuple[str, Callable[[Settings], None]]] = [
    ("predefinita", _abilita_router),
]


def _candidate(nome: str, **modifiche: Any) -> tuple[str, Callable[[Settings], None]]:
    def applica(settings: Settings) -> None:
        _abilita_router(settings)
        for percorso, valore in modifiche.items():
            sezione, _, campo = percorso.partition("__")
            setattr(getattr(settings, sezione), campo, valore)

    return nome, applica


SWEEP_CANDIDATES.extend(
    [
        _candidate("breakpoint anche al primo turno", cache_planner__skip_first_turn=False),
        _candidate("TTL lungo sempre", cache_planner__long_ttl_min_turns=1,
                   cache_planner__long_ttl_min_gap_seconds=0),
        _candidate("marker intermedi piu' fitti", cache_planner__intermediate_every_blocks=8),
        _candidate("solo due breakpoint", cache_planner__max_breakpoints=2),
        _candidate("potatura aggressiva", context__trigger_ratio=0.02),
        _candidate("effort basso piu' spesso", router__simple_max_question_tokens=400),
    ]
)


@dataclass
class SweepEntry:
    name: str
    cost_usd: float
    saved_ratio: float
    cache_ratio: float


async def run_sweep(
    *,
    scenarios: list[Scenario] | None = None,
    live: bool = False,
    project_root: Path | None = None,
) -> tuple[list[SweepEntry], BenchRun]:
    """Prova piu' configurazioni e le ordina per costo misurato.

    E' il meccanismo di auto-miglioramento: la configurazione consigliata non
    e' quella che sembra sensata, e' quella che ha speso meno sui carichi veri.
    """
    scenarios = scenarios or all_scenarios(project_root)
    run = BenchRun(
        id=uuid.uuid4().hex[:12],
        label="ricerca configurazione",
        mode="live" if live else "simulato",
        created_at=time.time(),
        fingerprint=corpus_fingerprint(scenarios),
    )

    for scenario in scenarios:
        run.measurements.append(
            await _run_scenario(scenario, make_settings(None), BASELINE_VARIANT, live=live)
        )
    riferimento = run.totals(BASELINE_VARIANT).cost_usd

    esiti: list[SweepEntry] = []
    for nome, applica in SWEEP_CANDIDATES:
        for scenario in scenarios:
            run.measurements.append(
                await _run_scenario(scenario, make_settings(applica), nome, live=live)
            )
        totale = run.totals(nome)
        esiti.append(
            SweepEntry(
                name=nome,
                cost_usd=totale.cost_usd,
                saved_ratio=(riferimento - totale.cost_usd) / riferimento if riferimento else 0.0,
                cache_ratio=totale.cache_ratio,
            )
        )

    esiti.sort(key=lambda voce: voce.cost_usd)
    return esiti, run


# --- potatura del contesto ------------------------------------------------


@dataclass
class PruningVariant:
    """Una strategia di potatura del contesto messa alla prova."""

    scenario: str
    name: str
    description: str
    cost_usd: float
    cache_ratio: float
    delta_ratio: float


async def measure_pruning(
    *, live: bool = False, project_root: Path | None = None
) -> list[PruningVariant]:
    """Confronta le strategie di potatura sui carichi con molti tool result.

    Per molto tempo questa misura ha detto che potare e mettere in cache sono
    incompatibili, e la conclusione sembrava definitiva. Non lo era: mancava un
    parametro. L'edit ``clear_tool_uses_20250919`` accetta ``keep``, che il
    gateway lasciava al valore predefinito del server - e con ``keep`` fisso il
    confine di potatura scorre di un risultato a ogni turno, quindi l'insieme
    dei blocchi svuotati e' diverso a ogni richiesta e il prefisso e' nuovo per
    costruzione.

    Le tre varianti isolano esattamente quel punto.
    """
    from .workloads import scenario_agente, scenario_costruzione

    root = project_root or Path.cwd()
    scenari = [scenario_agente(), scenario_costruzione(root)]

    def _potatura(settings: Settings, **modifiche: Any) -> None:
        _abilita_cache_planner(settings)
        settings.context.enabled = True
        # Soglie abbassate apposta: qui interessa il confronto fra strategie,
        # non se la potatura scatti con i valori predefiniti.
        settings.context.trigger_ratio = 0.02
        settings.context.local_compaction = False
        for chiave, valore in modifiche.items():
            setattr(settings.context, chiave, valore)

    varianti: list[tuple[str, str, Callable[[Settings], None]]] = [
        (
            "nessuna potatura",
            "il contesto resta integrale",
            _abilita_cache_planner,
        ),
        (
            "confine mobile",
            "keep fisso: il confine scorre a ogni turno",
            lambda s: _potatura(s, prune_step_turns=0),
        ),
        (
            "confine a scatti",
            "gli stessi blocchi restano svuotati per piu' turni",
            lambda s: _potatura(s),
        ),
    ]

    esiti: list[PruningVariant] = []
    for scenario in scenari:
        riferimento = None
        for nome, descrizione, applica in varianti:
            misura = await _run_scenario(
                scenario, make_settings(applica), nome, live=live
            )
            if riferimento is None:
                riferimento = misura.cost_usd
            esiti.append(
                PruningVariant(
                    scenario=scenario.name,
                    name=nome,
                    description=descrizione,
                    cost_usd=misura.cost_usd,
                    cache_ratio=misura.cache_ratio,
                    delta_ratio=(riferimento - misura.cost_usd) / riferimento
                    if riferimento
                    else 0.0,
                )
            )
    return esiti


# --- compattazione del contesto -------------------------------------------


@dataclass
class CompactionVariant:
    """Una strategia di compattazione messa alla prova."""

    name: str
    description: str
    cost_usd: float
    aux_cost_usd: float
    cache_ratio: float
    full_price_tokens: int
    prompt_tokens: int
    summaries: int


async def measure_compaction(*, live: bool = False) -> list[CompactionVariant]:
    """Confronta le strategie di taglio su una conversazione lunga.

    La domanda a cui risponde e' se comprimere la cronologia convenga davvero,
    una volta contato il prezzo della compressione: la chiamata al riassuntore,
    e soprattutto il prompt caching che si perde quando il riassunto cambia.

    Le varianti sono cumulative e isolano una tecnica ciascuna:

    * *solo cache* - non si comprime affatto, e' il metro di paragone;
    * *taglio a inseguimento* - il punto di taglio segue la coda della
      conversazione, quindi si sposta a ogni turno (era il comportamento
      originale del gateway);
    * *taglio a scatti* - il punto di taglio avanza a blocchi, cosi' lo stesso
      riassunto vale per piu' turni e il prefisso resta fermo;
    * *scatti + riassunto incrementale* - quando il taglio avanza si riparte
      dal riassunto precedente invece di rileggere tutta la cronologia.
    """
    from .workloads import scenario_conversazione_lunga

    scenario = scenario_conversazione_lunga()

    def _compattazione(settings: Settings, **modifiche: Any) -> None:
        _abilita_cache_planner(settings)
        settings.context.enabled = True
        # Soglie abbassate apposta: con quelle predefinite nemmeno una
        # conversazione di quaranta turni si avvicina alla finestra di Opus 5,
        # e la compattazione non scatterebbe mai.
        settings.context.trigger_ratio = 0.01
        settings.context.hard_ratio = 0.02
        settings.context.local_compaction = True
        settings.context.min_gain_tokens = 0
        for chiave, valore in modifiche.items():
            setattr(settings.context, chiave, valore)

    varianti: list[tuple[str, str, Callable[[Settings], None]]] = [
        (
            "solo cache",
            "nessuna compattazione",
            lambda s: _abilita_cache_planner(s),
        ),
        (
            "taglio a inseguimento",
            "il taglio segue la coda: riassunto nuovo a ogni turno",
            lambda s: _compattazione(s, recompute_every_messages=1, incremental_summary=False),
        ),
        (
            "taglio a scatti",
            "il taglio avanza a blocchi: il riassunto si riusa",
            lambda s: _compattazione(s, incremental_summary=False),
        ),
        (
            "scatti + incrementale",
            "il riassunto nuovo parte da quello vecchio",
            lambda s: _compattazione(s, incremental_summary=True),
        ),
    ]

    esiti: list[CompactionVariant] = []
    for nome, descrizione, applica in varianti:
        conteggio = {"n": 0}
        misura = await _run_scenario_contando_riassunti(
            scenario, make_settings(applica), nome, live=live, conteggio=conteggio
        )
        esiti.append(
            CompactionVariant(
                name=nome,
                description=descrizione,
                cost_usd=misura.cost_usd,
                aux_cost_usd=misura.aux_cost_usd,
                cache_ratio=misura.cache_ratio,
                full_price_tokens=misura.full_price_tokens,
                prompt_tokens=misura.prompt_tokens,
                summaries=conteggio["n"],
            )
        )
    return esiti


async def _run_scenario_contando_riassunti(
    scenario: Scenario,
    settings: Settings,
    variant: str,
    *,
    live: bool,
    conteggio: dict[str, int],
) -> Measurement:
    """Come ``_run_scenario``, ma conta quante volte si chiama il riassuntore.

    Il numero di riassunti e' la misura diretta della stabilita' del prefisso:
    un riassunto per turno significa un prefisso nuovo per turno.
    """
    from .pipeline import context as context_module

    originale = context_module.ContextStage._call_summarizer

    async def contato(self, ctx, istruzioni, corpo):
        conteggio["n"] += 1
        return await originale(self, ctx, istruzioni, corpo)

    context_module.ContextStage._call_summarizer = contato
    try:
        return await _run_scenario(scenario, settings, variant, live=live)
    finally:
        context_module.ContextStage._call_summarizer = originale


# --- riscrittura del prompt -----------------------------------------------


@dataclass
class PromptVariant:
    """Un livello di riscrittura del prompt messo alla prova."""

    name: str
    description: str
    validated: bool
    cost_usd: float
    cache_ratio: float
    prompt_tokens: int
    full_price_tokens: int
    tokens_removed: int
    tokens_removed_uncached: int


async def measure_prompt_optimization(*, live: bool = False) -> list[PromptVariant]:
    """Misura cosa vale accorciare il prompt, e soprattutto *dove*.

    Un avvertimento sul metodo, perche' senza di esso questi numeri si leggono
    male. Il simulatore conta i token dalla lunghezza del testo. Va benissimo
    per rispondere alla domanda strutturale - dove finiscono i token tolti, e
    la riscrittura rompe la cache? - perche' li' quel che conta e' a quale
    tariffa un token viene fatturato, non quanti caratteri servono a formarlo.
    Non va bene per la domanda lessicale: se "usare" costi davvero meno token
    di "utilizzare" lo sa solo `messages.count_tokens`. Sotto questa metrica
    qualunque accorciamento sembra un guadagno, per costruzione.

    Percio' le varianti riportano `validated`: vero quando il risultato non
    dipende da quell'assunzione, falso quando ci si appoggia.
    """
    from .workloads import scenario_prompt_verboso

    scenario = scenario_prompt_verboso()

    def _prompt(settings: Settings, **modifiche: Any) -> None:
        _abilita_cache_planner(settings)
        settings.prompt.enabled = True
        settings.prompt.normalize = False
        settings.prompt.strip_filler = False
        settings.prompt.substitute = False
        for chiave, valore in modifiche.items():
            setattr(settings.prompt, chiave, valore)

    varianti: list[tuple[str, str, bool, Callable[[Settings], None]]] = [
        (
            "prompt originale",
            "nessuna riscrittura",
            True,
            lambda s: (_abilita_cache_planner(s), setattr(s.prompt, "enabled", False))[0],
        ),
        (
            "normalizzazione",
            "spazi, righe vuote, caratteri invisibili: nessuna parola cambiata",
            True,
            lambda s: _prompt(s, normalize=True),
        ),
        (
            "+ formule di riempimento",
            "via le perifrasi che introducono un'istruzione senza aggiungerle nulla",
            True,
            lambda s: _prompt(s, normalize=True, strip_filler=True),
        ),
        (
            "+ sostituzioni lessicali",
            "sinonimi piu' corti - risparmio in token NON verificato",
            False,
            lambda s: _prompt(
                s, normalize=True, strip_filler=True, substitute=True, only_verified=False
            ),
        ),
    ]

    esiti: list[PromptVariant] = []
    for nome, descrizione, validata, applica in varianti:
        conteggio = {"tolti": 0, "coda": 0}
        misura = await _run_scenario_contando_riscritture(
            scenario, make_settings(applica), nome, live=live, conteggio=conteggio
        )
        esiti.append(
            PromptVariant(
                name=nome,
                description=descrizione,
                validated=validata,
                cost_usd=misura.cost_usd,
                cache_ratio=misura.cache_ratio,
                prompt_tokens=misura.prompt_tokens,
                full_price_tokens=misura.full_price_tokens,
                tokens_removed=conteggio["tolti"],
                tokens_removed_uncached=conteggio["coda"],
            )
        )
    return esiti


async def _run_scenario_contando_riscritture(
    scenario: Scenario,
    settings: Settings,
    variant: str,
    *,
    live: bool,
    conteggio: dict[str, int],
) -> Measurement:
    """Come ``_run_scenario``, ma somma i token tolti dalla riscrittura."""
    from .server import Gateway

    gateway = Gateway(settings)
    if not live:
        stub_app, _ = create_stub()
        gateway.client = anthropic.AsyncAnthropic(
            api_key="bench",
            base_url="http://simulatore",
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.ASGITransport(app=stub_app)
            ),
        )
    await gateway.startup()

    misura = Measurement(scenario=scenario.name, variant=variant)
    try:
        for payload in scenario.requests:
            request = ChatCompletionRequest.model_validate(payload)
            inizio = time.monotonic()
            _, ctx = await gateway.complete(request)
            misura.latency_ms += (time.monotonic() - inizio) * 1000

            misura.requests += 1
            if ctx.source == SOURCE_API:
                misura.upstream_calls += 1
            misura.prompt_tokens += ctx.usage.total_prompt_tokens
            misura.output_tokens += ctx.usage.output_tokens
            misura.cache_read_tokens += ctx.usage.cache_read_tokens
            misura.cache_write_tokens += ctx.usage.cache_creation_tokens
            misura.full_price_tokens += ctx.usage.input_tokens
            misura.cost_usd += ctx.total_cost_usd
            misura.aux_cost_usd += ctx.aux_cost_usd
            conteggio["tolti"] += ctx.prompt_tokens_removed
            conteggio["coda"] += ctx.prompt_tokens_removed_uncached
    finally:
        await gateway.shutdown()

    return misura


# --- progressi fra una versione e l'altra ---------------------------------


def stage_contributions_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ricostruisce i contributi degli stadi da una misura gia' registrata.

    Serve al confronto fra versioni: i contributi non vengono salvati come
    tali, ma sono ricavabili dai costi per variante, che invece lo sono. Cosi'
    una misura vecchia resta interrogabile anche se nel frattempo il codice che
    la calcolava e' cambiato.
    """
    per_variante: dict[str, float] = {}
    for riga in results:
        per_variante[riga["variant"]] = per_variante.get(riga["variant"], 0.0) + riga["cost_usd"]

    riferimento = per_variante.get(BASELINE_VARIANT)
    if not riferimento:
        return []

    contributi: list[dict[str, Any]] = []
    precedente = riferimento
    for nome, _ in ABLATION_STEPS[1:]:
        if nome not in per_variante:
            # Gradino assente: la misura e' di una versione che non aveva
            # ancora questo stadio. Si interrompe invece di inventare uno zero,
            # perche' i gradini successivi sono cumulativi e sarebbero falsati.
            break
        corrente = per_variante[nome]
        contributi.append(
            {
                "stage": nome.removeprefix("+ ").strip(),
                "saved_usd": precedente - corrente,
                "saved_ratio": (precedente - corrente) / riferimento,
                "cumulative_ratio": (riferimento - corrente) / riferimento,
            }
        )
        precedente = corrente
    return contributi


async def stage_progress(
    store: Store, corrente: list[dict[str, Any]], *, corpus: str | None = None
) -> dict[str, Any]:
    """Confronta i contributi di adesso con quelli della misura precedente.

    Risponde alla domanda che un registro di ottimizzazioni deve saper
    rispondere: *questa ottimizzazione e' migliorata rispetto a prima?* Senza
    di essa il progetto puo' solo dire quanto risparmia oggi, non se ieri
    risparmiava di piu'.

    Il confronto avviene solo fra misure dello stesso corpus di scenari.
    Aggiungere uno scenario cambia il denominatore di tutte le percentuali:
    accostare due corpus diversi produrrebbe progressi immaginari.
    """
    etichetta = corpus or f"ablazione {CORPUS_VERSION}"
    runs = await load_runs(store, limit=40)
    ablazioni = [run for run in runs if run.get("corpus") == etichetta]
    if len(ablazioni) < 2:
        return {
            "available": False,
            "corpus": etichetta,
            "runs_found": len(ablazioni),
            "comparable": False,
            "fingerprint": "",
            "previous_fingerprint": "",
            "stages": [],
        }

    # `load_runs` restituisce dalla piu' recente: la prima e' quella appena
    # registrata, la seconda e' il termine di paragone.
    #
    # Se le due hanno impronte diverse il corpus e' cambiato *contenuto* fra
    # l'una e l'altra, anche se l'elenco degli scenari e' lo stesso: lo
    # scenario `costruzione` legge i sorgenti veri del progetto, quindi ogni
    # commit che allunga il codice sposta il riferimento. Il confronto viene
    # comunque mostrato - nasconderlo sarebbe peggio - ma marcato, perche' una
    # parte del delta e' crescita del metro e non merito del gateway.
    impronta_ora = ablazioni[0].get("fingerprint") or ""
    impronta_prima = ablazioni[1].get("fingerprint") or ""
    # Le misure registrate prima che l'impronta esistesse hanno la stringa
    # vuota: di quelle non si sa, e "non si sa" non e' "sono uguali".
    confrontabile = bool(impronta_ora) and impronta_ora == impronta_prima

    precedente = {
        voce["stage"]: voce
        for voce in stage_contributions_from_results(ablazioni[1]["results"])
    }

    righe: list[dict[str, Any]] = []
    for voce in corrente:
        prima = precedente.get(voce["stage"])
        if prima is None:
            righe.append(
                {
                    "stage": voce["stage"],
                    "now": voce["saved_ratio"],
                    "before": None,
                    "delta": None,
                    "status": "nuovo",
                }
            )
            continue
        delta = voce["saved_ratio"] - prima["saved_ratio"]
        if delta > 0.002:
            stato = "migliorato"
        elif delta < -0.002:
            stato = "peggiorato"
        else:
            stato = "invariato"
        righe.append(
            {
                "stage": voce["stage"],
                "now": voce["saved_ratio"],
                "before": prima["saved_ratio"],
                "delta": delta,
                "status": stato,
            }
        )

    return {
        "available": True,
        "corpus": etichetta,
        "runs_found": len(ablazioni),
        "previous_at": ablazioni[1]["created_at"],
        "previous_label": ablazioni[1].get("label") or "",
        "comparable": confrontabile,
        "fingerprint": impronta_ora,
        "previous_fingerprint": impronta_prima,
        "stages": righe,
    }


# --- chiave della cache esatta --------------------------------------------


@dataclass
class CacheKeyVariant:
    """Effetto di come si calcola la chiave della cache esatta."""

    scenario: str
    key_kind: str
    cost_usd: float
    requests: int
    upstream_calls: int

    @property
    def hits(self) -> int:
        return self.requests - self.upstream_calls


async def measure_cache_key(*, live: bool = False) -> list[CacheKeyVariant]:
    """Misura quanto vale normalizzare il testo prima di calcolare la chiave.

    E' l'ottimizzazione con la resa piu' alta di tutto il gateway, e la ragione
    e' aritmetica: ogni altra leva sconta il prezzo di un token, un hit di
    cache lo azzera. Il prompt caching serve un token a 0,1x; la cache esatta
    non lo serve affatto.

    Il confronto usa due carichi apposta. Uno ha domande ripetute identiche -
    li' normalizzare non puo' cambiare nulla, ed e' la verifica che non ci sia
    una regressione. L'altro ha le stesse domande scritte ogni volta con
    spaziatura diversa, che e' il caso realistico: un utente che ritocca, un
    template incoerente, un copia e incolla con virgolette tipografiche.
    """
    from .workloads import scenario_ripetitivo, scenario_ripetitivo_sciatto

    scenari = [scenario_ripetitivo_sciatto(), scenario_ripetitivo()]

    def _cache(settings: Settings, normalizza: bool) -> None:
        _abilita_cache_planner(settings)
        settings.exact_cache.enabled = True
        settings.exact_cache.normalize_key = normalizza

    esiti: list[CacheKeyVariant] = []
    for scenario in scenari:
        for etichetta, normalizza in (("byte grezzi", False), ("testo normalizzato", True)):
            misura = await _run_scenario(
                scenario,
                make_settings(lambda s, n=normalizza: _cache(s, n)),
                etichetta,
                live=live,
            )
            esiti.append(
                CacheKeyVariant(
                    scenario=scenario.name,
                    key_kind=etichetta,
                    cost_usd=misura.cost_usd,
                    requests=misura.requests,
                    upstream_calls=misura.upstream_calls,
                )
            )
    return esiti


@dataclass
class CacheWriteVariant:
    """Costo e spreco del pianificatore a parita' di tutto il resto."""

    etichetta: str
    breakpoints: int
    cost_usd: float
    audit: CacheWriteAudit

    def to_dict(self) -> dict[str, Any]:
        return {
            "etichetta": self.etichetta,
            "breakpoints": self.breakpoints,
            "cost_usd": self.cost_usd,
            **self.audit.to_dict(),
        }


async def measure_cache_writes(*, live: bool = False) -> list[CacheWriteVariant]:
    """Quante delle scritture in cache vengono davvero rilette.

    Nasce da una constatazione dell'ablazione: il prompt caching vale il 67%
    del risparmio e gli altri quattro stadi insieme il 7%. Continuare a
    limare gli stadi piccoli significa contendersi un settimo di quello che
    vale il primo; l'unica domanda che sposta qualcosa e' se dentro quel 67%
    ci sia dello sprecato.

    Il confronto abbassa il tetto dei breakpoint da quattro a uno, con tutto
    il resto identico, e per ognuno riporta **due** numeri: quanto e' costato
    e quanto ha buttato. Servono entrambi, e nell'ordine giusto: lo spreco da
    solo si minimizza spegnendo il pianificatore, che e' la configurazione
    peggiore di tutte. Un tetto piu' basso conviene solo se lo spreco scende
    *senza* che il costo salga.

    Il gradino "pianificatore spento" e' li' proprio a ricordarlo: zero
    scritture, zero sprecato, e il conto piu' salato del gruppo.
    """
    scenari = all_scenarios()

    def _con_tetto(settings: Settings, tetto: int) -> None:
        _abilita_router(settings)
        settings.cache_planner.max_breakpoints = tetto

    def _spento(settings: Settings) -> None:
        _abilita_router(settings)
        settings.cache_planner.enabled = False

    gradini: list[tuple[str, int, Callable[[Settings], None]]] = [("spento", 0, _spento)]
    for tetto in (1, 2, 3, 4):
        etichetta = str(tetto) + (" (attuale)" if tetto == 4 else "")
        gradini.append((etichetta, tetto, lambda s, t=tetto: _con_tetto(s, t)))

    esiti: list[CacheWriteVariant] = []
    for etichetta, tetto, applica in gradini:
        eventi: list[CacheEvent] = []
        costo = 0.0
        for scenario in scenari:
            misura = await _run_scenario(
                scenario, make_settings(applica), etichetta, live=live, raccolta=eventi
            )
            costo += misura.cost_usd
        esiti.append(
            CacheWriteVariant(
                etichetta=etichetta,
                breakpoints=tetto,
                cost_usd=costo,
                audit=audit_cache_writes(eventi),
            )
        )
    return esiti


def gateway_overhead() -> dict[str, Any]:
    """Testo che il gateway aggiunge di suo, prima e dopo la riscrittura.

    Non e' una misura di esecuzione: e' il conteggio delle stringhe stesse. Ha
    senso cosi' perche' quel testo e' fisso e nostro, e la domanda che pone -
    quanto costa a ogni occorrenza - non dipende dal carico.
    """
    from .wording import CATALOG, catalog_totals

    return {
        "totals": catalog_totals(),
        "items": [
            {
                "key": voce.key,
                "purpose": voce.purpose,
                "before": voce.legacy_tokens,
                "after": voce.tokens,
                "saved": voce.saved,
                "text": voce.text if len(voce.text) <= 80 else voce.text[:77] + "...",
            }
            for voce in CATALOG
        ],
    }


# --- streaming ------------------------------------------------------------


@dataclass
class EsitoStreaming:
    modalita: str
    requests: int = 0
    prompt_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


async def measure_streaming(scenario_name: str = "chat") -> list[EsitoStreaming]:
    """Lo stesso carico servito in un colpo solo e a pezzi.

    Il banco esegue tutto attraverso ``Gateway.complete``, che e' il percorso
    non-streaming: il percorso in streaming vive nella rotta HTTP e non e' mai
    stato misurato - zero richieste su cinquantuno del corpus. Non e' una svista
    da poco, perche' la maggior parte delle interfacce di chat trasmette, e
    quindi il risparmio pubblicato descriveva la meta' del traffico reale.

    Questa misura passa dall'app vera, l'unico modo di toccare quel percorso.
    Resta fuori dal corpus di proposito: aggiungere uno scenario cambierebbe il
    denominatore di tutte le percentuali storiche, e la domanda qui e' diversa
    da quella dell'ablazione - non "quanto vale uno stadio" ma "il risultato
    cambia se la risposta arriva a pezzi".
    """
    import json

    from fastapi.testclient import TestClient

    from .server import create_app

    esiti: list[EsitoStreaming] = []
    for modalita in ("in un colpo", "a pezzi"):
        settings = make_settings(_abilita_prompt)
        settings.storage.path = ":memory:"
        app = create_app(settings)
        gateway = app.state.gateway
        stub_app, _ = create_stub()
        gateway.client = anthropic.AsyncAnthropic(
            api_key="bench",
            base_url="http://simulatore",
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.ASGITransport(app=stub_app)
            ),
        )

        scenario = next(
            s for s in all_scenarios(Path.cwd()) if s.name == scenario_name
        )
        esito = EsitoStreaming(modalita=modalita)
        with TestClient(app) as client:
            for payload in scenario.requests:
                corpo = dict(payload)
                corpo["stream"] = modalita == "a pezzi"
                if corpo["stream"]:
                    # Senza, l'usage non arriva al client - ma il gateway lo
                    # registra comunque, ed e' quello che si sta misurando.
                    corpo["stream_options"] = {"include_usage": True}
                risposta = client.post("/v1/chat/completions", json=corpo)
                assert risposta.status_code == 200, risposta.text
                if corpo["stream"]:
                    assert "[DONE]" in risposta.text, "flusso incompleto"

            dati = await gateway.store.stats()
            esito.requests = int(dati.get("requests") or 0)
            esito.prompt_tokens = int(dati.get("total_prompt_tokens") or 0)
            esito.cache_read_tokens = int(dati.get("cache_read_tokens") or 0)
            esito.cost_usd = float(dati.get("cost_usd") or 0)
        esiti.append(esito)
    return esiti
