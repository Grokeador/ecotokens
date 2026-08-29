"""Console dal vivo: cosa sta facendo il gateway adesso, e quanto costa.

E' la seconda pagina del progetto, e va tenuta distinta dalla prima. La
dashboard (`ecotokens dashboard`) misura un **corpus finto** eseguito due
volte, con e senza gli stadi: risponde a "quanto risparmierebbe". Questa
pagina legge il **traffico vero** gia' passato di qui: risponde a "quanto ha
risparmiato". Confonderle sarebbe il tipico difetto del metro - un numero
plausibile ottenuto rispondendo a un'altra domanda.

Tre scelte che vale la pena spiegare.

**Il rendering sta nel browser, non qui.** Il server produce solo JSON
(`/admin/live`); la pagina lo disegna. Cosi' esiste un unico posto in cui una
misura diventa un pixel, e non c'e' modo che la versione che si aggiorna da
sola e quella del primo caricamento raccontino cose diverse.

**Nessuna richiesta di rete.** Nessun font remoto, nessun CDN: un gateway
locale che apre una connessione verso l'esterno per mostrare una tabella
tradirebbe il motivo per cui e' locale. Vale anche quando la connessione
c'e' - una pagina che mostra il traffico dell'utente non lo racconta a
nessun altro.

**Ogni avviso e' un conteggio.** Nessuna frase della pagina dice che qualcosa
va male sulla base di un'euristica: dice quante volte una cosa e' successa, e
il lettore decide. La sezione finale elenca cio' che questa pagina **non** sa
misurare, che e' la parte piu' importante e quella che si e' piu' tentati di
lasciar fuori.
"""

from __future__ import annotations

import time
from typing import Any

from . import __version__

# --- raccolta -------------------------------------------------------------


async def build_console_data(gateway: Any) -> dict[str, Any]:
    """Tutto cio' che la console mostra, in un solo giro di interrogazioni."""
    store = gateway.store
    settings = gateway.settings

    stats = await store.stats()
    oggi, mese = await store.current_spend()
    stadi = await store.stage_activity()
    scritture = await store.cache_write_report()
    taratura = await store.estimate_calibration()
    latenza = await store.latency_report()
    overhead = await store.overhead_report()
    recenti = await store.recent_events(25)
    sessioni = await store.list_sessions(10)

    baseline = float(stats.get("baseline_cost_usd") or 0)
    costo = float(stats.get("cost_usd") or 0)
    richieste = int(stats.get("requests") or 0)

    dati: dict[str, Any] = {
        "generated_at": time.time(),
        "version": __version__,
        "requests": richieste,
        "totals": {
            "cost_usd": costo,
            "baseline_cost_usd": baseline,
            "saved_usd": float(stats.get("saved_usd") or 0),
            "saved_ratio": (baseline - costo) / baseline if baseline else 0.0,
            "prompt_tokens": int(stats.get("total_prompt_tokens") or 0),
            "input_tokens": int(stats.get("input_tokens") or 0),
            "cache_creation_tokens": int(stats.get("cache_creation_tokens") or 0),
            "cache_read_tokens": int(stats.get("cache_read_tokens") or 0),
            "output_tokens": int(stats.get("output_tokens") or 0),
            "cache_hit_ratio": float(stats.get("cache_hit_ratio") or 0),
        },
        "spend": {
            "today_usd": oggi,
            "month_usd": mese,
            "daily_limit": settings.budget.daily_usd,
            "monthly_limit": settings.budget.monthly_usd,
            "enabled": settings.budget.enabled,
        },
        "by_source": stats.get("by_source", []),
        "by_model": stats.get("by_model", []),
        "by_day": stats.get("by_day", []),
        "stages": stadi,
        "cache_writes": scritture,
        "calibration": taratura,
        "latency": latenza,
        "overhead": overhead,
        "recent": recenti,
        "sessions": sessioni,
        "profile": settings.profilo,
        "config": _config_stadi(gateway),
        "faults": _guasti(gateway),
        "not_measured": NON_MISURATO,
    }
    dati["alerts"] = _avvisi(dati)
    return dati


def _guasti(gateway: Any) -> list[dict[str, Any]]:
    """Gli stadi che si sono rotti, e quante volte.

    Vive nel processo, non nel database: sparisce a ogni riavvio, ed e'
    giusto cosi'. Un guasto e' un fatto di **questa** esecuzione, e un elenco
    che sopravvive al riavvio direbbe "rotto" di uno stadio che nel frattempo
    e' stato corretto.
    """
    voci = []
    for nome, voce in sorted(gateway.pipeline.guasti.items()):
        voci.append(
            {
                "stage": nome,
                "count": voce["conteggio"],
                "consecutive": voce["consecutivi"],
                "disabled": voce["spento"],
                "last": voce["ultimo"],
                "where": voce["dove"],
            }
        )
    return voci


def _config_stadi(gateway: Any) -> list[dict[str, Any]]:
    """Stadi montati nella pipeline e loro stato, letti dalla pipeline vera.

    Non da `settings`: fra la configurazione e cio' che gira c'e' spazio per
    una discrepanza, ed e' esattamente quella che una console dovrebbe far
    vedere invece di ripetere l'intenzione.
    """
    sezioni = {
        "memory": gateway.settings.memory,
        "semantic_cache": gateway.settings.semantic_cache,
        "budget": gateway.settings.budget,
    }
    voci = []
    for stadio in gateway.pipeline.stages:
        acceso = bool(getattr(stadio, "enabled", True))
        # Il motivo lo dichiara la configurazione, che e' anche il posto dove
        # si decide. Ripeterlo qui vorrebbe dire tenerne due copie, e una
        # delle due invecchierebbe: e' successo alla didascalia della
        # dashboard, che diceva "spenti" mentre la sua tabella diceva "attivo".
        guasto = gateway.pipeline.guasti.get(stadio.name)
        if not acceso and guasto and guasto["spento"]:
            # Distinzione che vale l'intera riga: uno stadio spento da un bug
            # e uno spento per scelta appaiono identici, e confonderli manda a
            # cercare il risparmio mancante nel posto sbagliato.
            motivo = (
                f"disattivato dal gateway dopo {guasto['consecutivi']} guasti "
                f"consecutivi in {guasto['dove']}"
            )
        else:
            motivo = "" if acceso else getattr(sezioni.get(stadio.name), "motivo_se_spenta", "")
        voci.append({"name": stadio.name, "enabled": acceso, "reason": motivo})
    return voci


# --- avvisi: conteggi, non giudizi ---------------------------------------


def _conta_note(stadi: list[dict[str, Any]], stadio: str, frammento: str) -> int:
    """Quante volte uno stadio ha scritto una nota che contiene un frammento."""
    for voce in stadi:
        if voce["stage"] != stadio:
            continue
        return sum(n for nota, n in voce.get("notes", []) if frammento in nota)
    return 0


def _avvisi(dati: dict[str, Any]) -> list[dict[str, Any]]:
    """Le cose che la pagina ha contato e che meritano di essere lette per prime.

    Ogni voce porta il proprio numero. Un avviso senza numero è un'opinione
    con l'aria di una misura, ed è il modo più rapido di rendere una console
    ignorabile: chi la legge impara che i suoi allarmi non significano niente.
    """
    avvisi: list[dict[str, Any]] = []
    stadi = dati["stages"]
    richieste = dati["requests"]

    # 0. I guasti interni vengono prima di tutto, e prima anche del controllo
    #    sulle richieste: uno stadio che si rompe sulla prima richiesta della
    #    giornata e' esattamente il caso in cui nessun conteggio e' ancora
    #    maturato, ed e' quando serve saperlo.
    guasti = dati.get("faults") or []
    if guasti:
        spenti = [g["stage"] for g in guasti if g["disabled"]]
        totale = sum(g["count"] for g in guasti)
        avvisi.append(
            {
                "level": "bad" if spenti else "warn",
                "count": totale,
                "title": (
                    f"{totale} guasti interni in {len(guasti)} stadi"
                    + (f", {len(spenti)} disattivati" if spenti else "")
                ),
                "body": (
                    "La richiesta e' stata servita lo stesso: uno stadio che si rompe "
                    "viene annullato e la catena prosegue. Ma un'ottimizzazione che non "
                    "gira non risparmia, e il risparmio mancante va cercato qui prima "
                    "che altrove. "
                    + (
                        "Disattivati dopo guasti ripetuti: " + ", ".join(spenti) + ". "
                        "Tornano attivi al riavvio, o dalle impostazioni."
                        if spenti
                        else "Ultimo: " + guasti[0]["last"]
                    )
                ),
            }
        )

    if not richieste:
        return avvisi

    # 1. Il declassamento del modello puo' spegnere la cache, in silenzio.
    #    Le soglie minime non sono monotone: Opus 5 ne chiede 512, Haiku 4.5
    #    ne chiede 4096. Un prompt da mille token che era memorizzabile smette
    #    di esserlo, e l'API non lo segnala in nessun modo.
    sotto_soglia = _conta_note(stadi, "cache_planner", "sotto la soglia")
    if sotto_soglia:
        declassate = _conta_note(stadi, "router", "richiede almeno")
        avvisi.append(
            {
                "level": "warn",
                "count": sotto_soglia,
                "title": f"{sotto_soglia} richieste senza alcun breakpoint di cache",
                "body": (
                    "Il prompt era sotto la soglia minima del modello, e sotto soglia "
                    "la cache non si crea senza che l’API emetta alcun errore: "
                    "l’unico segno è questa riga. "
                    + (
                        f"Su {declassate} richieste il router ha segnalato di aver "
                        "alzato la soglia declassando il modello — i due conteggi "
                        "guardano cose diverse e non vanno sottratti. Il risparmio del "
                        "modello economico e quello della cache non si sommano: si "
                        "escludono. Quale dei due convenga dipende da quante volte il "
                        "prefisso sarebbe stato riletto."
                        if declassate
                        else ""
                    )
                ),
            }
        )

    # 2. Scritture pagate 1,25x e mai rilette.
    scritture = dati["cache_writes"]
    sprecati = int(scritture.get("token_sprecati_in_mezzo") or 0)
    if sprecati:
        avvisi.append(
            {
                "level": "warn",
                "count": sprecati,
                "title": f"{sprecati:,} token scritti in cache e mai riletti".replace(",", " "),
                "body": (
                    "Sono scritture invalidate da una successiva, non code di "
                    "conversazione: quelle sono strutturali e stanno contate a parte. "
                    "Il sovrapprezzo pagato è di "
                    f"${float(scritture.get('costo_sprecato_usd') or 0):.4f}."
                ),
            }
        )

    # 3. Richieste costate piu' della baseline.
    sopra = _conta_note(stadi, "ledger", "costo superiore alla baseline")
    if sopra:
        avvisi.append(
            {
                "level": "bad",
                "count": sopra,
                "title": f"{sopra} richieste sono costate più che senza gateway",
                "body": (
                    "Succede quando si paga una scrittura di cache che nessuna "
                    "richiesta successiva ha ancora riletto. Su una conversazione che "
                    "prosegue si ripaga al turno dopo; su una richiesta isolata no."
                ),
            }
        )

    # 4. Stadi accesi che non hanno mai fatto niente.
    muti = [voce["stage"] for voce in stadi if voce["acted_in"] == 0]
    if muti:
        avvisi.append(
            {
                "level": "idle",
                "count": len(muti),
                "title": "Stadi accesi che non sono mai intervenuti: " + ", ".join(muti),
                "body": (
                    "Non è di per sé un difetto - uno stadio può non avere avuto "
                    "occasione. È la domanda da fare prima di raffinarne l'euristica: "
                    "l'effort adattivo è stato migliorato per mesi mentre un veto lo "
                    "spegneva sul 45% del traffico, e nessuno lo stava contando."
                ),
            }
        )

    # 5. Il tetto di spesa, se acceso.
    spesa = dati["spend"]
    if spesa["enabled"] and spesa["daily_limit"]:
        quota = spesa["today_usd"] / spesa["daily_limit"]
        if quota >= 0.8:
            avvisi.append(
                {
                    "level": "bad" if quota >= 1 else "warn",
                    "count": round(quota * 100),
                    "title": f"Tetto giornaliero al {quota:.0%}",
                    "body": (
                        f"${spesa['today_usd']:.4f} su ${spesa['daily_limit']:.2f}. "
                        "Al superamento le richieste vengono rifiutate prima di "
                        "raggiungere l'API."
                    ),
                }
            )

    # 6. Lo stimatore locale che sbaglia troppo.
    for riga in dati["calibration"]:
        errore = abs(float(riga.get("scarto_medio") or 0))
        if int(riga.get("campioni") or 0) >= 5 and errore >= 0.15:
            avvisi.append(
                {
                    "level": "warn",
                    "count": riga["campioni"],
                    "title": (
                        f"Lo stimatore sbaglia del {errore:.0%} su {riga['model']}"
                    ),
                    "body": (
                        "È il conteggio usato dal preventivo del budget e dalla "
                        "soglia di cache. Sbagliando per difetto si marca un prefisso "
                        "che non verrà memorizzato; per eccesso si rinuncia a uno che "
                        "lo sarebbe stato."
                    ),
                }
            )

    return avvisi


# Cio' che questa pagina non sa dire. Sta nel codice e non nel testo HTML
# perche' e' contenuto, non decorazione: si aggiorna quando cambia il gateway.
NON_MISURATO: list[dict[str, str]] = [
    {
        "title": "Nessun numero qui sopra viene dall'API vera",
        "body": (
            "Le misure del progetto girano contro un simulatore: i test non devono "
            "richiedere rete, e un banco che chiama l'API costa a ogni esecuzione. Il "
            "prezzo è che ogni percentuale vale *se* le assunzioni sul comportamento "
            "dell'API sono giuste. Sono elencate una per una — `ecotokens assunzioni` "
            "— con, per ognuna, cosa risulterebbe diverso se fosse sbagliata. I numeri "
            "di questa pagina invece vengono dal traffico vero passato di qui."
        ),
    },
    {
        "title": "Se la risposta era giusta",
        "body": (
            "Tutto quello che sta sopra misura il prezzo di una risposta, non il suo "
            "valore. Con il profilo aggressivo il modello e l'effort cambiano: una "
            "parte del risparmio è un'altra risposta a un prezzo diverso, e quale "
            "delle due frasi conti di più non lo decide questa pagina."
        ),
    },
    {
        "title": "Il risparmio più grande, che non compare",
        "body": (
            "I token mai chiesti non lasciano traccia da nessuna parte. Un ciclo "
            "agentico che fa sei chiamate dove ne bastava una costa sei volte tanto, "
            "e qui si vede solo come sei righe legittime. Il numero di turni è la "
            "leva più grossa di tutte e la sola che il gateway non può tirare."
        ),
    },
    {
        "title": "Cosa è costato potare",
        "body": (
            "La potatura del contesto e il riassunto della cronologia tolgono token "
            "dal prompt, e questo si conta. Quello che si perde - un dettaglio che "
            "sarebbe servito tre turni dopo - non si conta, e non è zero."
        ),
    },
    {
        "title": "Il traffico che non passa di qui",
        "body": (
            "Solo le richieste instradate attraverso il gateway compaiono. Un client "
            "rimasto puntato direttamente su api.anthropic.com continua a spendere "
            "senza che questa pagina lo sappia, e il totale sembrerà ottimo."
        ),
    },
]


# --- pagina ---------------------------------------------------------------


def render_console() -> str:
    """La pagina: struttura, stile e disegno. I dati arrivano da /admin/live."""
    return (
        "<!doctype html>\n<html lang=\"it\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>EcoTokens - console</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n{_CORPO}\n"
        f"<script>{_JS}</script>\n</body>\n</html>\n"
    )


_CSS = """
:root {
  --ground: #eef1f2; --surface: #fbfcfc; --surface-sunken: #e6ebec;
  --ink: #12191b; --ink-soft: #58666b; --ink-faint: #8b989c;
  --rule: #d5dcde; --accent: #15616d; --accent-soft: #d7e6e8;
  --good: #1b7a4b; --good-soft: #cfe6da; --bad: #a33a24; --bad-soft: #f0d9d2;
  --warn: #b07d2b; --warn-soft: #f2e5cb; --idle: #9aa6aa;
  --shadow: 0 1px 2px rgba(18,25,27,.06), 0 8px 24px -16px rgba(18,25,27,.28);
  --radius: 10px;
  --font-body: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-mono: ui-monospace, "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0d1214; --surface: #161d20; --surface-sunken: #101619;
    --ink: #e6ebec; --ink-soft: #9dabaf; --ink-faint: #6d7c81;
    --rule: #253034; --accent: #55b2c1; --accent-soft: #17323a;
    --good: #4bb582; --good-soft: #16332a; --bad: #dd8368; --bad-soft: #38211b;
    --warn: #d3a55a; --warn-soft: #33280f; --idle: #5d6a6e;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 10px 30px -18px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"] {
  --ground: #0d1214; --surface: #161d20; --surface-sunken: #101619;
  --ink: #e6ebec; --ink-soft: #9dabaf; --ink-faint: #6d7c81;
  --rule: #253034; --accent: #55b2c1; --accent-soft: #17323a;
  --good: #4bb582; --good-soft: #16332a; --bad: #dd8368; --bad-soft: #38211b;
  --warn: #d3a55a; --warn-soft: #33280f; --idle: #5d6a6e;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: var(--font-body); font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.page {
  max-width: 1140px; margin: 0 auto;
  padding: clamp(1.25rem, 4vw, 2.5rem) clamp(.75rem, 3vw, 1.75rem) 4rem;
  display: flex; flex-direction: column; gap: clamp(1.1rem, 2.5vw, 1.75rem);
}
.mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.muted { color: var(--ink-faint); }
h1, h2, h3 { margin: 0; text-wrap: balance; }
h1 { font-size: clamp(1.5rem, 4vw, 2rem); font-weight: 600; letter-spacing: -.015em; }
h2 { font-size: 1.15rem; font-weight: 600; }
h3 { font-size: .95rem; font-weight: 600; }
p { margin: 0; }
a { color: var(--accent); }

/* --- testata --- */
.masthead { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 1rem; justify-content: space-between; }
.masthead .lede { max-width: 58ch; color: var(--ink-soft); font-size: .92rem; }
.eyebrow {
  font-size: .7rem; text-transform: uppercase; letter-spacing: .14em;
  color: var(--accent); font-weight: 700;
}
.controls { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
button {
  font: inherit; font-size: .82rem; color: var(--ink); cursor: pointer;
  background: var(--surface); border: 1px solid var(--rule);
  border-radius: 999px; padding: .3rem .85rem;
}
button:hover { border-color: var(--accent); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.pulse {
  display: inline-flex; align-items: center; gap: .4rem; font-size: .78rem;
  color: var(--ink-faint);
}
.pulse .dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--good);
  animation: respiro 2s ease-in-out infinite;
}
.pulse.ferma .dot { background: var(--idle); animation: none; }
@keyframes respiro { 0%, 100% { opacity: 1 } 50% { opacity: .25 } }

/* --- pannelli --- */
.panel {
  background: var(--surface); border: 1px solid var(--rule);
  border-radius: var(--radius); padding: clamp(1rem, 2.5vw, 1.4rem);
  display: flex; flex-direction: column; gap: 1rem; box-shadow: var(--shadow);
}
.panel-quiet { box-shadow: none; background: var(--surface-sunken); }
.panel-head { display: flex; flex-direction: column; gap: .35rem; }
.panel-head p { max-width: 70ch; color: var(--ink-soft); font-size: .86rem; }

/* --- statistiche --- */
.verdict { background: var(--rule); display: grid; gap: 1px; border-radius: var(--radius); overflow: hidden; grid-template-columns: repeat(4, 1fr); }
.stat { background: var(--surface); padding: .85rem 1rem; display: flex; flex-direction: column; gap: .1rem; }
.stat .label { font-size: .68rem; text-transform: uppercase; letter-spacing: .1em; color: var(--ink-faint); font-weight: 700; }
.stat .value { font-size: 1.25rem; font-weight: 500; letter-spacing: -.02em; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.stat .detail { font-size: .75rem; color: var(--ink-faint); }
.stat.big .value { color: var(--good); font-size: 1.6rem; }

/* --- barra dei token --- */
.bar { display: flex; height: 26px; border-radius: 6px; overflow: hidden; background: var(--surface-sunken); border: 1px solid var(--rule); }
.seg { height: 100%; }
.seg-full { background: var(--bad); }
.seg-write { background: var(--warn); }
.seg-read { background: var(--good); }
.legend { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: .35rem 1.25rem; font-size: .8rem; }
.legend li { display: flex; align-items: center; gap: .45rem; }
.swatch { width: 10px; height: 10px; border-radius: 3px; }

/* --- stadi --- */
.stages { display: flex; flex-direction: column; gap: .55rem; }
.stage-row { display: grid; grid-template-columns: 9.5rem 1fr 7.5rem; align-items: center; gap: .75rem; }
.stage-name { font-size: .85rem; font-weight: 600; font-family: var(--font-mono); }
.stage-track { background: var(--surface-sunken); border-radius: 5px; height: 20px; overflow: hidden; border: 1px solid var(--rule); }
.stage-fill { height: 100%; background: var(--accent); }
.stage-fill.zero { background: var(--idle); }
.stage-num { font-size: .78rem; color: var(--ink-soft); text-align: right; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.stage-note { grid-column: 2 / -1; font-size: .76rem; color: var(--ink-faint); margin-top: -.35rem; }
.stage-row.conf { grid-template-columns: 9.5rem 1fr 4.5rem; align-items: baseline; }
.conf-note { grid-column: 2; margin-top: 0; }
.stage-num.acceso { color: var(--good); }
.stage-num.spento { color: var(--ink-faint); }

/* --- avvisi --- */
.alerts { display: flex; flex-direction: column; gap: .6rem; }
.alert { border-left: 3px solid var(--rule); padding: .55rem .85rem; border-radius: 0 6px 6px 0; background: var(--surface-sunken); }
.alert.warn { border-color: var(--warn); background: var(--warn-soft); }
.alert.bad { border-color: var(--bad); background: var(--bad-soft); }
.alert.idle { border-color: var(--idle); }
.alert h3 { font-size: .88rem; }
.alert p { font-size: .8rem; color: var(--ink-soft); margin-top: .15rem; max-width: 72ch; }

/* --- tabelle --- */
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .82rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { font-size: .7rem; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-faint); font-weight: 700; }
td.num { text-align: right; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
tbody tr:last-child td { border-bottom: none; }

/* --- feed --- */
.feed { display: flex; flex-direction: column; gap: .4rem; }
details.evento { border: 1px solid var(--rule); border-radius: 7px; background: var(--surface-sunken); }
details.evento > summary {
  cursor: pointer; padding: .45rem .7rem; display: grid; align-items: center; gap: .6rem;
  grid-template-columns: 4.5rem 1fr auto auto; font-size: .8rem;
}
details.evento > summary::-webkit-details-marker { display: none; }
.tag { font-size: .68rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; padding: .1rem .45rem; border-radius: 999px; text-align: center; }
.tag.api { background: var(--accent-soft); color: var(--accent); }
.tag.exact_cache, .tag.semantic_cache { background: var(--good-soft); color: var(--good); }
.evento-corpo { padding: .1rem .7rem .7rem; display: flex; flex-direction: column; gap: .45rem; }
.evento-stadio { font-size: .78rem; }
.evento-stadio b { font-family: var(--font-mono); font-size: .74rem; color: var(--accent); }
.evento-stadio ul { margin: .1rem 0 0; padding-left: 1.1rem; color: var(--ink-soft); }

/* --- vuoto --- */
.empty { display: flex; flex-direction: column; gap: .75rem; }
.empty pre {
  margin: 0; padding: .7rem .9rem; background: var(--surface-sunken); border: 1px solid var(--rule);
  border-radius: 7px; font-family: var(--font-mono); font-size: .78rem; overflow-x: auto;
}
.non-misurato { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: 1rem; }
.non-misurato h3 { color: var(--ink); }
.non-misurato p { font-size: .8rem; color: var(--ink-soft); margin-top: .2rem; }
.colophon { border-top: 1px solid var(--rule); padding-top: 1rem; font-size: .8rem; color: var(--ink-faint); display: flex; flex-direction: column; gap: .3rem; }
@media (max-width: 860px) {
  .verdict { grid-template-columns: repeat(2, 1fr); }
  .stage-row { grid-template-columns: 7rem 1fr 5.5rem; }
  details.evento > summary { grid-template-columns: 4rem 1fr; }
}
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
"""


_CORPO = """<div class="page">
  <header class="masthead">
    <div>
      <p class="eyebrow">EcoTokens · traffico vero</p>
      <h1>Console</h1>
      <p class="lede">Le richieste passate davvero da questo gateway, non un carico
      simulato. Per il confronto con e senza gli stadi c'è
      <a href="/admin/dashboard">il banco di misura</a>.</p>
    </div>
    <div class="controls">
      <span class="pulse" id="pulse"><span class="dot"></span><span id="pulse-testo">in ascolto</span></span>
      <button id="pausa" type="button">Ferma</button>
      <button id="tema" type="button">Tema</button>
    </div>
  </header>
  <div id="contenuto"></div>
  <footer class="colophon">
    <p>Aggiornata da sola ogni 5 secondi leggendo <span class="mono">/admin/live</span>.
    La pagina non fa nessuna richiesta fuori da questo gateway.</p>
    <p class="mono" id="orario"></p>
  </footer>
</div>"""


_JS = r"""
(function () {
  "use strict";
  var INTERVALLO = 5000;
  var attiva = true;
  var aperti = {};   // quali righe del feed erano espanse, per non richiuderle

  // --- formattazione ------------------------------------------------------
  function usd(v) {
    v = Number(v) || 0;
    if (v !== 0 && Math.abs(v) < 0.01) return "$" + v.toFixed(6);
    return "$" + v.toFixed(4);
  }
  function pct(v) { return ((Number(v) || 0) * 100).toFixed(1) + "%"; }
  function intero(v) {
    return (Math.round(Number(v) || 0)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }
  function segno(v) {
    v = (Number(v) || 0) * 100;
    return (v > 0 ? "+" : "") + v.toFixed(1) + "%";
  }
  function ms(v) {
    v = Number(v) || 0;
    return v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms";
  }
  function ora(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleTimeString("it-IT", { hour12: false });
  }
  function origine() {
    // L'indirizzo vero, non un esempio: chi serve il gateway su un'altra
    // porta copierebbe altrimenti un comando che non funziona.
    return window.location.origin;
  }
  function esc(t) {
    var d = document.createElement("div");
    d.textContent = t == null ? "" : String(t);
    return d.innerHTML;
  }

  function stat(label, valore, dettaglio, grande) {
    return '<div class="stat' + (grande ? " big" : "") + '">' +
      '<span class="label">' + esc(label) + "</span>" +
      '<span class="value">' + esc(valore) + "</span>" +
      '<span class="detail">' + esc(dettaglio || "") + "</span></div>";
  }
  function pannello(titolo, sottotitolo, corpo, quieto) {
    return '<section class="panel' + (quieto ? " panel-quiet" : "") + '">' +
      '<div class="panel-head"><h2>' + esc(titolo) + "</h2>" +
      (sottotitolo ? "<p>" + sottotitolo + "</p>" : "") + "</div>" + corpo + "</section>";
  }

  // --- sezioni ------------------------------------------------------------

  function vuoto() {
    return pannello(
      "Nessuna richiesta registrata",
      "La console non inventa numeri: finché non passa traffico non c'è niente da misurare. " +
      "Le stime a carico simulato stanno nel <a href=\"/admin/dashboard\">banco di misura</a>.",
      '<div class="empty"><p>Manda una richiesta a questo gateway: i numeri compaiono ' +
      "da soli, la pagina si aggiorna ogni cinque secondi.</p>" +
      "<pre>curl " + origine() + "/v1/chat/completions \\\n" +
      "  -H 'Content-Type: application/json' \\\n" +
      "  -d '{\"model\":\"claude-opus-5\",\"messages\":[{\"role\":\"user\",\"content\":\"ciao\"}]}'</pre>" +
      "<p class=\"muted\">Con l'SDK OpenAI basta <span class=\"mono\">base_url=\"" +
      origine() + "/v1\"</span>.</p></div>"
    );
  }

  function verdetto(d) {
    var t = d.totals, s = d.spend;
    var tetto = s.enabled && s.daily_limit
      ? "su $" + Number(s.daily_limit).toFixed(2) + " di tetto"
      : "nessun tetto impostato";
    return '<div class="verdict">' +
      stat("Risparmio", pct(t.saved_ratio), usd(t.saved_usd) + " su " + intero(d.requests) + " richieste", true) +
      stat("Costo reale", usd(t.cost_usd), "quello che è stato speso") +
      stat("Senza gateway", usd(t.baseline_cost_usd), "stesso traffico a prezzo pieno") +
      stat("Prompt da cache", pct(t.cache_hit_ratio), intero(t.cache_read_tokens) + " token a 0,1x") +
      stat("Spesa di oggi", usd(s.today_usd), tetto) +
      stat("Nel mese", usd(s.month_usd), "profilo: " + d.profile) +
      stat("Token di prompt", intero(t.prompt_tokens), intero(t.output_tokens) + " generati") +
      stat("Overhead del gateway", intero(d.overhead.overhead_tokens) + " tok",
           usd(d.overhead.aux_cost_usd) + " di chiamate interne") +
      "</div>";
  }

  function flusso(d) {
    var t = d.totals;
    var tot = t.prompt_tokens || 1;
    var parti = [
      ["seg-full", t.input_tokens, "prezzo pieno", "1x"],
      ["seg-write", t.cache_creation_tokens, "scritti in cache", "1,25x / 2x"],
      ["seg-read", t.cache_read_tokens, "riletti dalla cache", "0,1x"]
    ];
    var barra = '<div class="bar">' + parti.map(function (p) {
      return '<div class="seg ' + p[0] + '" style="width:' + (p[1] / tot * 100) + '%"></div>';
    }).join("") + "</div>";
    var voci = '<ul class="legend">' + parti.map(function (p) {
      return "<li><span class=\"swatch " + p[0] + "\" style=\"background:var(--" +
        (p[0] === "seg-full" ? "bad" : p[0] === "seg-write" ? "warn" : "good") + ")\"></span>" +
        esc(p[2]) + ' <span class="mono">' + intero(p[1]) + "</span> " +
        '<span class="muted mono">' + p[3] + "</span></li>";
    }).join("") + "</ul>";
    return pannello("Dove finiscono i token di prompt",
      "La quantità non cambia: cambia la tariffa a cui viene pagata. Una rilettura costa un decimo, " +
      "una scrittura un quarto in più — ed è per questo che scrivere in cache qualcosa che " +
      "nessuno rileggerà fa perdere soldi.",
      barra + voci);
  }

  function stadi(d) {
    if (!d.stages.length) return "";
    var considerate = d.stages[0].requests_considered;
    var righe = d.stages.map(function (s) {
      var zero = s.acted_in === 0;
      var nota = s.notes && s.notes.length ? s.notes[0][0] : "";
      return '<div class="stage-row">' +
        '<span class="stage-name">' + esc(s.stage) + "</span>" +
        '<div class="stage-track"><div class="stage-fill' + (zero ? " zero" : "") +
        '" style="width:' + (s.ratio * 100) + '%"></div></div>' +
        '<span class="stage-num">' + s.acted_in + " / " + s.enabled_in + "</span>" +
        "</div>" +
        (nota ? '<div class="stage-note">' + esc(nota) + "</div>"
              : '<div class="stage-note">mai intervenuto</div>');
    }).join("");
    return pannello("Quante volte ogni stadio ha fatto qualcosa",
      "Il conteggio è su " + intero(considerate) + " richieste in cui lo stadio era acceso. " +
      "È la domanda da fare <em>prima</em> di raffinare uno stadio: l'effort adattivo è stato " +
      "migliorato per mesi mentre un veto lo spegneva sul 45% del traffico. Uno stadio che agisce senza " +
      "lasciare una nota risulta inattivo — distorsione nota, preferita a contare tutto sempre.",
      '<div class="stages">' + righe + "</div>");
  }

  function avvisi(d) {
    if (!d.alerts.length) {
      return pannello("Niente da segnalare",
        "Nessuna scrittura di cache sprecata, nessuna richiesta sopra la baseline, nessuno stadio muto. " +
        "Vale per il traffico registrato finora, che è poco o molto a seconda di quanto è girato.",
        "", true);
    }
    var corpo = '<div class="alerts">' + d.alerts.map(function (a) {
      return '<div class="alert ' + esc(a.level) + '"><h3>' + esc(a.title) + "</h3>" +
        "<p>" + esc(a.body) + "</p></div>";
    }).join("") + "</div>";
    return pannello("Cosa è stato contato", "Ogni riga porta il proprio numero: sono conteggi, non giudizi.", corpo);
  }

  function scritture(d) {
    var c = d.cache_writes;
    if (!c || !c.scritture) return "";
    var corpo = '<div class="verdict">' +
      stat("Scritture", intero(c.scritture), intero(c.token_scritti) + " token marcati") +
      stat("Riletti", intero(c.token_recuperati), "hanno pagato la scrittura") +
      stat("Sprecati, evitabili", intero(c.token_sprecati_in_mezzo),
           "invalidati da una scrittura dopo") +
      stat("Sprecati, strutturali", intero(c.token_sprecati_di_coda),
           "ultima scrittura di una sessione") +
      stat("Quota sprecata", pct(c.quota_sprecata), "sul totale scritto") +
      stat("Sovrapprezzo evitabile", usd(c.costo_sprecato_in_mezzo_usd),
           "solo il di più rispetto a 1x") +
      "</div>";
    return pannello("Le scritture che nessuno rilegge",
      "Una scrittura in coda alla conversazione è strutturale: l'ultima di ogni sessione non può " +
      "essere riletta da nessuno. Una scrittura invalidata da quella dopo, invece, si poteva evitare.",
      corpo);
  }

  function tabella(titolo, sottotitolo, intestazioni, righe, quieto) {
    if (!righe.length) return "";
    var head = "<tr>" + intestazioni.map(function (h) {
      return "<th" + (h[1] ? ' class="num"' : "") + ">" + esc(h[0]) + "</th>";
    }).join("") + "</tr>";
    var corpo = righe.map(function (r) {
      return "<tr>" + r.map(function (c, i) {
        return "<td" + (intestazioni[i][1] ? ' class="num"' : "") + ">" + esc(c) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    return pannello(titolo, sottotitolo,
      '<div class="scroll"><table><thead>' + head + "</thead><tbody>" + corpo + "</tbody></table></div>", quieto);
  }

  function latenza(d) {
    return tabella("Quanto si aspetta",
      "La mediana, non la media: una sola richiesta lenta sposta la media e non sposta l'esperienza. " +
      "È l'altra faccia del risparmio — un hit di cache non costa token e non fa aspettare.",
      [["Provenienza", false], ["Richieste", true], ["Mediana", true], ["95° percentile", true]],
      d.latency.map(function (l) { return [l.source, intero(l.requests), ms(l.median_ms), ms(l.p95_ms)]; }));
  }

  function modelli(d) {
    return tabella("Per modello",
      "Con il declassamento acceso, il modello che compare qui non è sempre quello chiesto dal client.",
      [["Modello", false], ["Richieste", true], ["Costo", true], ["Risparmio", true]],
      d.by_model.map(function (m) { return [m.model, intero(m.requests), usd(m.cost_usd), usd(m.saved_usd)]; }));
  }

  function taratura(d) {
    if (!d.calibration.length) return "";
    return tabella("Quanto sbaglia lo stimatore locale",
      "Il conteggio esatto costa un round-trip, quindi il gateway stima. I campioni arrivano dalle chiamate " +
      "a <span class=\"mono\">count_tokens</span> già pagate per altro: è la sola misura del progetto " +
      "che non costa niente ottenere.",
      [["Modello", false], ["Campioni", true], ["Scarto medio", true],
       ["Minimo", true], ["Massimo", true]],
      d.calibration.map(function (c) {
        return [c.model, intero(c.campioni), segno(c.scarto_medio),
                segno(c.scarto_min), segno(c.scarto_max)];
      }), true);
  }

  function feed(d) {
    if (!d.recent.length) return "";
    var righe = d.recent.map(function (e) {
      var acted = (e.stages && e.stages.acted) || {};
      var nomi = Object.keys(acted);
      var dettaglio = nomi.length
        ? nomi.map(function (n) {
            return '<div class="evento-stadio"><b>' + esc(n) + "</b><ul>" +
              acted[n].map(function (nota) { return "<li>" + esc(nota) + "</li>"; }).join("") +
              "</ul></div>";
          }).join("")
        : '<p class="muted">Nessuno stadio ha lasciato una nota su questa richiesta.</p>';
      var chiave = "e" + e.id;
      return '<details class="evento" data-k="' + chiave + '"' + (aperti[chiave] ? " open" : "") + ">" +
        "<summary>" +
        '<span class="tag ' + esc(e.source) + '">' + esc(e.source.replace("_", " ")) + "</span>" +
        "<span>" + esc(e.model) + ' <span class="muted mono">' + intero(e.prompt_tokens) + " tok" +
        (e.cache_read_tokens ? " · " + intero(e.cache_read_tokens) + " da cache" : "") + "</span></span>" +
        '<span class="mono">' + usd(e.cost_usd) + "</span>" +
        '<span class="mono muted">' + ora(e.ts) + "</span>" +
        "</summary><div class=\"evento-corpo\">" + dettaglio + "</div></details>";
    }).join("");
    return pannello("Le ultime richieste",
      "Ogni riga si apre su cosa ha fatto ciascuno stadio a quella richiesta. È il livello a cui si " +
      "vede perché un numero aggregato è quello che è.",
      '<div class="feed">' + righe + "</div>");
  }

  function configurazione(d) {
    if (!d.config || !d.config.length) return "";
    var righe = d.config.map(function (s) {
      return '<div class="stage-row conf">' +
        '<span class="stage-name">' + esc(s.name) + "</span>" +
        '<span class="stage-note conf-note">' + esc(s.reason || "") + "</span>" +
        '<span class="stage-num ' + (s.enabled ? "acceso" : "spento") + '">' +
        (s.enabled ? "attivo" : "spento") + "</span></div>";
    }).join("");
    return pannello("Gli stadi montati adesso",
      "Letti dalla pipeline che ha servito le richieste, non dal file di configurazione: " +
      "fra ciò che si è scritto e ciò che gira c'è spazio per una differenza, ed è quella " +
      "che una console dovrebbe far vedere. Dove uno stadio è spento per scelta, accanto " +
      "c'è il perché.",
      '<div class="stages">' + righe + "</div>", true);
  }

  function nonMisurato(d) {
    var corpo = '<div class="non-misurato">' + d.not_measured.map(function (v) {
      return "<div><h3>" + esc(v.title) + "</h3><p>" + esc(v.body) + "</p></div>";
    }).join("") + "</div>";
    return pannello("Cosa questa pagina non misura",
      "La regola del progetto è che un risparmio non si dichiara, si misura. Il rovescio è dire " +
      "anche dove la misura non arriva, altrimenti il totale si legge come se coprisse tutto.",
      corpo, true);
  }

  // --- disegno ------------------------------------------------------------
  function disegna(d) {
    var nodo = document.getElementById("contenuto");
    // Ricorda quali righe del feed erano aperte: il ridisegno non deve
    // richiudere quella che si sta leggendo.
    aperti = {};
    Array.prototype.forEach.call(nodo.querySelectorAll("details.evento[open]"), function (el) {
      aperti[el.getAttribute("data-k")] = true;
    });
    if (!d.requests) {
      nodo.innerHTML = vuoto() + configurazione(d) + nonMisurato(d);
    } else {
      nodo.innerHTML = [
        verdetto(d), avvisi(d), flusso(d), stadi(d), scritture(d),
        latenza(d), modelli(d), taratura(d), feed(d), configurazione(d), nonMisurato(d)
      ].join("");
    }
    document.getElementById("orario").textContent =
      "EcoTokens v" + (d.version || "?") + " · ultimo aggiornamento " +
      new Date(d.generated_at * 1000).toLocaleString("it-IT");
  }

  function errore(messaggio) {
    document.getElementById("contenuto").innerHTML = pannello(
      "Il gateway non risponde",
      "La pagina resta quella dell'ultimo aggiornamento riuscito. Riprova da sola fra qualche secondo.",
      '<p class="mono muted">' + esc(messaggio) + "</p>", true);
  }

  function aggiorna() {
    if (!attiva) return;
    fetch("/admin/live", { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(disegna)
      .catch(function (e) { errore(e.message); });
  }

  document.getElementById("pausa").addEventListener("click", function () {
    attiva = !attiva;
    this.textContent = attiva ? "Ferma" : "Riprendi";
    document.getElementById("pulse").className = attiva ? "pulse" : "pulse ferma";
    document.getElementById("pulse-testo").textContent = attiva ? "in ascolto" : "ferma";
    if (attiva) aggiorna();
  });

  document.getElementById("tema").addEventListener("click", function () {
    var r = document.documentElement;
    var scuro = r.getAttribute("data-theme") === "dark" ||
      (!r.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    r.setAttribute("data-theme", scuro ? "light" : "dark");
  });

  aggiorna();
  setInterval(aggiorna, INTERVALLO);
})();
"""
