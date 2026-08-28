"""Dashboard: tutti i parametri misurati, con e senza gateway.

Raccoglie in un solo posto le tre fonti di verita' del progetto:

* il **banco di misura**, che esegue lo stesso carico con e senza gli stadi di
  ottimizzazione;
* l'**ablazione**, che accende uno stadio alla volta e attribuisce a ciascuno
  il suo contributo;
* il **traffico reale** gia' passato dal gateway, letto dal registro consumi.

Il risultato e' una pagina HTML autonoma: nessuna dipendenza esterna, nessuna
richiesta di rete, si apre da file o si serve da ``/dashboard``.
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from .bench import (
    BASELINE_VARIANT,
    CORPUS_VERSION,
    FULL_VARIANT,
    gateway_overhead,
    load_runs,
    measure_cache_key,
    measure_cache_writes,
    measure_compaction,
    measure_prompt_optimization,
    measure_pruning,
    open_results_store,
    run_ablation,
    run_benchmark,
    save_run,
    stage_contributions,
    stage_progress,
)
from .ceiling import (
    measure_ceiling,
    measure_repetition_curve,
    ripetizioni_per_obiettivo,
)
from .config import Settings
from .tuning_log import TUNING_LOG
from .workloads import all_scenarios

# --- raccolta dei dati ----------------------------------------------------


async def build_dashboard_data(
    settings: Settings, *, measure: bool = True, project_root: Path | None = None
) -> dict[str, Any]:
    """Raccoglie misure fresche e storico, pronti per il rendering."""
    root = project_root or Path.cwd()
    dati: dict[str, Any] = {
        "generated_at": time.time(),
        "mode": "simulato",
        "tuning": [
            {
                "area": voce.area,
                "title": voce.title,
                "finding": voce.finding,
                "effect": voce.effect,
            }
            for voce in TUNING_LOG
        ],
        "scenarios": [],
        "stages": [],
        "interactions": [],
        "compaction": [],
        "prompt": [],
        "cache_key": [],
        "progress": None,
        "history": [],
        "totals": None,
        "live": None,
        "calibration": [],
        "config": _config_snapshot(settings),
        # Non dipende dal carico ne' dal database: e' il conteggio delle
        # stringhe che il gateway inserisce, quindi si raccoglie sempre, anche
        # quando la dashboard viene generata senza rifare le misure.
        "overhead": gateway_overhead(),
    }

    database, store = open_results_store(settings.storage.path)
    try:
        if measure:
            descrizioni = {s.name: s.description for s in all_scenarios(root)}

            misura = await run_benchmark(label="confronto A/B", project_root=root)
            await save_run(store, misura, corpus=f"confronto {CORPUS_VERSION}")
            dati["mode"] = misura.mode
            dati["scenarios"] = [
                {
                    "name": confronto.scenario,
                    "description": descrizioni.get(confronto.scenario, ""),
                    "requests": confronto.before.requests,
                    "upstream_before": confronto.before.upstream_calls,
                    "upstream_after": confronto.after.upstream_calls,
                    "cost_before": confronto.before.cost_usd,
                    "cost_after": confronto.after.cost_usd,
                    "saved_ratio": confronto.saved_ratio,
                    "cache_ratio": confronto.after.cache_ratio,
                    "tokens_avoided": confronto.tokens_avoided,
                }
                for confronto in misura.comparisons
            ]
            prima = misura.totals(BASELINE_VARIANT)
            dopo = misura.totals(FULL_VARIANT)
            dati["totals"] = {
                "requests": prima.requests,
                "cost_before": prima.cost_usd,
                "cost_after": dopo.cost_usd,
                "saved_usd": prima.cost_usd - dopo.cost_usd,
                "saved_ratio": (prima.cost_usd - dopo.cost_usd) / prima.cost_usd
                if prima.cost_usd
                else 0.0,
                "prompt_tokens": prima.prompt_tokens,
                "flow_before": {
                    "full": prima.full_price_tokens,
                    "write": prima.cache_write_tokens,
                    "read": prima.cache_read_tokens,
                },
                "flow_after": {
                    "full": dopo.full_price_tokens,
                    "write": dopo.cache_write_tokens,
                    "read": dopo.cache_read_tokens,
                },
                "upstream_before": prima.upstream_calls,
                "upstream_after": dopo.upstream_calls,
                "output_before": prima.output_tokens,
                "output_after": dopo.output_tokens,
            }

            ablazione = await run_ablation(label="ablazione", project_root=root)
            await save_run(store, ablazione, corpus=f"ablazione {CORPUS_VERSION}")
            dati["stages"] = stage_contributions(ablazione)

            dati["interactions"] = [
                {
                    "scenario": voce.scenario,
                    "name": voce.name,
                    "description": voce.description,
                    "cost_usd": voce.cost_usd,
                    "cache_ratio": voce.cache_ratio,
                    "delta_ratio": voce.delta_ratio,
                }
                for voce in await measure_pruning(project_root=root)
            ]

            compattazione = await measure_compaction()
            riferimento = compattazione[0].cost_usd if compattazione else 0.0
            dati["compaction"] = [
                {
                    "name": voce.name,
                    "description": voce.description,
                    "cost_usd": voce.cost_usd,
                    "aux_cost_usd": voce.aux_cost_usd,
                    "cache_ratio": voce.cache_ratio,
                    "summaries": voce.summaries,
                    "delta_ratio": (riferimento - voce.cost_usd) / riferimento
                    if riferimento
                    else 0.0,
                }
                for voce in compattazione
            ]

            riscritture = await measure_prompt_optimization()
            origine = riscritture[0].cost_usd if riscritture else 0.0
            dati["prompt"] = [
                {
                    "name": voce.name,
                    "description": voce.description,
                    "validated": voce.validated,
                    "cost_usd": voce.cost_usd,
                    "cache_ratio": voce.cache_ratio,
                    "tokens_removed": voce.tokens_removed,
                    "tokens_removed_uncached": voce.tokens_removed_uncached,
                    "delta_ratio": (origine - voce.cost_usd) / origine if origine else 0.0,
                    # Quanto rende davvero togliere mille token dal prompt. Il
                    # confronto interessante e' con il prezzo pieno dell'input:
                    # la differenza e' lo sconto che la cache aveva gia' fatto.
                    "yield_per_1k": ((origine - voce.cost_usd) / voce.tokens_removed * 1000)
                    if voce.tokens_removed
                    else 0.0,
                }
                for voce in riscritture
            ]

            chiavi = await measure_cache_key()
            dati["cache_key"] = [
                {
                    "scenario": voce.scenario,
                    "key_kind": voce.key_kind,
                    "cost_usd": voce.cost_usd,
                    "requests": voce.requests,
                    "hits": voce.hits,
                    "upstream_calls": voce.upstream_calls,
                }
                for voce in chiavi
            ]

            scritture = await measure_cache_writes()
            dati["cache_writes"] = [voce.to_dict() for voce in scritture]

            tetto = await measure_ceiling()
            dati["ceiling_baseline"] = tetto.baseline_usd
            dati["ceiling_max"] = tetto.tetto_teorico()
            dati["ceiling"] = [
                {
                    "etichetta": passo.etichetta,
                    "descrizione": passo.descrizione,
                    "in_cambio": passo.in_cambio,
                    "sicura": passo.sicura,
                    "cost_usd": passo.cost_usd,
                    "saved_ratio": passo.saved_ratio(tetto.baseline_usd),
                }
                for passo in tetto.steps
            ]
            dati["ceiling_floor"] = (
                {
                    "output_usd": tetto.floor.output_usd,
                    "input_nuovo_usd": tetto.floor.input_nuovo_usd,
                    "riletture_usd": tetto.floor.riletture_usd,
                    "totale_usd": tetto.floor.totale_usd,
                }
                if tetto.floor
                else {}
            )

            curva = await measure_repetition_curve()
            dati["repetition"] = [
                {
                    "uniche": punto.uniche,
                    "ripetizioni": punto.ripetizioni,
                    "richieste": punto.richieste,
                    "baseline_usd": punto.baseline_usd,
                    "cost_usd": punto.cost_usd,
                    "saved_ratio": punto.saved_ratio,
                }
                for punto in curva
            ]
            dati["repetition_for_99"] = ripetizioni_per_obiettivo(curva, 0.99)

            dati["progress"] = await stage_progress(store, dati["stages"])

        dati["history"] = _summarise_history(await load_runs(store, limit=12))
        dati["live"] = await _live_traffic(store)
        dati["calibration"] = await store.estimate_calibration()
        dati["cache_writes_live"] = await store.cache_write_report()
    finally:
        database.close()

    return dati


def _config_snapshot(settings: Settings) -> list[dict[str, Any]]:
    """Stato degli stadi, come lo vedrebbe una richiesta in arrivo adesso."""
    aggressivo = settings.profilo == "aggressivo"
    adattivo = settings.router.effort_policy == "adattivo"
    return [
        {"name": f"profilo: {settings.profilo}", "enabled": True,
         "detail": "il modello e l'effort delle risposte cambiano"
                   if aggressivo else "nessuno stadio tocca il contenuto"},
        {"name": "prompt caching", "enabled": settings.cache_planner.enabled,
         "detail": f"max {settings.cache_planner.max_breakpoints} breakpoint"},
        {"name": "cache esatta", "enabled": settings.exact_cache.enabled,
         "detail": f"TTL {settings.exact_cache.ttl_seconds // 3600} h"},
        {"name": "cache semantica", "enabled": settings.semantic_cache.enabled,
         "detail": f"soglia {settings.semantic_cache.similarity_threshold}"},
        {"name": "potatura contesto", "enabled": settings.context.enabled,
         "detail": f"oltre il {settings.context.trigger_ratio * 100:.0f}% della finestra"},
        {"name": "riassunto cronologia", "enabled": settings.context.local_compaction,
         "detail": f"scatti da {settings.context.recompute_every_messages} messaggi, "
                   f"tetto {settings.context.summary_max_tokens} token"},
        # Il nome e il dettaglio seguono la politica: con `sempre_basso` di
        # adattivo non c'e' piu' niente, e chiamarlo cosi' nasconderebbe che
        # l'effort viene abbassato anche sulle domande difficili.
        {"name": "effort adattivo" if adattivo else "effort sempre basso",
         "enabled": settings.router.effort_downshift,
         "detail": f"domande sotto {settings.router.simple_max_question_tokens} token"
                   if adattivo else "su ogni richiesta, difficolta' ignorata"},
        {"name": "cambio di modello", "enabled": settings.router.model_downgrade,
         "detail": "una volta per sessione"},
        {"name": "memoria", "enabled": settings.memory.enabled,
         "detail": f"max {settings.memory.max_facts_injected} fatti"},
        {"name": "tetto di spesa", "enabled": settings.budget.enabled,
         "detail": f"${settings.budget.daily_usd:.2f} al giorno"},
    ]


def _summarise_history(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Riduce ogni misura registrata a un punto della serie storica."""
    storico: list[dict[str, Any]] = []
    for run in runs:
        # Solo i confronti A/B: ablazioni e ricerche di configurazione misurano
        # cose diverse, e metterli sulla stessa serie darebbe un andamento finto.
        if run.get("corpus") != "confronto":
            continue
        per_variante: dict[str, float] = {}
        for riga in run.get("results", []):
            per_variante[riga["variant"]] = per_variante.get(riga["variant"], 0.0) + riga["cost_usd"]

        riferimento = per_variante.get(BASELINE_VARIANT)
        if not riferimento:
            continue
        # La variante migliore fra quelle provate in quella misura.
        candidati = {k: v for k, v in per_variante.items() if k != BASELINE_VARIANT}
        if not candidati:
            continue
        migliore_nome = min(candidati, key=candidati.get)
        migliore = candidati[migliore_nome]
        storico.append(
            {
                "label": run["label"],
                "mode": run["mode"],
                "created_at": run["created_at"],
                "cost_before": riferimento,
                "cost_after": migliore,
                "best_variant": migliore_nome,
                "saved_ratio": (riferimento - migliore) / riferimento,
            }
        )
    storico.reverse()
    return storico


async def _live_traffic(store) -> dict[str, Any] | None:
    """Statistiche del traffico vero gia' passato dal gateway."""
    stats = await store.stats()
    if not stats.get("requests"):
        return None
    today, month = await store.current_spend()
    return {
        "requests": int(stats["requests"]),
        "prompt_tokens": int(stats.get("total_prompt_tokens") or 0),
        "output_tokens": int(stats.get("output_tokens") or 0),
        "cache_read_tokens": int(stats.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(stats.get("cache_creation_tokens") or 0),
        "cache_hit_ratio": stats.get("cache_hit_ratio", 0.0),
        "cost_usd": float(stats.get("cost_usd") or 0),
        "baseline_cost_usd": float(stats.get("baseline_cost_usd") or 0),
        "saved_usd": float(stats.get("saved_usd") or 0),
        "by_source": stats.get("by_source") or [],
        "today_usd": today,
        "month_usd": month,
    }


# --- rendering ------------------------------------------------------------


def _fmt_usd(value: float) -> str:
    return f"${value:,.4f}"


def _fmt_int(value: float) -> str:
    return f"{int(value):,}".replace(",", " ")


def _fmt_pct(value: float, segno: bool = False) -> str:
    return f"{value * 100:+.1f}%" if segno else f"{value * 100:.1f}%"


def _esc(value: Any) -> str:
    return html.escape(str(value))


def render_dashboard(data: dict[str, Any], *, standalone: bool = True) -> str:
    """Genera la pagina. ``standalone`` aggiunge l'involucro del documento."""
    corpo = _body(data)
    if not standalone:
        return f"<title>Bilancio Token EcoTokens</title>\n{_STYLE}\n{corpo}"
    return (
        "<!doctype html>\n<html lang=\"it\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Bilancio Token EcoTokens</title>\n"
        f"{_STYLE}\n</head>\n<body>\n{corpo}\n</body>\n</html>\n"
    )


def _body(data: dict[str, Any]) -> str:
    sezioni = [
        _header(data),
        _verdict(data),
        _flow(data),
        _scenarios(data),
        _stages(data),
        _interactions(data),
        _compaction(data),
        _prompt(data),
        _cache_key(data),
        _overhead(data),
        _cache_writes(data),
        _ceiling(data),
        _calibration(data),
        _progress(data),
        _tuning(data),
        _history(data),
        _live(data),
        _config(data),
        _footer(data),
    ]
    return '<main class="page">\n' + "\n".join(parte for parte in sezioni if parte) + "\n</main>"


def _header(data: dict[str, Any]) -> str:
    generato = time.strftime("%d/%m/%Y %H:%M", time.localtime(data["generated_at"]))
    modo = data.get("mode", "simulato")
    nota = (
        "misura simulata"
        if modo != "live"
        else "misura eseguita contro l'API reale"
    )
    return f"""<header class="masthead">
  <p class="eyebrow">EcoTokens &middot; banco di misura</p>
  <h1>Bilancio token</h1>
  <p class="lede">Lo stesso identico carico di richieste, eseguito due volte:
  una con gli stadi di ottimizzazione spenti, una con il gateway al lavoro.
  Cambia una cosa sola fra le due esecuzioni.</p>
  <p class="meta"><span class="chip chip-{'live' if modo == 'live' else 'sim'}">{_esc(nota)}</span>
  <span class="mono">generata il {generato}</span></p>
</header>"""


def _verdict(data: dict[str, Any]) -> str:
    totali = data.get("totals")
    if not totali:
        return ""
    quota = totali["saved_ratio"]
    stato = "good" if quota > 0 else "bad"
    return f"""<section class="verdict" aria-label="Sintesi">
  <div class="verdict-main state-{stato}">
    <p class="label">Risparmio complessivo</p>
    <p class="huge mono">{_fmt_pct(quota)}</p>
    <p class="sub">{_fmt_usd(totali['saved_usd'])} su {totali['requests']} richieste</p>
  </div>
  <div class="verdict-grid">
    {_stat('Costo senza gateway', _fmt_usd(totali['cost_before']), 'tutti gli stadi spenti')}
    {_stat('Costo con gateway', _fmt_usd(totali['cost_after']), 'configurazione predefinita')}
    {_stat('Token di prompt', _fmt_int(totali['prompt_tokens']), 'identici nei due casi')}
    {_stat('A prezzo pieno', _fmt_int(totali['flow_after']['full']),
           f"erano {_fmt_int(totali['flow_before']['full'])}")}
    {_stat('Chiamate all API', _fmt_int(totali['upstream_after']),
           f"erano {_fmt_int(totali['upstream_before'])}")}
    {_stat('Token generati', _fmt_int(totali['output_after']),
           f"erano {_fmt_int(totali['output_before'])}")}
  </div>
</section>"""


def _stat(label: str, value: str, detail: str) -> str:
    return f"""<div class="stat">
      <p class="label">{_esc(label)}</p>
      <p class="value mono">{_esc(value)}</p>
      <p class="detail">{_esc(detail)}</p>
    </div>"""


def _flow(data: dict[str, Any]) -> str:
    totali = data.get("totals")
    if not totali:
        return ""

    def barra(flusso: dict[str, int], titolo: str, sottotitolo: str) -> str:
        totale = max(1, flusso["full"] + flusso["write"] + flusso["read"])
        segmenti = [
            ("full", flusso["full"], "prezzo pieno", "1&times;"),
            ("write", flusso["write"], "scritti in cache", "1,25&times;"),
            ("read", flusso["read"], "letti da cache", "0,1&times;"),
        ]
        pezzi = []
        for chiave, valore, etichetta, moltiplicatore in segmenti:
            quota = valore / totale * 100
            if quota <= 0:
                continue
            pezzi.append(
                f'<div class="seg seg-{chiave}" style="width:{quota:.2f}%" '
                f'title="{etichetta}: {_fmt_int(valore)} token"></div>'
            )
        legenda = "".join(
            f'<li><span class="key key-{chiave}"></span>'
            f'<span class="k-label">{etichetta}</span>'
            f'<span class="k-mult mono">{moltiplicatore}</span>'
            f'<span class="k-val mono">{_fmt_int(valore)}</span></li>'
            for chiave, valore, etichetta, moltiplicatore in segmenti
        )
        return f"""<div class="flow-row">
      <div class="flow-head"><h3>{_esc(titolo)}</h3><p>{_esc(sottotitolo)}</p></div>
      <div class="bar">{''.join(pezzi)}</div>
      <ul class="legend">{legenda}</ul>
    </div>"""

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Dove finiscono i token di prompt</h2>
    <p>La quantita' di token non cambia fra i due casi: cambia il prezzo a cui
    vengono pagati. Una lettura dalla cache costa un decimo del prezzo pieno,
    una scrittura costa un quarto in piu' &mdash; ed e' per questo che scrivere
    in cache qualcosa che nessuno rileggera' fa perdere soldi.</p>
  </div>
  {barra(totali['flow_before'], 'Senza gateway', 'tutto a prezzo pieno')}
  {barra(totali['flow_after'], 'Con gateway', 'il prefisso viene riletto')}
</section>"""


def _scenarios(data: dict[str, Any]) -> str:
    scenari = data.get("scenarios") or []
    if not scenari:
        return ""
    massimo = max(s["cost_before"] for s in scenari) or 1

    righe = []
    for scenario in scenari:
        larghezza_prima = scenario["cost_before"] / massimo * 100
        larghezza_dopo = scenario["cost_after"] / massimo * 100
        stato = "good" if scenario["saved_ratio"] > 0 else "bad"
        righe.append(
            f"""<article class="scenario">
      <div class="scenario-head">
        <h3>{_esc(scenario['name'])}</h3>
        <p>{_esc(scenario['description'])}</p>
      </div>
      <div class="scenario-bars">
        <div class="pair">
          <span class="pair-label">senza</span>
          <div class="track"><div class="fill fill-before" style="width:{larghezza_prima:.1f}%"></div></div>
          <span class="pair-value mono">{_fmt_usd(scenario['cost_before'])}</span>
        </div>
        <div class="pair">
          <span class="pair-label">con</span>
          <div class="track"><div class="fill fill-after" style="width:{larghezza_dopo:.1f}%"></div></div>
          <span class="pair-value mono">{_fmt_usd(scenario['cost_after'])}</span>
        </div>
      </div>
      <div class="scenario-meta">
        <span class="pill pill-{stato} mono">{_fmt_pct(scenario['saved_ratio'], segno=True)}</span>
        <span class="muted">{_fmt_pct(scenario['cache_ratio'])} del prompt da cache</span>
        <span class="muted">{scenario['upstream_after']} di {scenario['requests']} richieste inoltrate</span>
      </div>
    </article>"""
        )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Per tipo di carico</h2>
    <p>Quattro carichi diversi, perche' il risparmio non e' un numero unico: dipende
    da quanto del prompt si ripete. Lo scenario <span class="mono">costruzione</span>
    non e' inventato &mdash; legge i file veri di questo repository e ricostruisce il
    traffico prodotto scrivendolo.</p>
  </div>
  <div class="scenario-list">{''.join(righe)}</div>
</section>"""


def _stages(data: dict[str, Any]) -> str:
    stadi = data.get("stages") or []
    if not stadi:
        return ""
    massimo = max((abs(s["saved_ratio"]) for s in stadi), default=0) or 1

    righe = []
    for stadio in stadi:
        quota = stadio["saved_ratio"]
        larghezza = abs(quota) / massimo * 100
        if quota > 0.001:
            stato, nota = "good", ""
        elif quota < -0.001:
            stato, nota = "bad", ""
        else:
            stato, nota = "idle", "non e' mai intervenuto su questi carichi"
        righe.append(
            f"""<tr>
      <td class="stage-name">{_esc(stadio['stage'])}
        {f'<span class="note">{_esc(nota)}</span>' if nota else ''}</td>
      <td class="stage-bar"><div class="track"><div class="fill fill-{stato}"
          style="width:{larghezza:.1f}%"></div></div></td>
      <td class="num mono">{_fmt_usd(stadio['saved_usd'])}</td>
      <td class="num mono">{_fmt_pct(quota, segno=True)}</td>
      <td class="num mono muted">{_fmt_pct(stadio['cumulative_ratio'])}</td>
    </tr>"""
        )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Quanto vale ogni stadio</h2>
    <p>Gli stadi vengono accesi uno alla volta, in modo cumulativo: la differenza
    fra un gradino e il precedente e' il contributo di quello stadio. E' una misura,
    non una stima &mdash; ed e' cosi' che si e' scoperto che l'effort adattivo non
    interveniva mai, perche' valutava la difficolta' guardando il prompt intero
    invece della domanda.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Stadio</th><th></th><th class="num">Contributo</th>
      <th class="num">Quota</th><th class="num">Cumulato</th></tr></thead>
      <tbody>{''.join(righe)}</tbody>
    </table>
  </div>
</section>"""


def _interactions(data: dict[str, Any]) -> str:
    """Le strategie di potatura del contesto, per carico."""
    voci = data.get("interactions") or []
    if not voci:
        return ""

    per_scenario: dict[str, list[dict[str, Any]]] = {}
    for voce in voci:
        per_scenario.setdefault(voce["scenario"], []).append(voce)

    righe = []
    for scenario, varianti in per_scenario.items():
        for indice, voce in enumerate(varianti):
            quota = voce["delta_ratio"]
            stato = "good" if quota > 0.001 else "bad" if quota < -0.001 else "idle"
            etichetta = (
                f'<td class="stage-name" rowspan="{len(varianti)}">{_esc(scenario)}</td>'
                if indice == 0
                else ""
            )
            righe.append(
                f"""<tr>
      {etichetta}
      <td>{_esc(voce['name'])}
        <span class="note">{_esc(voce['description'])}</span></td>
      <td class="num mono">{_fmt_usd(voce['cost_usd'])}</td>
      <td class="num mono">{_fmt_pct(voce['cache_ratio'])}</td>
      <td class="num"><span class="pill pill-{stato}">{_fmt_pct(quota, segno=True)}</span></td>
    </tr>"""
            )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Potare il contesto senza distruggere la cache</h2>
    <p>Per molto tempo questa misura ha detto che potare i vecchi risultati dei tool e
    mettere in cache sono incompatibili, e la conclusione sembrava definitiva. Non lo era:
    mancava un parametro.</p>
    <p>L'edit <code>clear_tool_uses_20250919</code> accetta <code>keep</code>, che il
    gateway lasciava al valore predefinito del server. Con <code>keep</code> fisso il
    confine di potatura sta sempre a N risultati dal fondo, quindi <strong>scorre di un
    risultato a ogni turno</strong>: l'insieme dei blocchi svuotati è diverso a ogni
    richiesta, il prefisso è nuovo per costruzione, e la cache non trova mai niente.
    Scegliendo invece quanti potarne <em>dall'inizio</em>, a scatti, fra uno scatto e
    l'altro vengono svuotati esattamente gli stessi blocchi.</p>
    <p class="caveat">Lo scatto si misura in <strong>turni</strong>, non in risultati: sei
    chiamate per turno ne consumano sei volte più in fretta di una, e contato in risultati
    lo stesso valore faceva tornare il confine a inseguire su metà dei carichi.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Carico</th><th>Strategia</th><th class="num">Costo</th>
      <th class="num">Da cache</th><th class="num">vs nessuna potatura</th></tr></thead>
      <tbody>{''.join(righe)}</tbody>
    </table>
  </div>
</section>"""

def _compaction(data: dict[str, Any]) -> str:
    """Confronto fra le strategie di compattazione della cronologia.

    La sezione esiste per una ragione precisa: comprimere il contesto sembra
    un risparmio ovvio e sui numeri non lo e' affatto, perche' riscrivere
    l'inizio del prompt fa mancare la cache. Questi quattro numeri sono il
    controesempio.
    """
    varianti = data.get("compaction") or []
    if not varianti:
        return ""

    righe = []
    for voce in varianti:
        quota = voce["delta_ratio"]
        if quota > 0.001:
            stato = "good"
        elif quota < -0.001:
            stato = "bad"
        else:
            stato = "idle"
        righe.append(
            f"""<tr>
      <td class="stage-name">{_esc(voce['name'])}
        <span class="note">{_esc(voce['description'])}</span></td>
      <td class="num mono">{_fmt_usd(voce['cost_usd'])}</td>
      <td class="num mono muted">{_fmt_usd(voce['aux_cost_usd'])}</td>
      <td class="num mono">{_fmt_pct(voce['cache_ratio'])}</td>
      <td class="num mono">{voce['summaries']}</td>
      <td class="num mono"><span class="pill pill-{stato}">{_fmt_pct(quota, segno=True)}</span></td>
    </tr>"""
        )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Comprimere la cronologia conviene?</h2>
    <p>Su una consulenza di quaranta turni, dove la cronologia diventa la voce di
    spesa principale. Il confronto e' contro il <strong>non comprimere affatto</strong>,
    e include il prezzo della compressione: la chiamata al riassuntore, e soprattutto
    il prompt caching che si perde quando il riassunto cambia.</p>
    <p>La colonna <em>riassunti</em> e' la piu' istruttiva: misura la stabilita' del
    prefisso. Un riassunto per turno significa un prompt nuovo per turno, quindi una
    cache che non viene mai riletta &mdash; ed e' il motivo per cui la prima strategia
    costa piu' del non fare nulla.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Strategia</th><th class="num">Costo</th>
      <th class="num">di cui riassunti</th><th class="num">Da cache</th>
      <th class="num">Riassunti</th><th class="num">vs non comprimere</th></tr></thead>
      <tbody>{''.join(righe)}</tbody>
    </table>
  </div>
</section>"""


def _prompt(data: dict[str, Any]) -> str:
    """Quanto vale accorciare il prompt, e quanto di quel valore e' verificato."""
    varianti = data.get("prompt") or []
    if not varianti:
        return ""

    righe = []
    for voce in varianti:
        quota = voce["delta_ratio"]
        stato = "good" if quota > 0.001 else "bad" if quota < -0.001 else "idle"
        marchio = (
            ""
            if voce["validated"]
            else '<span class="flag">non validato</span>'
        )
        righe.append(
            f"""<tr>
      <td class="stage-name">{_esc(voce['name'])} {marchio}
        <span class="note">{_esc(voce['description'])}</span></td>
      <td class="num mono">{_fmt_usd(voce['cost_usd'])}</td>
      <td class="num mono">{_fmt_int(voce['tokens_removed'])}</td>
      <td class="num mono muted">{_fmt_int(voce['tokens_removed_uncached'])}</td>
      <td class="num mono">{_fmt_pct(voce['cache_ratio'])}</td>
      <td class="num mono"><span class="pill pill-{stato}">{_fmt_pct(quota, segno=True)}</span></td>
    </tr>"""
        )

    # Sull'ultimo livello validato, non sul migliore: il massimo premierebbe
    # la variante che ha tolto pochi token e raddoppierebbe la resa apparente.
    validati = [v for v in varianti if v["validated"] and v["tokens_removed"]]
    resa = validati[-1]["yield_per_1k"] if validati else 0.0

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Accorciare il prompt</h2>
    <p>Tre livelli, in ordine di rischio. Il primo non cambia una parola: toglie
    spazi ripetuti, righe vuote, caratteri invisibili da copia e incolla. Il secondo
    toglie le perifrasi che introducono un'istruzione senza aggiungerle nulla. Il
    terzo sostituisce parole con sinonimi piu' corti.</p>
    <p><strong>La colonna della cache e' quella da guardare per prima:</strong> resta
    ferma su tutte le varianti. Le riscritture sono deterministiche e idempotenti, quindi
    lo stesso testo che torna indietro a ogni turno viene riscritto sempre allo stesso
    modo e il prefisso non si muove. Una riscrittura instabile qui varrebbe meno di zero.</p>
    <p class="caveat"><strong>Perche' un livello e' marcato "non validato".</strong>
    Il banco conta i token dalla lunghezza del testo. Va bene per chiedersi <em>dove</em>
    finiscono i token tolti, perche' li' conta la tariffa a cui vengono fatturati. Non va
    bene per chiedersi se &laquo;usare&raquo; costi davvero meno token di
    &laquo;utilizzare&raquo;: quello lo sa solo <code>messages.count_tokens</code>, e sotto
    questa metrica qualunque accorciamento sembra un guadagno per costruzione. Le
    sostituzioni lessicali restano spente finche' <code>ecotokens substitutions --live</code>
    non le ha verificate contro il tokenizer vero.</p>
    <p class="verdict-line">Resa misurata: <strong>{_fmt_usd(resa)} ogni mille token
    tolti</strong>, contro $0,0050 di prezzo pieno dell'input su Opus 5. La differenza e'
    lo sconto che il prompt caching aveva gia' fatto su quei token: accorciare il prompt
    rende circa un quarto di quello che sembra.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Livello</th><th class="num">Costo</th>
      <th class="num">Token tolti</th><th class="num">fuori dalla cache</th>
      <th class="num">Da cache</th><th class="num">vs originale</th></tr></thead>
      <tbody>{''.join(righe)}</tbody>
    </table>
  </div>
</section>"""


def _cache_key(data: dict[str, Any]) -> str:
    """Quanto vale normalizzare il testo prima di calcolare la chiave."""
    varianti = data.get("cache_key") or []
    if not varianti:
        return ""

    per_scenario: dict[str, list[dict[str, Any]]] = {}
    for voce in varianti:
        per_scenario.setdefault(voce["scenario"], []).append(voce)

    schede = []
    for scenario, voci in per_scenario.items():
        grezzo = next((v for v in voci if v["key_kind"] == "byte grezzi"), None)
        pulito = next((v for v in voci if v["key_kind"] == "testo normalizzato"), None)
        if grezzo is None or pulito is None:
            continue
        delta = (
            (grezzo["cost_usd"] - pulito["cost_usd"]) / grezzo["cost_usd"]
            if grezzo["cost_usd"]
            else 0.0
        )
        stato = "good" if delta > 0.001 else "idle"
        verdetto = (
            "normalizzare recupera hit che andavano persi"
            if delta > 0.001
            else "nessuna differenza: le richieste erano gia' identiche"
        )
        schede.append(
            f"""<article class="interaction state-{stato}">
      <header>
        <h3>{_esc(scenario)}</h3>
        <span class="pill pill-{stato} mono">{_fmt_pct(delta, segno=True)}</span>
      </header>
      <p class="verdict-line">{_esc(verdetto)}</p>
      <dl>
        <div><dt>chiave sui byte grezzi</dt>
          <dd class="mono">{_fmt_usd(grezzo['cost_usd'])}
          <span class="muted">&middot; {grezzo['hits']} hit su {grezzo['requests']}</span></dd></div>
        <div><dt>chiave sul testo normalizzato</dt>
          <dd class="mono">{_fmt_usd(pulito['cost_usd'])}
          <span class="muted">&middot; {pulito['hits']} hit su {pulito['requests']}</span></dd></div>
      </dl>
    </article>"""
        )

    if not schede:
        return ""

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>La chiave della cache</h2>
    <p>Due richieste che differiscono per uno spazio doppio, una riga vuota o una
    virgoletta tipografica sono la stessa domanda. Con la chiave calcolata sui byte
    grezzi finiscono su voci diverse, e la stessa risposta si paga tante volte quante
    sono le varianti.</p>
    <p class="verdict-line">È l'ottimizzazione con la <strong>resa più alta di tutto il
    gateway</strong>, e la ragione è aritmetica: ogni altra leva sconta il prezzo di un
    token, un hit di cache lo azzera. Il prompt caching serve un token a 0,1×; la cache
    esatta non lo serve affatto.</p>
    <p class="caveat">Il secondo carico esiste per verificare che non ci sia una
    regressione: lì le domande si ripetono già identiche, e normalizzare non può
    cambiare nulla. Infatti non cambia.</p>
  </div>
  <div class="interaction-grid">{''.join(schede)}</div>
</section>"""


def _overhead(data: dict[str, Any]) -> str:
    """Il testo che il gateway aggiunge di suo, prima e dopo la riscrittura."""
    overhead = data.get("overhead")
    if not overhead or not overhead.get("items"):
        return ""

    totali = overhead["totals"]
    righe = []
    for voce in overhead["items"]:
        stato = "good" if voce["saved"] > 0 else "bad" if voce["saved"] < 0 else "idle"
        righe.append(
            f"""<tr>
      <td class="stage-name">{_esc(voce['key'])}
        <span class="note">{_esc(voce['purpose'])}</span></td>
      <td class="num mono muted">{_fmt_int(voce['before'])}</td>
      <td class="num mono">{_fmt_int(voce['after'])}</td>
      <td class="num"><span class="pill pill-{stato}">{voce['saved']:+d}</span></td>
    </tr>"""
        )

    quota = totali["saved"] / totali["before"] if totali["before"] else 0.0

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Il testo che aggiunge il gateway</h2>
    <p>Il gateway non si limita a inoltrare: aggiunge testo suo. Delimitatori attorno al
    riassunto della cronologia, un blocco per i fatti ricordati, un'istruzione quando il
    client chiede JSON, le regole date al riassuntore. Sono token che l'utente paga senza
    averli scritti.</p>
    <p>A differenza del prompt dell'utente, questo testo è nostro: accorciarlo non cambia
    il comportamento di nessuna applicazione, e non richiede il permesso di nessuno. Un tag
    serve a separare, non a spiegare — <code>&lt;storico&gt;</code> delimita esattamente
    quanto <code>&lt;riassunto-conversazione-precedente&gt;</code> e costa un quarto.</p>
    <p class="verdict-line">Complessivamente <strong>{_fmt_int(totali['before'])} →
    {_fmt_int(totali['after'])} token</strong>, {_fmt_pct(quota)} in meno.</p>
    <p class="caveat">Onestà sulle proporzioni: sono token per occorrenza, non per
    richiesta, e la maggior parte delle voci compare di rado. Sul totale di una fattura
    incide poco. È stato fatto perché è gratis e senza rischio, non perché sposti l'ago.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Voce</th><th class="num">Prima</th>
      <th class="num">Adesso</th><th class="num">Variazione</th></tr></thead>
      <tbody>{''.join(righe)}</tbody>
    </table>
  </div>
</section>"""


def _cache_writes(data: dict[str, Any]) -> str:
    """Quanto del prompt caching e' scritto e mai riletto.

    E' la sezione nata da una constatazione aritmetica: il prompt caching vale
    il 67% del risparmio e gli altri quattro stadi insieme il 7%. Da un certo
    punto in poi, l'unico posto dove cercare ancora e' dentro il 67%.
    """
    righe_dati = data.get("cache_writes") or []
    if not righe_dati:
        return ""

    vero = data.get("cache_writes_live") or {}

    con_tetto = [voce for voce in righe_dati if voce["breakpoints"]]
    minimo = min((voce["cost_usd"] for voce in con_tetto), default=0.0)

    righe = []
    for voce in righe_dati:
        scritti = voce["token_scritti"]
        quota = voce["quota_sprecata"]
        # Il verde va al costo piu' basso, non allo spreco piu' basso: lo
        # spreco minimo e' della riga che non scrive niente, ed e' la peggiore.
        economico = bool(voce["breakpoints"]) and abs(voce["cost_usd"] - minimo) < 1e-9
        stato = "good" if voce["token_sprecati_in_mezzo"] == 0 else "idle"
        etichetta = _esc(voce["etichetta"])
        if not voce["breakpoints"]:
            etichetta = f'{etichetta} <span class="muted">(pianificatore off)</span>'
        righe.append(
            f"""<tr>
      <td class="stage-name">{etichetta}</td>
      <td class="num mono{' win' if economico else ''}">{_fmt_usd(voce['cost_usd'])}</td>
      <td class="num mono">{_fmt_int(scritti)}</td>
      <td class="num mono">{_fmt_int(voce['token_recuperati'])}</td>
      <td class="num"><span class="pill pill-{stato}">{_fmt_int(voce['token_sprecati_in_mezzo'])}</span></td>
      <td class="num mono muted">{_fmt_int(voce['token_sprecati_di_coda'])}</td>
      <td class="num mono">{_fmt_pct(quota)}</td>
    </tr>"""
        )

    if vero.get("scritture"):
        nota_vera = (
            f"""<p>Sul traffico vero registrato finora: <strong>{_fmt_int(vero['token_scritti'])}</strong>
    token scritti in cache, di cui <strong>{_fmt_int(vero['token_sprecati_in_mezzo'])}</strong>
    orfani in mezzo e {_fmt_int(vero['token_sprecati_di_coda'])} di coda, per un sovrapprezzo
    di {_fmt_usd(vero['costo_sprecato_usd'])} su {_fmt_int(vero['sessioni'])} sessioni.</p>"""
        )
    else:
        nota_vera = (
            """<p class="caveat">Sul traffico vero: <strong>nessuna scrittura registrata</strong>.
    La tabella qui sopra viene dal simulatore, e il simulatore non ha mai visto una
    cache vera. Vale come confronto fra configurazioni, non come misura di quanto si
    stia sprecando adesso.</p>"""
        )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Le scritture che nessuno rilegge</h2>
    <p>Una scrittura in cache costa <strong>1,25×</strong> (cinque minuti) o <strong>2×</strong>
    (un'ora); una rilettura costa <strong>0,1×</strong>. Riletta anche una sola volta, una
    scrittura è già in guadagno. Mai riletta, è una perdita netta pari al 25% del suo
    prezzo pieno: si è pagato di più per non avere niente in cambio.</p>
    <p>L'API non dice <em>quale</em> voce ha riletto, ma la cache è un match di prefisso e
    le letture crescono da sinistra: se una richiesta successiva della stessa sessione
    legge più a fondo, la differenza può venire solo da ciò che si era scritto prima.
    Si prende la lettura più favorevole al gateway, quindi questi numeri sono un
    <strong>limite inferiore</strong> allo spreco.</p>
    <p class="caveat">Le due colonne vanno lette insieme, e in quest'ordine: prima il costo,
    poi lo spreco. Lo spreco da solo si azzera spegnendo il pianificatore — che è la riga
    più cara della tabella. Un tetto più basso conviene solo se lo spreco scende
    <em>senza</em> che il costo salga.</p>
    {nota_vera}
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Tetto di breakpoint</th>
          <th class="num">Costo</th>
          <th class="num">Token scritti</th>
          <th class="num">Ripagati</th>
          <th class="num">Orfani in mezzo</th>
          <th class="num">Orfani di coda</th>
          <th class="num">Spreco</th>
        </tr>
      </thead>
      <tbody>
    {"".join(righe)}
      </tbody>
    </table>
  </div>
  <p class="note"><strong>Orfani in mezzo</strong> è l'unica quota su cui il pianificatore
  possa fare qualcosa: è una scrittura che altre richieste hanno seguito senza mai
  rileggerla. <strong>Di coda</strong> è l'ultima scrittura di una sessione, che nessuno
  poteva sapere fosse l'ultima — e che un'altra sessione con lo stesso prefisso potrebbe
  ancora rileggere. Sommarle darebbe un numero più grosso e meno utile.</p>
</section>"""

def _ceiling(data: dict[str, Any]) -> str:
    """Fin dove puo' arrivare il risparmio, e cosa lo ferma.

    Il numero di testa di questa pagina invita a una domanda sola: perche' non
    di piu'? La risposta e' aritmetica e va data insieme al numero, altrimenti
    la si cerca dove non c'e' - o peggio, la si ottiene ritoccando il corpus.
    """
    passi = data.get("ceiling") or []
    pavimento = data.get("ceiling_floor") or {}
    curva = data.get("repetition") or []
    if not passi or not pavimento:
        return ""

    riferimento = float(data.get("ceiling_baseline") or 0.0)
    massimo = float(data.get("ceiling_max") or 0.0)

    righe_passi = []
    for passo in passi:
        quota = passo["saved_ratio"]
        sicura = passo["sicura"]
        stato = "good" if sicura else "idle"
        scambio = (
            '<span class="muted">niente che non sia già misurato</span>'
            if sicura
            else _esc(passo["in_cambio"])
        )
        righe_passi.append(
            f"""<tr>
      <td class="stage-name">{_esc(passo['etichetta'])}
        <span class="note">{_esc(passo['descrizione'])}</span></td>
      <td class="num mono">{_fmt_usd(passo['cost_usd'])}</td>
      <td class="num"><span class="pill pill-{stato}">{_fmt_pct(quota)}</span></td>
      <td>{scambio}</td>
    </tr>"""
        )

    righe_pavimento = []
    for chiave, etichetta, perche in (
        ("output_usd", "Output generato",
         "nessuna cache lo sconta: non esisteva prima della richiesta"),
        ("input_nuovo_usd", "Input mai visto",
         "contenuto nuovo, va trasmesso almeno una volta"),
        ("riletture_usd", "Riletture da cache",
         "già scontate a 0,1×, ma non gratuite"),
    ):
        righe_pavimento.append(
            f"""<tr>
      <td class="stage-name">{etichetta}</td>
      <td class="num mono">{_fmt_usd(pavimento[chiave])}</td>
      <td class="muted">{perche}</td>
    </tr>"""
        )
    righe_pavimento.append(
        f"""<tr>
      <td class="stage-name"><strong>Totale</strong></td>
      <td class="num mono"><strong>{_fmt_usd(pavimento['totale_usd'])}</strong></td>
      <td class="muted">il massimo teorico è quindi {_fmt_pct(massimo)}</td>
    </tr>"""
    )

    righe_curva = []
    for punto in curva:
        quota = punto["saved_ratio"]
        stato = "good" if quota >= 0.95 else "idle"
        righe_curva.append(
            f"""<tr>
      <td class="stage-name">{punto['uniche']} domande &times;{punto['ripetizioni']}</td>
      <td class="num mono muted">{punto['richieste']}</td>
      <td class="num mono">{_fmt_usd(punto['baseline_usd'])}</td>
      <td class="num mono">{_fmt_usd(punto['cost_usd'])}</td>
      <td class="num"><span class="pill pill-{stato}">{_fmt_pct(quota)}</span></td>
    </tr>"""
        )

    necessarie = data.get("repetition_for_99")
    chiusura = (
        f"""<p>Su richieste tutte uguali il 99% arriva a circa
    <strong>{necessarie} ripetizioni</strong> della stessa domanda: la cache esatta non
    sconta il prezzo di un token, lo azzera. La prima richiesta però si paga sempre,
    quindi la curva sale verso il 100% senza toccarlo.</p>"""
        if necessarie
        else ""
    )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Fin dove si può arrivare</h2>
    <p>Il numero in testa a questa pagina invita a una domanda sola: perché non di
    più? La risposta è aritmetica, e conviene darla insieme al numero.</p>
    <p>Le leve non sono tutte della stessa natura. Le prime non costano niente che non
    sia già misurato; le ultime scambiano denaro contro <strong>qualità</strong>, e la
    qualità questo banco non la misura — sa quanto è lunga una risposta, non se è
    giusta. Metterle nella stessa colonna farebbe sembrare il 95% un traguardo
    raggiunto invece che un prezzo pagato.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Leva</th><th class="num">Costo</th><th class="num">Risparmio</th>
      <th>In cambio di</th></tr></thead>
      <tbody>
    {"".join(righe_passi)}
      </tbody>
    </table>
  </div>
  <div class="panel-head" style="margin-top:1.5rem">
    <h3>Il pavimento</h3>
    <p>Sotto una certa cifra non si scende, perché il modello deve pur rispondere.
    Valutato al modello più economico del listino e con l'input a prezzo pieno anziché
    a 1,25× — è un limite che nessuna configurazione può battere, non una stima
    realistica.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Voce</th><th class="num">Costo</th><th>Perché resta</th></tr></thead>
      <tbody>
    {"".join(righe_pavimento)}
      </tbody>
    </table>
  </div>
  <div class="panel-head" style="margin-top:1.5rem">
    <h3>Il risparmio dipende dal traffico, non dal gateway</h3>
    <p>«Quanto risparmia EcoTokens» non ha una risposta sola. Su richieste tutte
    diverse l'unica leva è il prefisso condiviso; su richieste che si ripetono entra la
    cache esatta, e quella non sconta un token: lo azzera.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Carico</th><th class="num">Richieste</th><th class="num">Senza</th>
      <th class="num">Con</th><th class="num">Risparmio</th></tr></thead>
      <tbody>
    {"".join(righe_curva)}
      </tbody>
    </table>
  </div>
  {chiusura}
  <p class="caveat">Il numero di testa è quello del corpus standard, che mescola
  carichi ripetitivi e carichi tutti diversi, su un riferimento di
  {_fmt_usd(riferimento)}. Alzarlo aggiungendo ripetizioni al corpus si farebbe in
  dieci minuti, e non misurerebbe più niente.</p>
</section>"""

def _calibration(data: dict[str, Any]) -> str:
    """Quanto sbaglia lo stimatore locale, misurato contro il tokenizer vero."""
    righe_dati = data.get("calibration") or []

    if not righe_dati:
        return """<section class="panel">
  <div class="panel-head">
    <h2>Quanto vale il metro</h2>
    <p>Ogni numero di questa pagina viene da uno stimatore che conta i token dalla
    lunghezza del testo. Va benissimo per chiedersi <em>a quale tariffa</em> un token
    viene fatturato — ed è la domanda a cui rispondono quasi tutte le misure qui — ma
    non dice quanto sbaglia in assoluto.</p>
    <p>Ogni chiamata a <code>POST /v1/messages/count_tokens</code> è un punto di
    taratura gratuito: l'API risponde con il conteggio vero, il gateway lo confronta
    con la propria stima e registra lo scarto. Non costa un token in più, perché quella
    chiamata era già stata fatta per rispondere al client.</p>
    <p class="caveat">Finora <strong>nessun campione</strong>. Servono credenziali e
    almeno un client che chieda un preventivo prima di generare.</p>
  </div>
</section>"""

    righe = []
    for voce in righe_dati:
        medio = voce.get("scarto_medio") or 0.0
        minimo = voce.get("scarto_min") or 0.0
        massimo = voce.get("scarto_max") or 0.0
        ampiezza = massimo - minimo
        # Una stima che sbaglia sempre allo stesso modo si corregge; una che
        # oscilla non si corregge, e la media da sola non lo farebbe vedere.
        stato = "good" if abs(medio) < 0.05 and ampiezza < 0.15 else "bad" if ampiezza > 0.4 else "idle"
        righe.append(
            f"""<tr>
      <td class="stage-name">{_esc(voce['model'])}</td>
      <td class="num mono">{_fmt_int(voce['campioni'])}</td>
      <td class="num mono">{_fmt_int(voce['token_esatti'])}</td>
      <td class="num"><span class="pill pill-{stato}">{_fmt_pct(medio, segno=True)}</span></td>
      <td class="num mono muted">{_fmt_pct(minimo, segno=True)} … {_fmt_pct(massimo, segno=True)}</td>
    </tr>"""
        )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Quanto vale il metro</h2>
    <p>Ogni numero di questa pagina viene da uno stimatore che conta i token dalla
    lunghezza del testo. Questa tabella dice di quanto sbaglia, confrontandolo con il
    conteggio vero dell'API su traffico reale.</p>
    <p>I campioni arrivano dalle chiamate a <code>POST /v1/messages/count_tokens</code>,
    e non costano un token in più: quella chiamata era già stata fatta per rispondere
    al client.</p>
    <p class="caveat">La colonna dello <strong>scarto medio</strong> da sola non basta.
    Una stima che sbaglia del +5% sempre è utilizzabile — si corregge. Una che oscilla
    fra −30% e +40% con media zero non lo è, e la media la farebbe sembrare perfetta:
    per questo accanto c'è l'intervallo.</p>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Modello</th><th class="num">Campioni</th>
      <th class="num">Token contati</th><th class="num">Scarto medio</th>
      <th class="num">Intervallo</th></tr></thead>
      <tbody>{''.join(righe)}</tbody>
    </table>
  </div>
</section>"""


def _progress(data: dict[str, Any]) -> str:
    """Ogni ottimizzazione confrontata con la misura precedente."""
    progresso = data.get("progress")
    if not progresso:
        return ""

    if not progresso.get("available"):
        return f"""<section class="panel">
  <div class="panel-head">
    <h2>Progressi rispetto alla versione precedente</h2>
    <p>Serve almeno una misura precedente sullo stesso corpus di scenari
    (<code>{_esc(progresso.get('corpus', ''))}</code>) per poter confrontare. Finora
    ne risulta {progresso.get('runs_found', 0)}. Il confronto comparira' alla prossima
    esecuzione di <code>ecotokens dashboard</code>.</p>
    <p class="caveat">Il vincolo non e' una formalita': aggiungere uno scenario cambia il
    denominatore di tutte le percentuali, e accostare due corpus diversi produrrebbe
    progressi immaginari.</p>
  </div>
</section>"""

    quando = time.strftime("%d/%m/%Y %H:%M", time.localtime(progresso["previous_at"]))
    righe = []
    for voce in progresso["stages"]:
        stato = voce["status"]
        classe = {
            "migliorato": "good",
            "peggiorato": "bad",
            "invariato": "idle",
            "nuovo": "idle",
        }[stato]
        if voce["before"] is None:
            prima = '<span class="muted">&mdash;</span>'
            delta = '<span class="pill pill-idle">nuovo</span>'
        else:
            prima = _fmt_pct(voce["before"])
            # Un delta sotto il mezzo punto base si mostra come zero pulito:
            # "-0,0%" e' rumore di arrotondamento travestito da regressione.
            valore = voce["delta"] if abs(voce["delta"]) > 0.0005 else 0.0
            delta = (
                f'<span class="pill pill-{classe}">'
                f'{_fmt_pct(valore, segno=True)}</span>'
            )
        righe.append(
            f"""<tr>
      <td class="stage-name">{_esc(voce['stage'])}</td>
      <td class="num mono muted">{prima}</td>
      <td class="num mono">{_fmt_pct(voce['now'])}</td>
      <td class="num">{delta}</td>
      <td class="muted">{_esc(stato)}</td>
    </tr>"""
        )

    if progresso.get("comparable"):
        avviso = f"""<p class="caveat">Il confronto e' limitato alle misure dello stesso
    corpus (<code>{_esc(progresso['corpus'])}</code>), con la stessa impronta di contenuto
    (<code class="mono">{_esc(progresso.get('fingerprint', ''))}</code>): le due misure hanno
    visto esattamente lo stesso carico.</p>"""
    else:
        prima = progresso.get("previous_fingerprint") or "sconosciuta"
        adesso = progresso.get("fingerprint") or "sconosciuta"
        avviso = f"""<p class="caveat"><span class="flag">confronto contaminato</span>
    Le due misure hanno impronte di contenuto diverse
    (<code class="mono">{_esc(prima)}</code> &rarr; <code class="mono">{_esc(adesso)}</code>):
    l'elenco degli scenari e' lo stesso, ma il carico no. Lo scenario
    <code>costruzione</code> legge i sorgenti veri del progetto al momento
    dell'esecuzione, quindi ogni commit che allunga il codice sposta anche il
    riferimento. <strong>Parte delle variazioni qui sotto e' crescita del metro, non
    merito del gateway.</strong> La riga resta visibile perche' nasconderla sarebbe
    peggio che segnalarla.</p>"""

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Progressi rispetto alla versione precedente</h2>
    <p>Ogni ottimizzazione confrontata con la misura del {_esc(quando)}, sullo stesso
    corpus di scenari. Le percentuali sono la quota di risparmio che quello stadio
    aggiunge da solo, non il totale.</p>
    {avviso}
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Ottimizzazione</th><th class="num">Prima</th>
      <th class="num">Adesso</th><th class="num">Variazione</th><th></th></tr></thead>
      <tbody>{''.join(righe)}</tbody>
    </table>
  </div>
</section>"""


def _tuning(data: dict[str, Any]) -> str:
    voci = data.get("tuning") or []
    if not voci:
        return ""
    schede = []
    for voce in voci:
        area = voce["area"]
        etichetta = "difetto del metro" if area == "misura" else "difetto del gateway"
        schede.append(
            f"""<article class="tuning tuning-{_esc(area)}">
      <header>
        <span class="tag tag-{_esc(area)}">{_esc(etichetta)}</span>
        <h3>{_esc(voce['title'])}</h3>
      </header>
      <p class="finding">{_esc(voce['finding'])}</p>
      <p class="effect"><span class="arrow" aria-hidden="true">&rarr;</span>{_esc(voce['effect'])}</p>
    </article>"""
        )
    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Cosa e' cambiato misurando</h2>
    <p>Ogni voce e' una convinzione smentita dai numeri. La distinzione conta:
    correggere il <em>metro</em> cambia cio' che si credeva, non cio' che il gateway fa;
    correggere il <em>gateway</em> cambia il comportamento. Tenerle separate evita di
    spacciare per miglioramento un errore di misura appena risolto.</p>
  </div>
  <div class="tuning-list">{''.join(schede)}</div>
</section>"""


def _history(data: dict[str, Any]) -> str:
    storico = data.get("history") or []
    if len(storico) < 2:
        return ""
    massimo = max(v["saved_ratio"] for v in storico) or 1
    minimo = min(0.0, min(v["saved_ratio"] for v in storico))
    intervallo = (massimo - minimo) or 1

    punti = []
    barre = []
    for indice, voce in enumerate(storico):
        x = (indice / max(1, len(storico) - 1)) * 100
        y = 100 - ((voce["saved_ratio"] - minimo) / intervallo) * 100
        punti.append(f"{x:.2f},{y:.2f}")
        etichetta = time.strftime("%d/%m %H:%M", time.localtime(voce["created_at"]))
        barre.append(
            f"""<li>
        <span class="h-label">{_esc(voce['label'])}</span>
        <span class="h-time mono muted">{etichetta}</span>
        <span class="h-value mono">{_fmt_pct(voce['saved_ratio'])}</span>
      </li>"""
        )

    linea = " ".join(punti)
    ultimo = punti[-1].split(",")
    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Come e' migliorato nel tempo</h2>
    <p>Ogni esecuzione del banco resta registrata: la serie mostra l'effetto delle
    correzioni fatte al gateway, non il rumore di misure diverse.</p>
  </div>
  <div class="spark">
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img"
         aria-label="Andamento del risparmio nel tempo">
      <polyline points="{linea}" class="spark-line" vector-effect="non-scaling-stroke" />
      <circle cx="{ultimo[0]}" cy="{ultimo[1]}" r="1.6" class="spark-dot" />
    </svg>
  </div>
  <ol class="history">{''.join(barre)}</ol>
</section>"""


def _live(data: dict[str, Any]) -> str:
    traffico = data.get("live")
    if not traffico:
        return """<section class="panel panel-quiet">
  <div class="panel-head">
    <h2>Traffico reale</h2>
    <p>Nessuna richiesta registrata finora. I numeri qui sopra vengono dal banco di
    misura; questa sezione si popola da sola appena un'applicazione comincia a usare
    il gateway.</p>
  </div>
</section>"""

    origini = "".join(
        f"""<li><span class="k-label">{_esc(riga['source'])}</span>
      <span class="k-val mono">{_fmt_int(riga['requests'])} richieste</span>
      <span class="k-mult mono">{_fmt_usd(riga['saved_usd'])}</span></li>"""
        for riga in traffico["by_source"]
    )
    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Traffico reale</h2>
    <p>Non e' una simulazione: sono le richieste davvero passate da questo gateway.</p>
  </div>
  <div class="verdict-grid">
    {_stat('Richieste servite', _fmt_int(traffico['requests']), 'dal primo avvio')}
    {_stat('Prompt da cache', _fmt_pct(traffico['cache_hit_ratio']), 'quota dei token di input')}
    {_stat('Costo effettivo', _fmt_usd(traffico['cost_usd']), 'con le ottimizzazioni attive')}
    {_stat('Senza ottimizzazioni', _fmt_usd(traffico['baseline_cost_usd']), 'stesso traffico, prezzo pieno')}
    {_stat('Risparmio', _fmt_usd(traffico['saved_usd']), 'differenza fra i due')}
    {_stat('Spesa di oggi', _fmt_usd(traffico['today_usd']), f"nel mese {_fmt_usd(traffico['month_usd'])}")}
  </div>
  <ul class="legend legend-wide">{origini}</ul>
</section>"""


# Gli stadi che possono restituire un contenuto diverso da quello che l'API
# avrebbe prodotto senza gateway. Non sono ottimizzazioni neutre: il banco misura
# quanto costa una risposta, non se e' la stessa risposta.
STADI_CHE_CAMBIANO_IL_CONTENUTO = (
    "cache semantica",
    "cambio di modello",
    "effort sempre basso",
)


def _config_prosa(stadi: list[dict[str, Any]]) -> str:
    """La frase del pannello, dedotta dallo stato invece che ricordata.

    E' stata sbagliata per un po': diceva che gli stadi che cambiano il contenuto
    erano spenti mentre la tabella sotto ne mostrava due accesi, perche' il
    profilo predefinito era diventato `aggressivo` e la frase no. Una didascalia
    che contraddice la propria tabella e' peggio di una assente - chi legge non
    sa quale delle due credere.
    """
    accesi = [
        stadio["name"]
        for stadio in stadi
        if stadio["name"] in STADI_CHE_CAMBIANO_IL_CONTENUTO and stadio["enabled"]
    ]
    spenti = [
        stadio["name"]
        for stadio in stadi
        if stadio["name"] in STADI_CHE_CAMBIANO_IL_CONTENUTO and not stadio["enabled"]
    ]
    if not accesi:
        return (
            "Gli stadi che possono cambiare il <em>contenuto</em> di una risposta "
            f"&mdash; {_elenco(spenti)} &mdash; sono spenti: quello che si "
            "risparmia qui e' la stessa risposta pagata meno."
        )
    frase = (
        f"Attenzione a come si legge il totale: {_elenco(accesi)} "
        f"{'cambiano' if len(accesi) > 1 else 'cambia'} il <em>contenuto</em> della "
        "risposta, non solo il suo prezzo. Il banco misura quanto e' lunga una "
        "risposta, non se e' giusta, quindi la parte di risparmio che arriva da qui "
        "e' interamente misurata e il suo costo interamente no."
    )
    if spenti:
        # "non e' in uso" invece di "resta spento": i nomi degli stadi hanno
        # generi diversi e la frase si costruisce senza sapere quali finiranno
        # nell'elenco.
        uso = "non sono in uso" if len(spenti) > 1 else "non e' in uso"
        frase += f" {_elenco(spenti).capitalize()} {uso}."
    return frase


def _elenco(nomi: list[str]) -> str:
    """`a`, `a e b`, `a, b e c` - senza virgola prima della congiunzione."""
    if len(nomi) <= 1:
        return _esc(nomi[0]) if nomi else ""
    return ", ".join(_esc(n) for n in nomi[:-1]) + " e " + _esc(nomi[-1])


def _config(data: dict[str, Any]) -> str:
    stadi = data.get("config") or []
    prosa = _config_prosa(stadi)
    voci = "".join(
        f"""<li class="{'on' if stadio['enabled'] else 'off'}">
      <span class="dot" aria-hidden="true"></span>
      <span class="c-name">{_esc(stadio['name'])}</span>
      <span class="c-detail muted">{_esc(stadio['detail'])}</span>
      <span class="c-state mono">{'attivo' if stadio['enabled'] else 'spento'}</span>
    </li>"""
        for stadio in stadi
    )
    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Configurazione in vigore</h2>
    <p>{prosa}</p>
  </div>
  <ul class="config">{voci}</ul>
</section>"""


def _footer(data: dict[str, Any]) -> str:
    modo = data.get("mode", "simulato")
    if modo == "live":
        nota = (
            "Le misure di questa pagina vengono da chiamate reali all'API Anthropic: "
            "token e costi sono quelli fatturati."
        )
    else:
        nota = (
            "Le misure vengono dal simulatore incluso nel progetto. La meccanica della "
            "cache e' fedele &mdash; match di prefisso, finestra di lookback di venti "
            "blocchi, marker fuori dall'impronta &mdash; ma i conteggi di token sono "
            "proporzionali alla dimensione del testo, non prodotti dal tokenizer vero. "
            "Le percentuali sono quindi indicative: <span class=\"mono\">ecotokens bench "
            "--live</span> le rifa' contro l'API reale."
        )
    return f"""<footer class="colophon">
  <p>{nota}</p>
  <p class="muted">Rigenerabile con <span class="mono">ecotokens dashboard</span>.</p>
</footer>"""


_STYLE = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zilla+Slab:wght@500;600;700&family=Public+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root {
  --ground: #eef1f2;
  --surface: #fbfcfc;
  --surface-sunken: #e6ebec;
  --ink: #12191b;
  --ink-soft: #58666b;
  --ink-faint: #8b989c;
  --rule: #d5dcde;
  --accent: #15616d;
  --accent-soft: #d7e6e8;
  --good: #1b7a4b;
  --good-soft: #cfe6da;
  --bad: #a33a24;
  --bad-soft: #f0d9d2;
  --warn: #b07d2b;
  --idle: #9aa6aa;
  --shadow: 0 1px 2px rgba(18, 25, 27, .06), 0 8px 24px -16px rgba(18, 25, 27, .28);
  --radius: 10px;
  --step: clamp(1.5rem, 3vw, 2.5rem);
  --font-display: "Zilla Slab", Georgia, serif;
  --font-body: "Public Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Consolas, monospace;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0e1315;
    --surface: #161d20;
    --surface-sunken: #101619;
    --ink: #e6ebec;
    --ink-soft: #9dabaf;
    --ink-faint: #6d7c81;
    --rule: #253034;
    --accent: #55b2c1;
    --accent-soft: #17323a;
    --good: #4bb582;
    --good-soft: #16332a;
    --bad: #dd8368;
    --bad-soft: #38211b;
    --warn: #d3a55a;
    --idle: #5d6a6e;
    --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 10px 30px -18px rgba(0, 0, 0, .8);
  }
}

:root[data-theme="dark"] {
  --ground: #0e1315;
  --surface: #161d20;
  --surface-sunken: #101619;
  --ink: #e6ebec;
  --ink-soft: #9dabaf;
  --ink-faint: #6d7c81;
  --rule: #253034;
  --accent: #55b2c1;
  --accent-soft: #17323a;
  --good: #4bb582;
  --good-soft: #16332a;
  --bad: #dd8368;
  --bad-soft: #38211b;
  --warn: #d3a55a;
  --idle: #5d6a6e;
  --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 10px 30px -18px rgba(0, 0, 0, .8);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: var(--font-body);
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

.page {
  max-width: 1080px;
  margin: 0 auto;
  padding: clamp(2rem, 5vw, 4rem) clamp(1rem, 4vw, 2rem) 4rem;
  display: flex;
  flex-direction: column;
  gap: var(--step);
}

.mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.muted { color: var(--ink-faint); }

h1, h2, h3 { font-family: var(--font-display); text-wrap: balance; margin: 0; }
h1 { font-size: clamp(2.4rem, 6vw, 3.4rem); font-weight: 600; letter-spacing: -.015em; line-height: 1.05; }
h2 { font-size: 1.4rem; font-weight: 600; letter-spacing: -.005em; }
h3 { font-size: 1.05rem; font-weight: 600; }
p { margin: 0; }

/* --- testata --- */
.masthead { display: flex; flex-direction: column; gap: .75rem; }
.eyebrow {
  font-size: .75rem; text-transform: uppercase; letter-spacing: .14em;
  color: var(--accent); font-weight: 600;
}
.lede { max-width: 62ch; color: var(--ink-soft); font-size: 1.05rem; }
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; font-size: .85rem; }
.chip {
  display: inline-flex; align-items: center; gap: .4rem;
  padding: .2rem .6rem; border-radius: 999px; font-size: .78rem; font-weight: 500;
  border: 1px solid var(--rule);
}
.chip::before {
  content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor;
}
.chip-sim { color: var(--warn); background: var(--surface); }
.chip-live { color: var(--good); background: var(--good-soft); }

/* --- verdetto --- */
.verdict {
  display: grid; grid-template-columns: minmax(230px, 1fr) 2fr; gap: 1px;
  background: var(--rule); border: 1px solid var(--rule);
  border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow);
}
.verdict-main {
  background: var(--surface); padding: 1.75rem;
  display: flex; flex-direction: column; justify-content: center; gap: .35rem;
}
.verdict-main.state-good { border-left: 4px solid var(--good); }
.verdict-main.state-bad { border-left: 4px solid var(--bad); }
.huge { font-size: clamp(2.8rem, 7vw, 3.8rem); font-weight: 600; line-height: 1; letter-spacing: -.03em; }
.state-good .huge { color: var(--good); }
.state-bad .huge { color: var(--bad); }
.verdict-main .sub { color: var(--ink-soft); font-size: .9rem; }
.verdict-grid {
  background: var(--rule);
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px;
}
.stat { background: var(--surface); padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: .15rem; }
.label {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--ink-faint); font-weight: 600;
}
.stat .value { font-size: 1.35rem; font-weight: 500; letter-spacing: -.02em; }
.stat .detail { font-size: .78rem; color: var(--ink-faint); }

/* --- pannelli --- */
.panel {
  background: var(--surface); border: 1px solid var(--rule);
  border-radius: var(--radius); padding: clamp(1.25rem, 3vw, 1.75rem);
  display: flex; flex-direction: column; gap: 1.25rem; box-shadow: var(--shadow);
}
.panel-quiet { box-shadow: none; background: var(--surface-sunken); }
.panel-head { display: flex; flex-direction: column; gap: .5rem; }
.panel-head p { max-width: 68ch; color: var(--ink-soft); font-size: .92rem; }

/* --- flusso token --- */
.flow-row { display: flex; flex-direction: column; gap: .6rem; }
.flow-head { display: flex; align-items: baseline; gap: .6rem; }
.flow-head p { font-size: .82rem; color: var(--ink-faint); }
.bar {
  display: flex; height: 30px; border-radius: 6px; overflow: hidden;
  background: var(--surface-sunken); border: 1px solid var(--rule);
}
.seg { height: 100%; }
.seg-full { background: var(--bad); }
.seg-write { background: var(--warn); }
.seg-read { background: var(--good); }
.legend { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: .4rem 1.5rem; }
.legend li { display: flex; align-items: center; gap: .5rem; font-size: .82rem; }
.legend-wide li { min-width: 240px; }
.key { width: 10px; height: 10px; border-radius: 3px; flex: none; }
.key-full { background: var(--bad); }
.key-write { background: var(--warn); }
.key-read { background: var(--good); }
.k-mult { color: var(--ink-faint); font-size: .78rem; }
.k-val { margin-left: auto; color: var(--ink-soft); }

/* --- scenari --- */
.scenario-list { display: flex; flex-direction: column; gap: 1.25rem; }
.scenario {
  display: grid; grid-template-columns: minmax(200px, 1fr) minmax(260px, 1.4fr);
  gap: .75rem 1.5rem; padding-bottom: 1.25rem; border-bottom: 1px solid var(--rule);
}
.scenario:last-child { border-bottom: 0; padding-bottom: 0; }
.scenario-head p { font-size: .84rem; color: var(--ink-faint); }
.scenario-bars { display: flex; flex-direction: column; gap: .4rem; justify-content: center; }
.pair { display: grid; grid-template-columns: 3rem 1fr 5.5rem; align-items: center; gap: .6rem; }
.pair-label { font-size: .74rem; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-faint); }
.pair-value { font-size: .84rem; text-align: right; }
.track { height: 12px; background: var(--surface-sunken); border-radius: 6px; overflow: hidden; }
.fill { height: 100%; border-radius: 6px; }
.fill-before { background: var(--bad); }
.fill-after { background: var(--good); }
.fill-good { background: var(--good); }
.fill-bad { background: var(--bad); }
.fill-idle { background: var(--idle); }
.scenario-meta {
  grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center;
  gap: .5rem 1rem; font-size: .8rem;
}
.pill {
  padding: .12rem .5rem; border-radius: 999px; font-size: .78rem; font-weight: 500;
}
.pill-good { background: var(--good-soft); color: var(--good); }
.pill-bad { background: var(--bad-soft); color: var(--bad); }
.pill-idle { background: var(--surface-sunken); color: var(--ink-faint); }
.flag {
  display: inline-block; padding: .05rem .4rem; border-radius: 4px;
  background: var(--bad-soft); color: var(--bad); font-size: .68rem;
  text-transform: uppercase; letter-spacing: .06em; vertical-align: middle;
}
.caveat {
  border-left: 2px solid var(--idle); padding-left: .8rem;
  color: var(--ink-faint); font-size: .86rem;
}
/* Nota di lettura sotto una tabella: spiega cosa distingue due colonne. */
p.note {
  margin-top: 1rem; max-width: 72ch;
  color: var(--ink-soft); font-size: .86rem; line-height: 1.55;
}
/* Il valore migliore di una colonna. Sta sul costo e mai sullo spreco: lo
   spreco minimo appartiene alla riga che non scrive in cache, che e' la
   configurazione peggiore. */
.win { color: var(--good); font-weight: 600; }

/* --- tabella stadi --- */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th {
  text-align: left; font-size: .72rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--ink-faint); font-weight: 600; padding: 0 .75rem .5rem 0;
  border-bottom: 1px solid var(--rule);
}
td { padding: .7rem .75rem .7rem 0; border-bottom: 1px solid var(--rule); vertical-align: middle; }
tbody tr:last-child td { border-bottom: 0; }
.num { text-align: right; }
th.num { padding-right: 0; }
.stage-name { font-weight: 500; min-width: 12rem; }
.stage-name .note { display: block; font-size: .76rem; color: var(--ink-faint); font-weight: 400; }
.stage-bar { width: 40%; min-width: 120px; }

/* --- interazioni --- */
.interaction-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
.interaction {
  background: var(--surface-sunken); border: 1px solid var(--rule);
  border-radius: 8px; padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: .6rem;
}
.interaction.state-good { border-left: 3px solid var(--good); }
.interaction.state-bad { border-left: 3px solid var(--bad); }
.interaction header { display: flex; align-items: center; justify-content: space-between; gap: .5rem; }
.verdict-line { font-size: .88rem; color: var(--ink-soft); }
.interaction dl { margin: 0; display: flex; flex-direction: column; gap: .4rem; }
.interaction dt {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-faint);
}
.interaction dd { margin: 0; font-size: .88rem; }

/* --- registro delle correzioni --- */
.tuning-list { display: flex; flex-direction: column; gap: .9rem; }
.tuning {
  border: 1px solid var(--rule); border-radius: 8px; padding: 1rem 1.1rem;
  display: flex; flex-direction: column; gap: .5rem; background: var(--surface-sunken);
}
.tuning-gateway { border-left: 3px solid var(--accent); }
.tuning-misura { border-left: 3px solid var(--warn); }
.tuning header { display: flex; flex-direction: column; gap: .35rem; }
.tag {
  align-self: flex-start; font-size: .68rem; text-transform: uppercase;
  letter-spacing: .1em; font-weight: 600; padding: .15rem .5rem; border-radius: 4px;
}
.tag-misura { background: var(--bad-soft); color: var(--warn); }
.tag-gateway { background: var(--accent-soft); color: var(--accent); }
.tuning .finding { font-size: .88rem; color: var(--ink-soft); max-width: 74ch; }
.tuning .effect { font-size: .88rem; max-width: 74ch; display: flex; gap: .5rem; }
.tuning .arrow { color: var(--accent); font-weight: 600; flex: none; }

/* --- storico --- */
.spark { height: 90px; }
.spark svg { width: 100%; height: 100%; overflow: visible; }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.spark-dot { fill: var(--accent); }
.history { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .3rem; }
.history li {
  display: grid; grid-template-columns: 1fr auto auto; gap: 1rem; align-items: baseline;
  font-size: .85rem; padding: .35rem 0; border-bottom: 1px solid var(--rule);
}
.history li:last-child { border-bottom: 0; }
.h-time { font-size: .78rem; }
.h-value { font-weight: 500; color: var(--good); }

/* --- configurazione --- */
.config { list-style: none; margin: 0; padding: 0; display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: .4rem 1.5rem; }
.config li {
  display: grid; grid-template-columns: auto 1fr auto; gap: .6rem; align-items: baseline;
  padding: .4rem 0; border-bottom: 1px solid var(--rule); font-size: .88rem;
}
.config .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--idle); align-self: center; }
.config .on .dot { background: var(--good); }
.config .c-detail { font-size: .78rem; }
.config .c-state { font-size: .76rem; color: var(--ink-faint); }
.config .on .c-state { color: var(--good); }

/* --- colophon --- */
.colophon {
  border-top: 1px solid var(--rule); padding-top: 1.25rem;
  display: flex; flex-direction: column; gap: .4rem;
  font-size: .84rem; color: var(--ink-soft); max-width: 72ch;
}

@media (max-width: 900px) {
  .verdict-grid { grid-template-columns: repeat(2, 1fr); }
}

@media (max-width: 720px) {
  .verdict { grid-template-columns: 1fr; }
  .scenario { grid-template-columns: 1fr; }
  .config li { grid-template-columns: auto 1fr; }
  .config .c-state { grid-column: 2; }
}

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
</style>"""
