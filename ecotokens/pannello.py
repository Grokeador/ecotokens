"""Pannello di controllo: cambiare le impostazioni senza aprire un file.

Le tre pagine esistenti mostrano numeri; questa e' l'unica che **decide**. La
differenza cambia tutto, e da li' vengono le sue regole.

**Un elenco esplicito di cio' che si puo' toccare.** Non tutto cio' che sta in
`Settings`: i campi sono dichiarati uno per uno qui sotto. Quel che resta fuori
resta fuori per una ragione, non per dimenticanza - le credenziali perche' una
chiave non si scrive in un campo di un modulo web, l'indirizzo di ascolto
perche' cambiarlo da una pagina raggiungibile via rete e' il modo piu' rapido
di aprirsi al mondo per sbaglio, il percorso del database perche' la
connessione e' gia' aperta.

**Ogni interruttore dice cosa costa.** E' la voce del progetto: un pannello che
elenca opzioni senza dire cosa fanno sposta la decisione sull'utente senza
dargli niente per prenderla. I numeri accanto a ogni campo sono misurati, e
dove non lo sono c'e' scritto che non lo sono.

**Cio' che cambia il contenuto delle risposte e' segnato.** Declassamento del
modello, effort sempre basso, cache semantica: non sono ottimizzazioni neutre,
e chi li accende deve saperlo mentre li accende, non dopo.

Le modifiche vengono scritte nel file di configurazione **e** applicate subito
alla pipeline in esecuzione: un pannello che chiede di riavviare per avere
effetto viene usato una volta sola.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import Settings

# --- cosa si puo' cambiare ------------------------------------------------


@dataclass(frozen=True)
class Campo:
    """Un'impostazione modificabile, con il suo prezzo dichiarato."""

    chiave: str  # "budget.daily_usd"
    etichetta: str
    tipo: Literal["booleano", "numero", "decimale", "scelta"]
    spiegazione: str
    scelte: tuple[str, ...] = ()
    minimo: float | None = None
    massimo: float | None = None
    # Vero se accendere questa opzione puo' cambiare **cosa** risponde il
    # modello, non solo quanto costa.
    cambia_risposte: bool = False


@dataclass(frozen=True)
class Gruppo:
    nome: str
    descrizione: str
    campi: tuple[Campo, ...]


GRUPPI: tuple[Gruppo, ...] = (
    Gruppo(
        nome="Profilo",
        descrizione=(
            "L'interruttore che governa gli altri. Cambiandolo si riscrivono i "
            "valori predefiniti di router ed effort; quelli scritti a mano qui "
            "sotto restano come sono."
        ),
        campi=(
            Campo(
                chiave="profilo",
                etichetta="Profilo",
                tipo="scelta",
                scelte=("prudente", "aggressivo"),
                cambia_risposte=True,
                spiegazione=(
                    "prudente: la risposta e' la stessa, pagata meno - 23,2% in meno "
                    "di un'applicazione che usa gia' il caching automatico. "
                    "aggressivo: 85,2%, ma declassa il modello e tiene l'effort al "
                    "minimo, quindi una parte del risparmio e' un'altra risposta."
                ),
            ),
        ),
    ),
    Gruppo(
        nome="Cache dei prompt",
        descrizione=(
            "La leva piu' grossa, ma non tutta merito del gateway: il caching "
            "automatico di Anthropic vale il 67,8% ed e' gratis per chiunque. "
            "Il pianificatore ne aggiunge 0,7 in media - meno 0,1 su una "
            "conversazione sola, piu' 19,9 quando molte richieste condividono un "
            "prefisso. Contro il confronto che conta davvero - uno sviluppatore "
            "che mette un `cache_control` sul proprio system prompt, una riga - "
            "il pianificatore vale +52,3% su un ciclo agentico con tool, +1,1% "
            "su una chat che cresce e -0,1% su molti utenti a turno singolo."
        ),
        campi=(
            Campo("cache_planner.enabled", "Pianificatore acceso", "booleano",
                  "Spento, ogni prompt si paga a prezzo pieno."),
            Campo("cache_planner.mode", "Chi piazza i breakpoint", "scelta",
                  "automatico delega ad Anthropic (un campo, zero manutenzione); "
                  "manuale usa il pianificatore di EcoTokens, che conviene dove piu' "
                  "richieste diverse condividono il prompt di sistema.",
                  scelte=("automatico", "manuale")),
            Campo("cache_planner.max_breakpoints", "Breakpoint massimi", "numero",
                  "L'API ne accetta al massimo 4.", minimo=0, massimo=4),
            Campo("cache_planner.adatta_primo_turno",
                  "Osserva se conviene marcare la coda", "booleano",
                  "Marcare la coda di una richiesta appena arrivata costa 0,25x "
                  "subito e rende 0,9x solo se qualcuno la rilegge: conviene sopra "
                  "il 27,8% di conversazioni che proseguono, che e' il rapporto fra "
                  "i due moltiplicatori dell'API e non un numero scelto. Acceso, il "
                  "gateway osserva quella frazione sulle proprie sessioni invece di "
                  "indovinarla. Misurato su traffico a turno singolo: porta il "
                  "merito del gateway da -1,6% a -0,2%, cioe' da dannoso a neutro, "
                  "senza togliere niente alle conversazioni che proseguono."),
        ),
    ),
    Gruppo(
        nome="Cache delle risposte",
        descrizione=(
            "L'unica ottimizzazione che vale il prezzo pieno: un hit non sconta "
            "la richiesta, la elimina."
        ),
        campi=(
            Campo("exact_cache.enabled", "Cache esatta", "booleano",
                  "Spenta, due richieste identiche si pagano due volte."),
            Campo("exact_cache.ttl_seconds", "Durata delle voci (secondi)", "numero",
                  "86400 e' un giorno. Piu' lunga significa piu' hit, ma anche "
                  "risposte piu' vecchie servite come se fossero nuove: la cache "
                  "esatta non sa se il mondo e' cambiato da allora.",
                  minimo=60, massimo=2_592_000),
            Campo("semantic_cache.enabled", "Cache semantica", "booleano",
                  "Serve una risposta gia' data a una domanda **solo simile**. E' "
                  "l'unico stadio che possa restituire una risposta sbagliata: due "
                  "domande vicine nello spazio degli embedding possono avere risposte "
                  "giuste diverse.", cambia_risposte=True),
        ),
    ),
    Gruppo(
        nome="Contesto",
        descrizione=(
            "Non e' un'ottimizzazione di costo: e' una difesa contro l'overflow. "
            "Misurato con `ecotokens ritenzione`: con potatura e riassunto accesi "
            "sopravvive lo **zero per cento** dei fatti piantati in un turno "
            "lontano. Accendere anche la memoria riporta al cento."
        ),
        campi=(
            Campo("context.enabled", "Potatura del contesto", "booleano",
                  "Toglie i vecchi risultati dei tool. Spenta, niente viene tolto: "
                  "nessun rischio di perdere un dato, nessuna difesa dall'overflow.",
                  cambia_risposte=True),
            Campo("context.prune_step_turns", "Ogni quanti turni pota", "numero",
                  "A scatti, non a ogni turno: un confine che insegue la coda cambia "
                  "il prefisso e distrugge la cache. A passo 4 le scritture mai "
                  "rilette sono 16.999 token, a passo 7 sono 4.509.",
                  minimo=1, massimo=50),
            Campo("context.local_compaction", "Riassunto della cronologia", "booleano",
                  "Sostituisce la parte vecchia con un riassunto. Perde dettaglio.",
                  cambia_risposte=True),
        ),
    ),
    Gruppo(
        nome="Modello ed effort",
        descrizione=(
            "Le due leve che cambiano **cosa** risponde il modello. Il banco misura "
            "quanto e' lunga una risposta, non se e' giusta: il risparmio di questo "
            "gruppo e' interamente misurato e il suo costo interamente no."
        ),
        campi=(
            Campo("router.effort_downshift", "Abbassa l'effort", "booleano",
                  "Riduce il ragionamento sulle domande semplici. Non tocca la cache.",
                  cambia_risposte=True),
            Campo("router.effort_policy", "Quando abbassarlo", "scelta",
                  "adattivo: solo dove il router giudica sicuro. sempre_basso: su ogni "
                  "richiesta, difficolta' ignorata.",
                  scelte=("adattivo", "sempre_basso"), cambia_risposte=True),
            Campo("router.model_downgrade", "Cambio di modello", "booleano",
                  "Vale il 17% del risparmio, piu' di tutti gli altri stadi tranne il "
                  "caching. Ma azzera la cache, che e' legata al modello, e alza la "
                  "soglia minima da 512 a 4096 token: su prompt medi la cache si "
                  "spegne in silenzio.", cambia_risposte=True),
        ),
    ),
    Gruppo(
        nome="Memoria",
        descrizione=(
            "Spenta di default perche' il banco non la sa misurare: ne vede il "
            "costo e non il beneficio. `ecotokens ritenzione` misura la meta' "
            "misurabile - se l'informazione arriva fino al prompt."
        ),
        campi=(
            Campo("memory.enabled", "Memoria dei fatti", "booleano",
                  "Estrae fatti stabili e li rimette nelle richieste successive. "
                  "Costa una chiamata di estrazione per turno."),
            Campo("memory.retrieval", "Come recupera i fatti", "scelta",
                  "stabile: tutti i fatti nel prefisso in cache, immune al problema "
                  "sotto. pertinente: solo quelli che somigliano alla domanda - ma la "
                  "ricerca e' lessicale, e su fatti scritti telegrafici trova zero "
                  "fatti su tre. Il prefisso costa lo 0,4-2,2% in piu': e' il prezzo "
                  "di un recupero che funziona.",
                  scelte=("stabile", "pertinente")),
        ),
    ),
    Gruppo(
        nome="Tetto di spesa",
        descrizione=(
            "L'unica funzione il cui scopo non e' risparmiare ma **impedire**. "
            "Spenta di default perche' non esiste una cifra predefinita sensata."
        ),
        campi=(
            Campo("budget.enabled", "Tetto acceso", "booleano",
                  "Al superamento le richieste vengono rifiutate prima di raggiungere "
                  "l'API."),
            Campo("budget.daily_usd", "Massimo al giorno (USD)", "decimale",
                  "0 mette il gateway in sola lettura.", minimo=0, massimo=10_000),
            Campo("budget.monthly_usd", "Massimo al mese (USD)", "decimale",
                  "Vale insieme a quello giornaliero, non al posto suo: sono due "
                  "domande diverse - oggi non ho speso, ma il mese e' finito.",
                  minimo=0, massimo=100_000),
            Campo("budget.precount", "Preventivo prima di inviare", "booleano",
                  "Conta i token con count_tokens prima di spendere, cosi' la "
                  "richiesta che sforerebbe viene fermata invece di essere l'ultima. "
                  "Costa un round-trip in piu', non fatturato."),
        ),
    ),
    Gruppo(
        nome="Osservazione",
        descrizione=(
            "Quanto guardano indietro le pagine, e quanto dettaglio si conserva. "
            "Leggevano ventimila righe ogni cinque secondi tenendo il lock del "
            "database, cioe' rallentavano le richieste vere."
        ),
        campi=(
            Campo("storage.observability_window", "Richieste guardate dalle pagine",
                  "numero",
                  "La domanda di console e quadro e' *cosa sta succedendo adesso*. "
                  "Il totale storico non ne risente: e' aggregato in SQL.",
                  minimo=100, massimo=50_000),
            Campo("storage.keep_detail_days", "Giorni di dettaglio conservati", "numero",
                  "Oltre, `ecotokens purge` aggrega in un riepilogo giornaliero e "
                  "cancella. I totali di costo e risparmio restano identici; spariscono "
                  "latenza, note e attribuzione per stadio.",
                  minimo=1, massimo=3650),
        ),
    ),
)

TUTTI_I_CAMPI: dict[str, Campo] = {
    campo.chiave: campo for gruppo in GRUPPI for campo in gruppo.campi
}

# Cio' che il pannello non tocca, e perche'. Compare in fondo alla pagina:
# un elenco di esclusioni senza motivazioni sembra una mancanza, con le
# motivazioni e' una scelta.
FUORI_PORTATA: tuple[tuple[str, str], ...] = (
    (
        "Chiavi e credenziali",
        "Ne' quella di Anthropic ne' quella del gateway. Una chiave non si "
        "scrive in un campo di un modulo web, e questo pannello finirebbe per "
        "mostrarla a chi apre la pagina.",
    ),
    (
        "Indirizzo e porta di ascolto",
        "Cambiarli da una pagina raggiungibile via rete e' il modo piu' rapido "
        "di aprirsi al mondo per sbaglio. Stanno nel file, dove serve accesso "
        "alla macchina.",
    ),
    (
        "Percorso del database",
        "La connessione e' gia' aperta: spostarla mentre il gateway lavora "
        "significherebbe perdere di vista i consumi in corso.",
    ),
    (
        "Modello predefinito e finestra di contesto",
        "Il primo lo sceglie il client a ogni richiesta; la seconda la decide "
        "il modello, non noi.",
    ),
)


# --- leggere e scrivere ---------------------------------------------------


def valore_corrente(settings: Settings, chiave: str) -> Any:
    """Il valore attuale di un campo, seguendo il percorso puntato."""
    oggetto: Any = settings
    for pezzo in chiave.split("."):
        oggetto = getattr(oggetto, pezzo)
    return oggetto


def stato(settings: Settings) -> dict[str, Any]:
    return {chiave: valore_corrente(settings, chiave) for chiave in TUTTI_I_CAMPI}


class ModificaRifiutata(ValueError):
    """Un valore fuori dai limiti dichiarati, o un campo che non esiste."""


def _converti(campo: Campo, grezzo: Any) -> Any:
    """Dal modulo HTML al tipo giusto, rifiutando cio' che non torna.

    I limiti sono quelli dichiarati nel campo, non quelli di pydantic: un
    numero di breakpoint superiore a 4 verrebbe accettato dal modello e
    rifiutato dall'API a meta' richiesta, cioe' molto piu' tardi e molto meno
    chiaramente.
    """
    if campo.tipo == "booleano":
        return grezzo in (True, "true", "on", "1", 1)
    if campo.tipo == "scelta":
        if grezzo not in campo.scelte:
            raise ModificaRifiutata(
                f"{campo.etichetta}: {grezzo!r} non e' fra {', '.join(campo.scelte)}"
            )
        return grezzo
    try:
        numero = int(grezzo) if campo.tipo == "numero" else float(grezzo)
    except (TypeError, ValueError):
        raise ModificaRifiutata(f"{campo.etichetta}: {grezzo!r} non e' un numero") from None
    if campo.minimo is not None and numero < campo.minimo:
        raise ModificaRifiutata(f"{campo.etichetta}: minimo {campo.minimo:g}")
    if campo.massimo is not None and numero > campo.massimo:
        raise ModificaRifiutata(f"{campo.etichetta}: massimo {campo.massimo:g}")
    return numero


def prepara(settings: Settings, modifiche: dict[str, Any]) -> tuple[Settings, list[dict[str, Any]]]:
    """Costruisce le impostazioni nuove e l'elenco di cosa cambia davvero.

    Le nuove passano da `Settings.model_validate`, cioe' dalla stessa porta di
    quelle lette da file: un pannello che scrivesse direttamente sugli oggetti
    salterebbe le regole del profilo, e si otterrebbero configurazioni che il
    file non potrebbe mai produrre.
    """
    dati = settings.model_dump()
    cambiati: list[dict[str, Any]] = []

    for chiave, grezzo in modifiche.items():
        campo = TUTTI_I_CAMPI.get(chiave)
        if campo is None:
            raise ModificaRifiutata(f"{chiave}: non e' un campo modificabile")
        nuovo = _converti(campo, grezzo)
        prima = valore_corrente(settings, chiave)
        if nuovo == prima:
            continue

        pezzi = chiave.split(".")
        dove = dati
        for pezzo in pezzi[:-1]:
            dove = dove[pezzo]
        dove[pezzi[-1]] = nuovo
        cambiati.append(
            {"chiave": chiave, "etichetta": campo.etichetta, "prima": prima,
             "dopo": nuovo, "cambia_risposte": campo.cambia_risposte}
        )

    # Il profilo riscrive i propri campi solo se non erano stati decisi a mano.
    # Passando l'intero dump, ogni campo risulta "scritto", quindi il profilo
    # non cambierebbe niente: si toglie cio' che il profilo governa quando e'
    # il profilo stesso a essere cambiato.
    if any(voce["chiave"] == "profilo" for voce in cambiati):
        governati = ("enabled", "model_downgrade", "downgrade_policy", "effort_policy")
        # I campi **inviati**, non quelli risultati diversi. Chi spegne il
        # declassamento nello stesso salvataggio in cui accende il profilo
        # aggressivo ha deciso, anche se il valore che ha scelto coincide con
        # quello che c'era: "non e' cambiato" non vuol dire "non l'ha chiesto",
        # e trattarli allo stesso modo faceva vincere il profilo su una scelta
        # esplicita, in silenzio.
        toccati_a_mano = {
            chiave.split(".", 1)[1]
            for chiave in modifiche
            if chiave.startswith("router.")
        }
        for campo_router in governati:
            if campo_router not in toccati_a_mano:
                dati["router"].pop(campo_router, None)

    return Settings.model_validate(dati), cambiati


def scrivi_configurazione(settings: Settings, percorso: Path) -> None:
    """Riscrive il file con i valori del pannello.

    Il file viene **rigenerato**, non modificato riga per riga: i commenti che
    ci fossero dentro si perdono. E' detto nella pagina, perche' scoprirlo dopo
    sarebbe la versione da configurazione del `git checkout --` che cancella
    senza chiedere.
    """
    righe = [
        "# Scritto dal pannello di controllo di EcoTokens.",
        f"# Ultima modifica: {time.strftime('%d/%m/%Y %H:%M')}",
        "#",
        "# I commenti scritti a mano in questo file non sopravvivono a un",
        "# salvataggio dal pannello: se ne servono, tenerli altrove.",
        "#",
        "# Le credenziali non stanno qui e non devono starci: l'SDK risolve da",
        "# solo ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN o `ant auth login`.",
        "",
        f'profilo = "{settings.profilo}"',
    ]

    sezioni: dict[str, list[str]] = {}
    for chiave in TUTTI_I_CAMPI:
        if "." not in chiave:
            continue
        sezione, campo = chiave.split(".", 1)
        valore = valore_corrente(settings, chiave)
        if isinstance(valore, bool):
            reso = "true" if valore else "false"
        elif isinstance(valore, str):
            reso = f'"{valore}"'
        else:
            reso = repr(valore)
        sezioni.setdefault(sezione, []).append(f"{campo} = {reso}")

    for sezione, voci in sezioni.items():
        righe.append("")
        righe.append(f"[{sezione}]")
        righe.extend(voci)

    percorso.write_text("\n".join(righe) + "\n", encoding="utf-8")


# --- la pagina ------------------------------------------------------------


def _esc(valore: Any) -> str:
    return html.escape(str(valore), quote=True)


def _controllo(campo: Campo, valore: Any) -> str:
    """Il controllo di un campo. Nessun JavaScript: un modulo e basta.

    Una pagina che decide non deve dipendere da uno script che potrebbe non
    partire - e un modulo HTML funziona anche da un browser vecchio, da riga di
    comando con curl, e senza che il gateway debba servire altro.
    """
    nome = _esc(campo.chiave)
    if campo.tipo == "booleano":
        # Il campo nascosto porta il "false": una casella non spuntata non
        # viene inviata affatto, e senza di esso spegnere qualcosa sarebbe
        # indistinguibile dal non averlo toccato.
        return (
            f'<input type="hidden" name="{nome}" value="false">'
            f'<label class="interruttore"><input type="checkbox" name="{nome}" '
            f'value="true"{" checked" if valore else ""}><span></span></label>'
        )
    if campo.tipo == "scelta":
        opzioni = "".join(
            f'<option value="{_esc(s)}"{" selected" if s == valore else ""}>{_esc(s)}</option>'
            for s in campo.scelte
        )
        return f'<select name="{nome}">{opzioni}</select>'
    passo = "1" if campo.tipo == "numero" else "0.01"
    limiti = ""
    if campo.minimo is not None:
        limiti += f' min="{campo.minimo:g}"'
    if campo.massimo is not None:
        limiti += f' max="{campo.massimo:g}"'
    return (
        f'<input type="number" name="{nome}" value="{_esc(valore)}" '
        f'step="{passo}"{limiti}>'
    )


def render_pannello(
    settings: Settings,
    *,
    percorso_config: Path | str | None = None,
    esito: dict[str, Any] | None = None,
) -> str:
    """Il pannello. `esito` e' il riepilogo dell'ultimo salvataggio."""
    corrente = stato(settings)

    gruppi = []
    for gruppo in GRUPPI:
        righe = []
        for campo in gruppo.campi:
            avviso = (
                '<span class="tocca">cambia le risposte</span>'
                if campo.cambia_risposte
                else ""
            )
            righe.append(
                '<div class="campo">'
                f'<div class="testa"><label>{_esc(campo.etichetta)}{avviso}</label>'
                f"{_controllo(campo, corrente[campo.chiave])}</div>"
                f'<p class="perche">{_esc(campo.spiegazione)}</p>'
                "</div>"
            )
        gruppi.append(
            f'<section class="gruppo"><h2>{_esc(gruppo.nome)}</h2>'
            f'<p class="intro">{_esc(gruppo.descrizione)}</p>'
            f'{"".join(righe)}</section>'
        )

    banda = ""
    if esito:
        if esito.get("errore"):
            banda = f'<div class="banda male">{_esc(esito["errore"])}</div>'
        elif esito.get("cambiati"):
            voci = "".join(
                f"<li>{_esc(v['etichetta'])}: <b>{_esc(v['prima'])}</b> &rarr; "
                f"<b>{_esc(v['dopo'])}</b>"
                + (
                    ' <span class="tocca">cambia le risposte</span>'
                    if v["cambia_risposte"]
                    else ""
                )
                + "</li>"
                for v in esito["cambiati"]
            )
            banda = (
                '<div class="banda bene"><b>Applicato subito</b>, e scritto in '
                f'<span class="mono">{_esc(esito.get("file", "ecotokens.toml"))}</span>.'
                f"<ul>{voci}</ul></div>"
            )
        else:
            banda = '<div class="banda">Nessun valore era diverso: niente da salvare.</div>'

    esclusi = "".join(
        f"<li><b>{_esc(cosa)}</b> &mdash; {_esc(perche)}</li>"
        for cosa, perche in FUORI_PORTATA
    )
    dove = _esc(percorso_config or "ecotokens.toml")

    return f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EcoTokens - impostazioni</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Impostazioni</h1>
    <nav><a href="/quadro">quadro</a> &middot; <a href="/">console</a> &middot;
    <a href="/admin/dashboard">rapporto</a></nav>
  </header>
  <p class="lede">Ogni voce dice cosa costa. I numeri sono misurati; dove non lo
  sono, c&rsquo;&egrave; scritto. Le modifiche valgono <b>subito</b> per le
  richieste successive e vengono scritte in <span class="mono">{dove}</span>.</p>
  {banda}
  <form method="post" action="/impostazioni">
    {"".join(gruppi)}
    <div class="azioni">
      <button type="submit">Salva e applica</button>
      <span class="nota">Il file viene rigenerato: i commenti scritti a mano
      dentro non sopravvivono.</span>
    </div>
  </form>
  <section class="gruppo fuori">
    <h2>Cosa questo pannello non tocca</h2>
    <p class="intro">Non per dimenticanza. Queste stanno nel file di
    configurazione, dove serve accesso alla macchina.</p>
    <ul>{esclusi}</ul>
  </section>
</div>
</body>
</html>
"""


_CSS = """
:root {
  --ground:#eef1f2; --surface:#fbfcfc; --sunken:#e6ebec; --ink:#12191b;
  --soft:#58666b; --faint:#8b989c; --rule:#d5dcde; --accent:#15616d;
  --good:#1b7a4b; --good-soft:#cfe6da; --warn:#b07d2b; --warn-soft:#f2e5cb;
  --bad:#a33a24; --bad-soft:#f0d9d2;
  --mono: ui-monospace, "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#0d1214; --surface:#161d20; --sunken:#101619; --ink:#e6ebec;
    --soft:#9dabaf; --faint:#6d7c81; --rule:#253034; --accent:#55b2c1;
    --good:#4bb582; --good-soft:#16332a; --warn:#d3a55a; --warn-soft:#33280f;
    --bad:#dd8368; --bad-soft:#38211b;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--ground); color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.page { max-width:820px; margin:0 auto; padding:1.5rem 1rem 3rem; }
header { display:flex; flex-wrap:wrap; gap:.5rem 1.5rem; align-items:baseline;
  justify-content:space-between; }
h1 { margin:0; font-size:1.5rem; font-weight:600; letter-spacing:-.015em; }
nav, .nota { font-size:.8rem; color:var(--faint); }
a { color:var(--accent); }
.lede { color:var(--soft); font-size:.9rem; max-width:66ch; margin:.4rem 0 1.2rem; }
.mono { font-family:var(--mono); }

.banda { border-radius:8px; padding:.7rem .9rem; margin-bottom:1.2rem;
  background:var(--sunken); border:1px solid var(--rule); font-size:.87rem; }
.banda.bene { background:var(--good-soft); border-color:var(--good); }
.banda.male { background:var(--bad-soft); border-color:var(--bad); }
.banda ul { margin:.4rem 0 0; padding-left:1.2rem; }
.banda li { margin:.15rem 0; }

.gruppo { background:var(--surface); border:1px solid var(--rule);
  border-radius:9px; padding:1rem 1.1rem; margin-bottom:.9rem; }
.gruppo h2 { margin:0; font-size:1rem; font-weight:600; }
.intro { margin:.3rem 0 .8rem; font-size:.83rem; color:var(--soft); max-width:70ch; }

.campo { padding:.55rem 0; border-top:1px solid var(--rule); }
.campo:first-of-type { border-top:none; }
.testa { display:flex; align-items:center; justify-content:space-between; gap:1rem; }
.testa label { font-size:.9rem; font-weight:500; display:flex; align-items:center; gap:.5rem; }
.perche { margin:.25rem 0 0; font-size:.78rem; color:var(--faint); max-width:72ch; }
.tocca { font-size:.65rem; font-weight:700; text-transform:uppercase;
  letter-spacing:.05em; color:var(--warn); background:var(--warn-soft);
  padding:.1rem .4rem; border-radius:999px; white-space:nowrap; }

input[type=number], select {
  font:inherit; font-size:.85rem; font-family:var(--mono); color:var(--ink);
  background:var(--sunken); border:1px solid var(--rule); border-radius:6px;
  padding:.25rem .5rem; min-width:9rem;
}
input:focus-visible, select:focus-visible, button:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px;
}
.interruttore { position:relative; display:inline-block; width:2.6rem; height:1.4rem; flex:none; }
.interruttore input { position:absolute; opacity:0; width:100%; height:100%; margin:0; cursor:pointer; }
.interruttore span { position:absolute; inset:0; background:var(--sunken);
  border:1px solid var(--rule); border-radius:999px; transition:background .12s; }
.interruttore span::after { content:""; position:absolute; top:2px; left:2px;
  width:1rem; height:1rem; border-radius:50%; background:var(--faint); transition:transform .12s; }
.interruttore input:checked + span { background:var(--good-soft); border-color:var(--good); }
.interruttore input:checked + span::after { transform:translateX(1.2rem); background:var(--good); }

.azioni { display:flex; align-items:center; gap:1rem; flex-wrap:wrap; margin:1.2rem 0 1.6rem; }
button { font:inherit; font-size:.9rem; font-weight:600; color:var(--surface);
  background:var(--accent); border:none; border-radius:7px; padding:.5rem 1.3rem; cursor:pointer; }
.fuori ul { margin:0; padding-left:1.2rem; font-size:.82rem; color:var(--soft); }
.fuori li { margin:.35rem 0; }
@media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
"""
