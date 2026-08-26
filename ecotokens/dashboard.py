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
    FULL_VARIANT,
    load_runs,
    measure_pruning_interaction,
    open_results_store,
    run_ablation,
    run_benchmark,
    save_run,
    stage_contributions,
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
        "history": [],
        "totals": None,
        "live": None,
        "config": _config_snapshot(settings),
    }

    database, store = open_results_store(settings.storage.path)
    try:
        if measure:
            descrizioni = {s.name: s.description for s in all_scenarios(root)}

            misura = await run_benchmark(label="confronto A/B", project_root=root)
            await save_run(store, misura, corpus="confronto")
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
            await save_run(store, ablazione, corpus="ablazione")
            dati["stages"] = stage_contributions(ablazione)

            dati["interactions"] = [
                {
                    "scenario": voce.scenario,
                    "baseline_name": voce.baseline_name,
                    "variant_name": voce.variant_name,
                    "baseline_cost": voce.baseline_cost,
                    "variant_cost": voce.variant_cost,
                    "baseline_cache_ratio": voce.baseline_cache_ratio,
                    "variant_cache_ratio": voce.variant_cache_ratio,
                    "delta_ratio": voce.delta_ratio,
                    "helps": voce.helps,
                }
                for voce in await measure_pruning_interaction(project_root=root)
            ]

        dati["history"] = _summarise_history(await load_runs(store, limit=12))
        dati["live"] = await _live_traffic(store)
    finally:
        database.close()

    return dati


def _config_snapshot(settings: Settings) -> list[dict[str, Any]]:
    """Stato degli stadi, come lo vedrebbe una richiesta in arrivo adesso."""
    return [
        {"name": "prompt caching", "enabled": settings.cache_planner.enabled,
         "detail": f"max {settings.cache_planner.max_breakpoints} breakpoint"},
        {"name": "cache esatta", "enabled": settings.exact_cache.enabled,
         "detail": f"TTL {settings.exact_cache.ttl_seconds // 3600} h"},
        {"name": "cache semantica", "enabled": settings.semantic_cache.enabled,
         "detail": f"soglia {settings.semantic_cache.similarity_threshold}"},
        {"name": "potatura contesto", "enabled": settings.context.enabled,
         "detail": f"oltre il {settings.context.trigger_ratio * 100:.0f}% della finestra"},
        {"name": "effort adattivo", "enabled": settings.router.effort_downshift,
         "detail": f"domande sotto {settings.router.simple_max_question_tokens} token"},
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
    interazioni = data.get("interactions") or []
    if not interazioni:
        return ""
    schede = []
    for voce in interazioni:
        stato = "good" if voce["helps"] else "bad"
        verdetto = "conviene" if voce["helps"] else "costa di piu'"
        schede.append(
            f"""<article class="interaction state-{stato}">
      <header>
        <h3>{_esc(voce['scenario'])}</h3>
        <span class="pill pill-{stato} mono">{_fmt_pct(voce['delta_ratio'], segno=True)}</span>
      </header>
      <p class="verdict-line">Potare il contesto <strong>{_esc(verdetto)}</strong> su questo carico.</p>
      <dl>
        <div><dt>{_esc(voce['baseline_name'])}</dt>
          <dd class="mono">{_fmt_usd(voce['baseline_cost'])}
          <span class="muted">&middot; {_fmt_pct(voce['baseline_cache_ratio'])} da cache</span></dd></div>
        <div><dt>{_esc(voce['variant_name'])}</dt>
          <dd class="mono">{_fmt_usd(voce['variant_cost'])}
          <span class="muted">&middot; {_fmt_pct(voce['variant_cache_ratio'])} da cache</span></dd></div>
      </dl>
    </article>"""
        )

    return f"""<section class="panel">
  <div class="panel-head">
    <h2>Quando due ottimizzazioni litigano</h2>
    <p>Potare i vecchi risultati dei tool toglie token dal prompt, ma sposta il
    confine di taglio a ogni turno: il prefisso cambia e il prompt caching salta.
    Se il saldo sia positivo dipende dal carico, e non si puo' dedurre &mdash; si
    misura. Per questo la potatura resta una difesa contro l'esaurimento della
    finestra di contesto, non un modo per risparmiare.</p>
  </div>
  <div class="interaction-grid">{''.join(schede)}</div>
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


def _config(data: dict[str, Any]) -> str:
    stadi = data.get("config") or []
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
    <p>Gli stadi che possono cambiare il <em>contenuto</em> di una risposta &mdash;
    cache semantica e cambio di modello &mdash; sono spenti per scelta: accenderli
    e' una decisione, non un valore predefinito.</p>
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
