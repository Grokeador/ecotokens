"""Quadro: tutti i parametri del progetto su una schermata sola.

E' la terza pagina, e ha un mestiere che le altre due non hanno.

* La **dashboard** e' un rapporto: spiega, argomenta, mostra come si e'
  arrivati a un numero. Si legge una volta.
* La **console** guarda il traffico vero, dal vivo, e risponde a "cosa sta
  succedendo adesso".
* Il **quadro** e' un cruscotto: nessuna prosa, tutto insieme, si guarda in
  cinque secondi per vedere se qualcosa si e' mosso. Si tiene aperto.

Due vincoli che derivano da quel mestiere, e che ne spiegano il codice.

**Si apre subito.** Non misura niente: legge le misure gia' registrate nel
database - l'ultimo confronto, l'ultima ablazione, l'ultima ritenzione, il
traffico vero. Una pagina di controllo che si fa aspettare non viene guardata,
e una che non viene guardata non controlla niente. Le misure si rifanno con i
comandi che le producono; qui si vede il loro esito.

**Dice quando un numero e' vecchio.** Un cruscotto che mostra la misura di tre
settimane fa senza dirlo e' peggio di uno vuoto: chi lo legge crede di sapere
com'e' adesso. Ogni riquadro porta la propria data, e quando manca del tutto lo
dice invece di mostrare uno zero.
"""

from __future__ import annotations

import html
import time
from typing import Any

from .config import Settings
from .store.db import Database
from .store.repos import Store

# --- raccolta -------------------------------------------------------------


async def build_quadro_data(settings: Settings, store: Store) -> dict[str, Any]:
    """Solo letture. Nessuna misura viene eseguita da questa pagina."""
    from .bench import (
        BASELINE_VARIANT,
        FULL_VARIANT,
        RIFERIMENTO_MODERNO,
        ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA,
        ABLATION_STEPS,
        load_runs,
    )

    corse = await load_runs(store, limit=60)
    confronto = _ultima(corse, "confronto")
    ablazione = _ultima(corse, "ablazione")

    dati: dict[str, Any] = {
        "generated_at": time.time(),
        "profilo": settings.profilo,
        "confronto": _riassumi_confronto(confronto, BASELINE_VARIANT, FULL_VARIANT),
        "stadi": _riassumi_ablazione(ablazione, ABLATION_STEPS, BASELINE_VARIANT),
        "vs_automatico": _riassumi_vs_automatico(
            ablazione, RIFERIMENTO_MODERNO, ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA,
            ABLATION_STEPS[-1][0],
        ),
        "ritenzione": await store.latest_retention(),
        "scritture": await store.cache_write_report(),
        "taratura": await store.estimate_calibration(),
        "traffico": await store.stats(),
        "spesa": dict(zip(("oggi", "mese"), await store.current_spend())),
        "stadi_vivi": await store.stage_activity(),
        "config": _config(settings),
    }
    return dati


def _ultima(corse: list[dict[str, Any]], prefisso: str) -> dict[str, Any] | None:
    """La corsa piu' recente di quel tipo. `load_runs` le da' gia' in ordine."""
    for corsa in corse:
        if (corsa.get("corpus") or "").startswith(prefisso):
            return corsa
    return None


def _costi(corsa: dict[str, Any] | None) -> dict[str, float]:
    if not corsa:
        return {}
    per_variante: dict[str, float] = {}
    for riga in corsa.get("results", []):
        per_variante[riga["variant"]] = per_variante.get(riga["variant"], 0.0) + riga["cost_usd"]
    return per_variante


def _riassumi_confronto(corsa, baseline: str, completo: str) -> dict[str, Any]:
    costi = _costi(corsa)
    prima, dopo = costi.get(baseline), costi.get(completo)
    if prima is None or dopo is None:
        return {}
    per_scenario = {}
    for riga in corsa.get("results", []):
        voce = per_scenario.setdefault(riga["scenario"], {"prima": 0.0, "dopo": 0.0})
        chiave = "prima" if riga["variant"] == baseline else "dopo" if riga["variant"] == completo else None
        if chiave:
            voce[chiave] += riga["cost_usd"]
    return {
        "quando": corsa["created_at"],
        "impronta": corsa.get("fingerprint") or "",
        "corpus": corsa.get("corpus") or "",
        "modo": corsa.get("mode") or "",
        "prima": prima,
        "dopo": dopo,
        "quota": (prima - dopo) / prima if prima else 0.0,
        "scenari": [
            {"nome": nome, "quota": (v["prima"] - v["dopo"]) / v["prima"] if v["prima"] else 0.0}
            for nome, v in sorted(
                per_scenario.items(),
                key=lambda kv: -((kv[1]["prima"] - kv[1]["dopo"]) / kv[1]["prima"] if kv[1]["prima"] else 0),
            )
        ],
    }


def _riassumi_ablazione(corsa, passi, baseline: str) -> dict[str, Any]:
    costi = _costi(corsa)
    riferimento = costi.get(baseline)
    if not riferimento:
        return {}
    voci, precedente = [], riferimento
    for nome, _ in passi[1:]:
        if nome not in costi:
            break
        corrente = costi[nome]
        voci.append(
            {
                "nome": nome.removeprefix("+ ").strip(),
                "quota": (precedente - corrente) / riferimento,
                "cumulato": (riferimento - corrente) / riferimento,
            }
        )
        precedente = corrente
    return {"quando": corsa["created_at"], "riferimento": riferimento, "voci": voci}


def _riassumi_vs_automatico(corsa, riferimento: str, prudente: str, aggressivo: str) -> dict[str, Any]:
    costi = _costi(corsa)
    base = costi.get(riferimento)
    if not base:
        return {}
    esito = {"base": base}
    for chiave, variante in (("prudente", prudente), ("aggressivo", aggressivo)):
        if variante in costi:
            esito[chiave] = (base - costi[variante]) / base
    return esito


def ricostruisci_vs_automatico(corse: list[dict[str, Any]]) -> dict[str, Any]:
    """Il confronto col caching automatico, dall'ultima ablazione registrata.

    Nella forma che la dashboard si aspetta - con `by_scenario` - cosi' la
    pagina servita dal gateway mostra lo stesso pannello di quella generata a
    mano, invece di ometterlo perche' nessuno ha ripetuto la misura.
    """
    from .bench import RIFERIMENTO_MODERNO, ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA, ABLATION_STEPS

    corsa = _ultima(corse, "ablazione")
    if not corsa:
        return {}
    per_scenario: dict[str, dict[str, float]] = {}
    for riga in corsa.get("results", []):
        voce = per_scenario.setdefault(riga["scenario"], {})
        voce[riga["variant"]] = voce.get(riga["variant"], 0.0) + riga["cost_usd"]

    costi = _costi(corsa)
    base = costi.get(RIFERIMENTO_MODERNO)
    prudente = costi.get(ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA)
    completo = costi.get(ABLATION_STEPS[-1][0])
    if not base or prudente is None or completo is None:
        return {}

    righe = []
    for nome, varianti in per_scenario.items():
        riferimento = varianti.get(RIFERIMENTO_MODERNO)
        dopo = varianti.get(ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA)
        if not riferimento or dopo is None:
            continue
        righe.append(
            {
                "scenario": nome,
                "reference_usd": riferimento,
                "cost_usd": dopo,
                "saved_ratio": (riferimento - dopo) / riferimento,
            }
        )
    righe.sort(key=lambda voce: -voce["saved_ratio"])
    return {
        "reference_usd": base,
        "senza_cambiare_la_risposta": {
            "cost_usd": prudente,
            "saved_ratio": (base - prudente) / base,
        },
        "cambiando_la_risposta": {
            "cost_usd": completo,
            "saved_ratio": (base - completo) / base,
        },
        "by_scenario": righe,
    }


def _config(settings: Settings) -> list[dict[str, Any]]:
    """Gli interruttori, con il motivo di quelli spenti.

    Il motivo si prende dalla sezione di configurazione quando c'e' - e' la'
    che si decide, quindi e' la' che va scritto - e altrimenti si dichiara qui.
    Nessun interruttore spento resta senza: "spento" da solo costringe chi
    legge a chiedere a qualcun altro, ed e' successo davvero.
    """
    def voce(nome: str, sezione: Any, acceso: bool, dettaglio: str,
             motivo: str = "") -> dict[str, Any]:
        return {
            "nome": nome,
            "acceso": acceso,
            "dettaglio": dettaglio
            if acceso
            else (motivo or getattr(sezione, "motivo_se_spenta", "")),
        }

    return [
        # Il profilo per primo: e' l'interruttore che governa gli altri, e senza
        # di lui la pagina non direbbe la cosa piu' importante - se una parte
        # del risparmio e' un'altra risposta invece della stessa pagata meno.
        {
            "nome": f"profilo {settings.profilo}",
            "acceso": True,
            "dettaglio": "modello ed effort cambiano: parte del risparmio "
            "e' un'altra risposta"
            if settings.profilo == "aggressivo"
            else "nessuno stadio tocca il contenuto delle risposte",
        },
        voce("prompt caching", settings.cache_planner, settings.cache_planner.enabled,
             f"{settings.cache_planner.mode}, max {settings.cache_planner.max_breakpoints}",
             motivo="nessun breakpoint: ogni prompt si paga a prezzo pieno"),
        voce("cache esatta", settings.exact_cache, settings.exact_cache.enabled,
             f"TTL {settings.exact_cache.ttl_seconds // 3600} h",
             motivo="due richieste identiche si pagano due volte"),
        voce("cache semantica", settings.semantic_cache, settings.semantic_cache.enabled,
             f"soglia {settings.semantic_cache.similarity_threshold}"),
        voce("potatura contesto", settings.context, settings.context.enabled,
             f"scatti da {settings.context.prune_step_turns} turni",
             motivo="niente viene tolto dal contesto: nessun rischio di perdere "
                    "un dato, nessuna difesa dall'overflow"),
        voce("riassunto", settings.context, settings.context.local_compaction,
             f"tetto {settings.context.summary_max_tokens} token",
             motivo="la cronologia vecchia resta integrale, e si paga intera"),
        voce("effort", settings.router, settings.router.effort_downshift,
             settings.router.effort_policy,
             motivo="il client decide da solo quanto far ragionare il modello"),
        voce("cambio modello", settings.router, settings.router.model_downgrade,
             settings.router.downgrade_policy,
             motivo="cambia il modello, quindi la risposta; e azzera la cache, "
                    "che e' legata al modello"),
        voce("memoria", settings.memory, settings.memory.enabled,
             f"recupero {settings.memory.retrieval}, max {settings.memory.max_facts_stable}"),
        voce("tetto di spesa", settings.budget, settings.budget.enabled,
             f"${settings.budget.daily_usd:.2f}/giorno"),
    ]


async def quadro_da_percorso(settings: Settings) -> dict[str, Any]:
    """Comodita' per la riga di comando: apre il database e chiude."""
    database = Database(settings.storage.path)
    database.connect()
    try:
        return await build_quadro_data(settings, Store(database))
    finally:
        database.close()


# --- pagina ---------------------------------------------------------------


def _esc(valore: Any) -> str:
    return html.escape(str(valore), quote=True)


def _pct(valore: float, segno: bool = False) -> str:
    return f"{valore * 100:{'+' if segno else ''}.1f}%"


def _usd(valore: float) -> str:
    return f"${valore:.4f}" if abs(valore) >= 0.01 or valore == 0 else f"${valore:.6f}"


def _num(valore: float) -> str:
    return f"{int(valore):,}".replace(",", " ")


def _eta(quando: float | None) -> str:
    """Da quanto e' vecchia una misura. Un cruscotto senza questo mente.

    Non e' una decorazione: la differenza fra "risparmi il 95%" e "risparmiavi
    il 95% tre settimane fa, prima di quattro modifiche" e' tutta qui, e senza
    la data la prima frase si legge al posto della seconda.
    """
    if not quando:
        return "mai misurato"
    minuti = (time.time() - quando) / 60
    if minuti < 60:
        return f"{int(minuti)} min fa"
    if minuti < 60 * 48:
        return f"{int(minuti / 60)} h fa"
    return f"{int(minuti / 1440)} g fa"


def _riquadro(titolo: str, eta: str, corpo: str, largo: int = 1, denso: bool = False) -> str:
    """Un riquadro. `denso` dispone l'elenco su due colonne.

    Serve dove le voci sono molte: la griglia allunga ogni riquadro al piu'
    alto della sua banda, quindi un elenco lungo in un angolo alza tutta la
    riga e la pagina smette di stare in una schermata - che e' l'unico
    requisito di questa pagina.
    """
    classi = f"box span{largo}" + (" denso" if denso else "")
    return (
        f'<section class="{classi}">'
        f'<h2>{_esc(titolo)}<span class="eta">{_esc(eta)}</span></h2>'
        f"{corpo}</section>"
    )


def _righe(coppie: list[tuple[str, str, str]]) -> str:
    """Righe etichetta / valore / nota: la forma di quasi tutto, qui dentro."""
    return '<ul class="rows">' + "".join(
        f'<li><span class="k">{_esc(k)}</span>'
        f'<span class="v">{v}</span>'
        f'<span class="n">{_esc(n)}</span></li>'
        for k, v, n in coppie
    ) + "</ul>"


def _mai(comando: str) -> str:
    """Un riquadro senza dati dice cosa eseguire, non mostra zeri.

    Uno zero e' una misura; il vuoto no. Confonderli e' il modo piu' rapido di
    far leggere "nessuno spreco" dove si dovrebbe leggere "non lo sappiamo".
    """
    return (
        f'<p class="empty">Mai misurato &mdash; '
        f'<span class="mono">{_esc(comando)}</span> lo produce.</p>'
    )


def render_quadro(d: dict[str, Any]) -> str:
    """Una schermata sola, densa, senza prosa. Si guarda, non si legge."""
    confronto = d["confronto"]
    stadi = d["stadi"]
    versus = d["vs_automatico"]
    traffico = d["traffico"]
    scritture = d["scritture"]

    # --- verdetto ---------------------------------------------------------
    if confronto:
        verdetto = _righe(
            [
                (
                    "vs nessuna cache",
                    f'<b class="good">{_pct(confronto["quota"])}</b>',
                    f'{_usd(confronto["prima"])} -> {_usd(confronto["dopo"])}',
                ),
                (
                    "vs caching automatico",
                    f'<b class="good">{_pct(versus["aggressivo"])}</b>'
                    if versus.get("aggressivo") is not None
                    else "&mdash;",
                    f'prudente {_pct(versus["prudente"])}'
                    if versus.get("prudente") is not None
                    else "",
                ),
                (
                    "profilo",
                    f'<b>{_esc(d["profilo"])}</b>',
                    "cambia il contenuto"
                    if d["profilo"] == "aggressivo"
                    else "non tocca il contenuto",
                ),
                (
                    "corpus",
                    f'<span class="mono">{_esc(confronto["corpus"])}</span>',
                    f'impronta {confronto["impronta"][:8]}',
                ),
            ]
        )
    else:
        verdetto = _mai("ecotokens bench")

    # --- contributo per stadio -------------------------------------------
    if stadi.get("voci"):
        corpo_stadi = '<ul class="bars">' + "".join(
            f'<li><span class="k">{_esc(v["nome"])}</span>'
            f'<span class="bar"><i style="width:{max(0.0, min(1.0, v["cumulato"])) * 100:.1f}%"></i></span>'
            f'<span class="v">{_pct(v["quota"], segno=True)}</span>'
            f'<span class="n">{_pct(v["cumulato"])}</span></li>'
            for v in stadi["voci"]
        ) + "</ul>"
    else:
        corpo_stadi = _mai("ecotokens ablate")

    # --- per carico -------------------------------------------------------
    carichi = (
        _righe([(s["nome"], _pct(s["quota"]), "") for s in confronto["scenari"]])
        if confronto.get("scenari")
        else _mai("ecotokens bench")
    )

    # --- ritenzione -------------------------------------------------------
    ritenzione = d["ritenzione"]
    if ritenzione.get("rows"):
        per_variante: dict[str, list[int]] = {}
        for riga in ritenzione["rows"]:
            voce = per_variante.setdefault(riga["variant"], [0, 0])
            voce[0] += riga["kept"]
            voce[1] += riga["lost"]
        righe_rit = []
        for nome, (tenuti, persi) in per_variante.items():
            totale = tenuti + persi
            quota = tenuti / totale if totale else 0.0
            colore = "good" if quota == 1 else "bad" if quota == 0 else "warn"
            righe_rit.append(
                (
                    nome,
                    f'<b class="{colore}">{quota * 100:.0f}%</b>',
                    f"{persi} persi" if persi else "tutti",
                )
            )
        corpo_rit = _righe(righe_rit)
    else:
        corpo_rit = _mai("ecotokens ritenzione")

    # --- scritture sprecate ----------------------------------------------
    if scritture.get("scritture"):
        corpo_scritture = _righe(
            [
                (
                    "scritture",
                    _num(scritture["scritture"]),
                    f'{_num(scritture["token_scritti"])} token',
                ),
                (
                    "sprecati, evitabili",
                    f'<b class="warn">{_num(scritture["token_sprecati_in_mezzo"])}</b>',
                    _usd(scritture["costo_sprecato_in_mezzo_usd"]),
                ),
                (
                    "sprecati, strutturali",
                    _num(scritture["token_sprecati_di_coda"]),
                    "coda di sessione",
                ),
                ("quota sprecata", _pct(scritture["quota_sprecata"]), "sul totale scritto"),
            ]
        )
    else:
        corpo_scritture = '<p class="empty">Nessuna scrittura nel traffico registrato.</p>'

    # --- traffico vero ----------------------------------------------------
    if traffico.get("requests"):
        base = float(traffico.get("baseline_cost_usd") or 0)
        costo = float(traffico.get("cost_usd") or 0)
        corpo_traffico = _righe(
            [
                (
                    "richieste",
                    _num(traffico["requests"]),
                    f'{_num(traffico["total_prompt_tokens"])} token',
                ),
                (
                    "risparmio",
                    f'<b class="good">{_pct((base - costo) / base if base else 0)}</b>',
                    f"{_usd(costo)} su {_usd(base)}",
                ),
                ("prompt da cache", _pct(traffico.get("cache_hit_ratio", 0)), ""),
                ("spesa oggi", _usd(d["spesa"]["oggi"]), f'mese {_usd(d["spesa"]["mese"])}'),
            ]
        )
    else:
        corpo_traffico = (
            '<p class="empty">Nessuna richiesta e&#768; ancora passata dal gateway. '
            'La <a href="/">console</a> si popola da sola appena ne arriva una.</p>'
        )

    # --- stadi sul traffico vero -----------------------------------------
    vivi = d["stadi_vivi"]
    corpo_vivi = (
        _righe(
            [
                (v["stage"], f'{v["acted_in"]}/{v["enabled_in"]}', _pct(v["ratio"]))
                for v in vivi
            ]
        )
        if vivi
        else '<p class="empty">Serve traffico vero.</p>'
    )

    # --- taratura ---------------------------------------------------------
    taratura = d["taratura"]
    corpo_taratura = (
        _righe(
            [
                (
                    t["model"],
                    _pct(t.get("scarto_medio") or 0, segno=True),
                    f'{t["campioni"]} campioni',
                )
                for t in taratura
            ]
        )
        if taratura
        else _mai("/v1/messages/count_tokens")
    )

    # --- interruttori -----------------------------------------------------
    corpo_config = '<ul class="switches">' + "".join(
        f'<li class="{"on" if v["acceso"] else "off"}">'
        f'<span class="dot"></span><span class="k">{_esc(v["nome"])}</span>'
        f'<span class="n">{_esc(v["dettaglio"])}</span></li>'
        for v in d["config"]
    ) + "</ul>"

    quando = time.strftime("%d/%m %H:%M", time.localtime(d["generated_at"]))
    return f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EcoTokens - quadro</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Quadro <span class="sub">non misura niente: legge, e ogni riquadro porta la propria et&agrave;</span></h1>
    <nav><a href="/">console dal vivo</a> &middot;
    <a href="/admin/dashboard">rapporto esteso</a> &middot;
    <span class="mono">{_esc(quando)}</span></nav>
  </header>
  <main class="grid">
    {_riquadro("Verdetto", _eta(confronto.get("quando")), verdetto)}
    {_riquadro("Contributo per stadio", _eta(stadi.get("quando")), corpo_stadi, largo=2)}
    {_riquadro("Per carico", _eta(confronto.get("quando")), carichi)}
    {_riquadro("Ritenzione", _eta(ritenzione.get("created_at")), corpo_rit)}
    {_riquadro("Cache scritta e mai riletta", "traffico vero", corpo_scritture)}
    {_riquadro("Traffico vero", "dal primo avvio", corpo_traffico)}
    {_riquadro("Stadi sul traffico vero", "dal primo avvio", corpo_vivi, denso=True)}
    {_riquadro("Taratura dello stimatore", "senza costo", corpo_taratura)}
    {_riquadro("Interruttori", "adesso", corpo_config, largo=2)}
  </main>
</div>
</body>
</html>
"""


# Denso di proposito: corpo piccolo, cifre tabulari, riquadri stretti. Non e'
# una pagina da leggere, e' una da guardare - il criterio e' che tutto stia su
# una schermata senza scorrere, a 1280 di larghezza.
_CSS = """
:root {
  --ground: #eef1f2; --surface: #fbfcfc; --sunken: #e6ebec;
  --ink: #12191b; --soft: #58666b; --faint: #8b989c; --rule: #d5dcde;
  --accent: #15616d; --good: #1b7a4b; --warn: #b07d2b; --bad: #a33a24;
  --idle: #9aa6aa;
  --mono: ui-monospace, "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0d1214; --surface: #161d20; --sunken: #101619;
    --ink: #e6ebec; --soft: #9dabaf; --faint: #6d7c81; --rule: #253034;
    --accent: #55b2c1; --good: #4bb582; --warn: #d3a55a; --bad: #dd8368;
    --idle: #5d6a6e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font: 13px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.page { max-width: 1280px; margin: 0 auto; padding: .75rem 1rem 1rem; }
header {
  display: flex; flex-wrap: wrap; gap: .35rem 1.5rem;
  align-items: baseline; justify-content: space-between; margin-bottom: .6rem;
}
h1 { margin: 0; font-size: 1.2rem; font-weight: 600; letter-spacing: -.015em; }
.sub { font-size: .75rem; font-weight: 400; color: var(--faint); margin-left: .5rem; }
nav { font-size: .78rem; color: var(--faint); }
a { color: var(--accent); }
.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }

.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: .7rem; align-items: start; }
.span2 { grid-column: span 2; }
.box {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 7px;
  padding: .55rem .7rem .65rem;
}
.box h2 {
  margin: 0 0 .4rem; font-size: .7rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--soft);
  display: flex; justify-content: space-between; align-items: baseline; gap: .5rem;
}
.eta { font-size: .65rem; font-weight: 500; text-transform: none; letter-spacing: 0; color: var(--faint); }

ul { list-style: none; margin: 0; padding: 0; }
.rows li {
  display: grid; grid-template-columns: 1fr auto; gap: 0 .5rem;
  padding: .18rem 0; border-bottom: 1px solid var(--rule);
}
.rows li:last-child { border-bottom: none; }
.rows .k { color: var(--soft); font-size: .78rem; }
.rows .v { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: .82rem; text-align: right; }
.rows .n { grid-column: 1 / -1; font-size: .68rem; color: var(--faint); font-family: var(--mono); }
.rows .n:empty { display: none; }
b { font-weight: 600; }
.good { color: var(--good); } .warn { color: var(--warn); } .bad { color: var(--bad); }

.bars li {
  display: grid; grid-template-columns: 8.5rem 1fr 3.4rem 3.2rem;
  align-items: center; gap: .5rem; padding: .13rem 0; font-size: .78rem;
}
.bars .k { color: var(--soft); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar { height: 9px; background: var(--sunken); border-radius: 3px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); }
.bars .v, .bars .n { font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; }
.bars .n { color: var(--faint); font-size: .72rem; }

.switches { display: grid; grid-template-columns: repeat(3, 1fr); gap: .1rem .8rem; }
.switches li { display: grid; grid-template-columns: 8px 1fr; gap: .45rem; align-items: baseline; padding: .16rem 0; }
.switches .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--idle); align-self: center; }
.switches.on .dot, .switches li.on .dot { background: var(--good); }
.switches .k { font-size: .78rem; }
.switches li.off .k { color: var(--faint); }
.switches .n { grid-column: 2; font-size: .67rem; color: var(--faint); line-height: 1.3; }

.denso .rows { display: grid; grid-template-columns: 1fr 1fr; gap: 0 .9rem; }
.denso .rows li { border-bottom: none; }

.empty { margin: 0; font-size: .76rem; color: var(--faint); }
.empty .mono { color: var(--soft); }

@media (max-width: 1100px) { .grid { grid-template-columns: repeat(2, 1fr); } .switches { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 640px) { .grid { grid-template-columns: 1fr; } .span2 { grid-column: span 1; } .switches { grid-template-columns: 1fr; } }
"""
