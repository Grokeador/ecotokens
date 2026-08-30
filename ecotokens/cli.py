"""Interfaccia a riga di comando: ``ecotokens serve | stats | purge``."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from .config import intestazioni_upstream, load_settings
from .store.db import Database
from .store.repos import Store

app = typer.Typer(add_completion=False, help="Gateway locale per Claude con economia di token")
console = Console()


# Indirizzi che restano sulla macchina. Tutto il resto e' raggiungibile da
# qualcun altro, e cambia cosa vuol dire lasciare il gateway senza chiave.
INDIRIZZI_LOCALI = {"127.0.0.1", "::1", "localhost"}


def esigi_chiave_se_esposto(host: str, api_key: str | None) -> None:
    """Non si apre il gateway al mondo senza una chiave.

    Il valore predefinito e' 127.0.0.1 e va benissimo. Chi scrive `0.0.0.0`
    quasi sempre vuole raggiungerlo da un'altra macchina, e quasi mai ha
    pensato che nello stesso momento lo raggiunge chiunque altro sulla rete:
    la porta inoltra all'API con **la chiave Anthropic dell'utente**, quindi
    non e' un servizio che espone dei dati, e' uno che espone una carta di
    credito. In piu' la console e il quadro mostrano modelli, costi e frammenti
    dei prompt gia' passati.

    Fermarsi e' l'unica risposta onesta: un avviso in mezzo ai log di avvio non
    lo legge nessuno, e il costo di sbagliare qui non e' simmetrico.
    """
    if host in INDIRIZZI_LOCALI or api_key:
        return

    console.print(f"[red]Rifiuto di ascoltare su {host} senza una chiave del gateway.[/]")
    console.print(
        "Quell'indirizzo e' raggiungibile da altre macchine, e questa porta "
        "inoltra all'API con la tua chiave Anthropic: chi la trova spende a tuo "
        "nome. Console e quadro mostrerebbero inoltre i prompt gia' passati."
    )
    console.print()
    console.print("Nel file di configurazione:")
    # Le parentesi quadre sono la sintassi di Rich: senza escape "[server]"
    # viene letto come un tag di stile e sparisce dalla riga, lasciando un
    # esempio di configurazione a cui manca proprio l'intestazione.
    console.print(r"  [cyan]\[server][/]")
    console.print('  [cyan]api_key = "una-frase-lunga-a-caso"[/]')
    console.print()
    console.print(
        "[dim]E' la chiave del gateway, non quella di Anthropic: i client la "
        "presentano in Authorization: Bearer. Per esporlo davvero servono anche "
        "TLS e un proxy davanti - vedi il README, sezione Esporre il gateway.[/]"
    )
    raise typer.Exit(code=2)


def esigi_credenziali() -> None:
    """Ferma un comando --live prima che spenda, se non c'e' come autenticarsi.

    Senza questo controllo l'SDK solleva un `TypeError` a meta' della prima
    richiesta, e l'utente vede una traccia di stack invece di sapere che gli
    manca una variabile d'ambiente. Il controllo guarda cio' che l'SDK ha
    effettivamente risolto, non le variabili d'ambiente: cosi' riconosce anche
    un profilo creato con `ant auth login`.
    """
    import anthropic

    client = anthropic.AsyncAnthropic()
    risolto = any(
        getattr(client, nome, None) is not None
        for nome in ("api_key", "auth_token", "credentials")
    )
    if risolto:
        return

    console.print("[red]Nessuna credenziale Anthropic trovata.[/]")
    console.print(
        "La misura --live chiama l'API vera e ha bisogno di autenticarsi. "
        "Le chiavi si creano su https://console.anthropic.com/settings/keys"
    )
    console.print()
    console.print("Impostala come variabile d'ambiente (poi riapri il terminale):")
    console.print('  [cyan]setx ANTHROPIC_API_KEY "la-tua-chiave"[/]')
    console.print()
    console.print(
        "[dim]Non metterla nel file di configurazione: e' il modo piu' facile "
        "per pubblicarla per sbaglio.[/]"
    )
    raise typer.Exit(code=2)



@app.command()
def serve(
    host: Optional[str] = typer.Option(None, help="Indirizzo di ascolto"),
    port: Optional[int] = typer.Option(None, help="Porta di ascolto"),
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    reload: bool = typer.Option(False, help="Ricarica automatica (sviluppo)"),
) -> None:
    """Avvia il gateway."""
    settings = load_settings(config)
    host = host or settings.server.host
    port = port or settings.server.port
    esigi_chiave_se_esposto(host, settings.server.api_key)

    logging.basicConfig(
        level=settings.server.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console.print(f"[bold green]EcoTokens[/] in ascolto su [cyan]http://{host}:{port}/v1[/]")
    console.print("Nei client basta impostare questo indirizzo come base_url.")
    console.print(
        f"Console dal vivo: [cyan]http://{host}:{port}/[/] "
        "[dim]- cosa fa ogni stadio, richiesta per richiesta[/]"
    )
    console.print(
        f"Quadro:           [cyan]http://{host}:{port}/quadro[/] "
        "[dim]- tutti i parametri su una schermata[/]"
    )
    console.print(
        f"Impostazioni:     [cyan]http://{host}:{port}/impostazioni[/] "
        "[dim]- cosa deve fare, con il prezzo di ogni scelta[/]\n"
    )

    from .server import create_app

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level=settings.server.log_level,
        reload=reload,
    )


@app.command()
def diagnosi(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
) -> None:
    """Controlla l'installazione: cosa non funzionera', prima che smetta.

    Quasi tutti i modi di configurare male questo gateway **non danno
    errore**. Una chiave assente si manifesta come un 401 sulla prima
    richiesta vera, una cartella non scrivibile come un registro che resta
    vuoto, FTS5 mancante come una memoria che non trova niente. Sono guasti
    silenziosi, e un guasto silenzioso costa piu' di uno rumoroso: si scopre
    dopo, quando si e' gia' concluso qualcosa di sbagliato.

    Il codice di uscita e' 0 se va tutto, 1 con avvisi, 2 con problemi gravi:
    cosi' si puo' mettere davanti a `serve` in uno script di avvio.
    """
    from .config import load_settings
    from .diagnosi import AVVISO, GRAVE, OK, esegui

    settings = load_settings(config)
    esito = esegui(settings, percorso_config=config)

    segni = {OK: "[green]OK[/]", AVVISO: "[yellow]!![/]", GRAVE: "[red]NO[/]"}
    tabella = Table(show_header=False, box=None, padding=(0, 1))
    tabella.add_column(width=3)
    tabella.add_column(style="bold", width=22)
    tabella.add_column(overflow="fold")

    for voce in esito.esiti:
        tabella.add_row(segni[voce.stato], voce.nome, voce.dettaglio)
        if voce.rimedio:
            tabella.add_row("", "", f"[dim]{voce.rimedio}[/]")
    console.print()
    console.print(tabella)
    console.print()

    if esito.gravi:
        quanti = len(esito.gravi)
        console.print(
            f"[red]{quanti} problem{'a' if quanti == 1 else 'i'} da risolvere "
            "prima di usarlo sul serio.[/]"
        )
    elif esito.avvisi:
        quanti = len(esito.avvisi)
        console.print(
            f"[yellow]{quanti} avvis{'o' if quanti == 1 else 'i'}: funziona, "
            "ma non come potrebbe.[/]"
        )
    else:
        console.print("[green]Tutto a posto.[/]")
    raise typer.Exit(code=esito.codice_uscita)


@app.command()
def consiglia(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
) -> None:
    """Che forma ha il **tuo** traffico, e cosa conviene accendere per quella.

    Tutte le percentuali che questo progetto pubblica sono medie su un corpus
    di scenari, e la stessa configurazione rende +52% su un ciclo agentico e
    -0,2% su molti utenti a turno singolo. Una media fra quei due non descrive
    nessuno.

    Il comando non misura niente di nuovo: legge il traffico gia' registrato e
    accosta a ogni consiglio il numero misurato **per quel regime**. Dove il
    campione e' troppo piccolo per decidere, lo dice invece di consigliare.
    """
    from .consiglia import CAMPIONE_MINIMO, DESCRIZIONE, analizza

    settings = load_settings(config)

    async def _raccogli():
        database = Database(settings.storage.path)
        database.connect()
        store = Store(database)
        try:
            return await store.profilo_traffico()
        finally:
            database.close()

    rapporto = analizza(asyncio.run(_raccogli()), settings)

    if not rapporto.sufficiente:
        console.print(
            f"[yellow]Campione troppo piccolo:[/] {rapporto.campione} richieste "
            f"registrate, ne servono almeno {CAMPIONE_MINIMO}."
        )
        console.print(
            "[dim]La quota di sessioni a turno singolo e il tasso di "
            "continuazione sono rapporti: su poche richieste oscillano fra 0 e "
            "1 senza dire niente. Meglio nessun consiglio che uno inventato con "
            "la faccia di uno misurato.[/]"
        )
        raise typer.Exit(code=0)

    segnali = rapporto.segnali
    console.print()
    console.print(
        f"[bold]Il tuo traffico somiglia a:[/] {DESCRIZIONE[rapporto.regime]}"
    )
    console.print(f"[dim]su {rapporto.campione:,} richieste registrate[/]")

    tavola = Table(title="I segnali su cui si regge questa lettura")
    tavola.add_column("Segnale")
    tavola.add_column("Osservato", justify="right")
    tasso = segnali.get("tasso_continuazione")
    for etichetta, valore in (
        ("turni per sessione (media)", f"{segnali.get('turni_medi', 0):.1f}"),
        ("sessioni a turno singolo", f"{segnali.get('quota_turno_singolo', 0):.0%}"),
        ("richieste servite da cache", f"{segnali.get('quota_da_cache', 0):.0%}"),
        ("richieste con potatura attiva", f"{segnali.get('quota_potatura', 0):.0%}"),
        ("prompt medio", f"{segnali.get('prompt_medio', 0):,.0f} token"),
        (
            "conversazioni che proseguono",
            f"{tasso:.0%}" if tasso is not None else "campione insufficiente",
        ),
    ):
        tavola.add_row(etichetta, valore)
    console.print(tavola)

    for consiglio in rapporto.consigli:
        console.print()
        console.print(f"[bold]{consiglio.titolo}[/]")
        console.print(f"  [cyan]{consiglio.verdetto}[/]")
        console.print(f"  [dim]{consiglio.perche}[/]")
        if consiglio.azione:
            console.print(f"  [yellow]{consiglio.azione}[/]")


@app.command()
def assunzioni() -> None:
    """Cosa il progetto da' per vero senza averlo verificato.

    Tutte le misure girano contro un simulatore, ed e' una scelta: i test non
    devono richiedere rete, e un banco che chiama l'API vera costa a ogni
    esecuzione. Ma un simulatore e' un insieme di assunzioni, e finche' restano
    implicite «risparmia il 72%» vuol dire «risparmia il 72% *se* sono tutte
    giuste», senza che nessuno sappia quante siano.

    Elencarle non le verifica. Trasforma un dubbio senza contorni in una lista
    finita, dove ogni voce dice cosa risulterebbe diverso se fosse sbagliata.
    """
    from .assunzioni import ASSUNZIONI, DICHIARATA, DOCUMENTATA, riepilogo

    ordine = {DOCUMENTATA: 0, DICHIARATA: 1}
    for voce in sorted(ASSUNZIONI, key=lambda a: (ordine.get(a.fonte, 2), a.nome)):
        colore = {"documentata": "cyan", "dichiarata": "yellow"}.get(voce.fonte, "green")
        console.print()
        console.print(f"[bold]{voce.nome}[/]  [{colore}]{voce.fonte}[/]")
        console.print(f"  {voce.valore}   [dim]({voce.dove})[/]")
        console.print(f"  [dim]Se fosse sbagliata:[/] {voce.cosa_cambia}")
        console.print(f"  [dim]Come verificarla:[/] {voce.come_verificarla}")

    console.print()
    console.print(f"[bold]{riepilogo()}[/]")
    console.print(
        "[dim]Le voci [yellow]dichiarate[/dim] sono quelle che possono spostare un "
        "numero del README. Le altre possono essere invecchiate, non inventate.[/]"
    )


@app.command()
def verifica(
    live: bool = typer.Option(
        False, "--live", help="Chiama l'API vera. Costa, ed e' l'unico modo che vale."
    ),
    simulato: bool = typer.Option(
        False,
        "--anche-simulato",
        help="Esegui contro il simulatore. I risultati sono circolari, e lo dice.",
    ),
    modello: str = typer.Option("claude-opus-5", help="Modello su cui verificare"),
) -> None:
    """Controlla le assunzioni del simulatore contro il comportamento vero.

    `ecotokens assunzioni` elenca cosa il progetto da' per vero; questo lo
    verifica. Contro il simulatore ogni controllo passa e **non significa
    niente**: si verificherebbe il simulatore contro se stesso, cioe' si
    produrrebbe una spunta verde priva di informazione. E' la forma di misura
    che questo progetto ha gia' sbagliato tre volte, quindi serve `--live` -
    oppure `--anche-simulato`, e allora ogni riga porta scritto che e' circolare.
    """
    import asyncio

    from .verifica import (
        CHIAMATE_PREVISTE,
        COMBACIA,
        DIVERGE,
        INDETERMINATO,
        nomi_scoperti,
        verifica as esegui_verifica,
    )

    if not (live or simulato):
        console.print(
            "[yellow]Serve --live.[/] Contro il simulatore ogni controllo passa "
            "per costruzione, e una spunta che non puo' fallire non e' una "
            "verifica. Per eseguirlo lo stesso, e vederlo dichiarato circolare: "
            "[cyan]--anche-simulato[/]."
        )
        raise typer.Exit(code=2)

    if live:
        esigi_credenziali()
        console.print(
            f"[dim]{CHIAMATE_PREVISTE} chiamate all'API su {modello}. "
            "Prompt corti: qualche centesimo.[/]"
        )

    async def gira():
        import anthropic

        if live:
            return await esegui_verifica(
                anthropic.AsyncAnthropic(
                    default_headers=intestazioni_upstream() or None
                ),
                modello,
            )

        import httpx2

        from .simulator import create_stub

        stub_app, _ = create_stub()
        client = anthropic.AsyncAnthropic(
            api_key="prova",
            base_url="http://simulatore",
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.ASGITransport(app=stub_app)
            ),
        )
        try:
            return await esegui_verifica(client, modello, circolare=True)
        finally:
            await client.close()

    rapporto = asyncio.run(gira())

    segni = {COMBACIA: "[green]OK[/]", DIVERGE: "[red]NO[/]", INDETERMINATO: "[yellow]??[/]"}
    for controllo in rapporto.controlli:
        console.print()
        console.print(f"{segni[controllo.esito]} [bold]{controllo.assunzione}[/]")
        console.print(f"   atteso:    {controllo.atteso}")
        console.print(f"   osservato: {controllo.osservato}")
        if controllo.nota:
            console.print(f"   [dim]{controllo.nota}[/]")

    console.print()
    console.print(f"[bold]{rapporto.riepilogo()}[/]")
    scoperte = sorted(nomi_scoperti())
    if scoperte:
        console.print(
            "[dim]Non controllate da qui: " + ", ".join(scoperte) + ". "
            "Un elenco di cio' che si sa fare, senza quello di cio' che non si "
            "sa fare, si legge come se coprisse tutto.[/]"
        )
    raise typer.Exit(code=2 if rapporto.divergenze else 0)


@app.command()
def stats(config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione")) -> None:
    """Mostra consumi, costi e risparmio registrati."""
    settings = load_settings(config)

    async def _collect():
        database = Database(settings.storage.path)
        database.connect()
        store = Store(database)
        try:
            return (
                await store.stats(),
                await store.current_spend(),
                await store.estimate_calibration(),
                await store.cache_write_report(),
            )
        finally:
            database.close()

    data, (today, month), taratura, scritture = asyncio.run(_collect())

    if not data.get("requests"):
        console.print("[yellow]Nessuna richiesta registrata finora.[/]")
        # La taratura si mostra comunque: si puo' aver chiamato solo
        # count_tokens, che non genera niente ma produce campioni.
        _stampa_taratura(taratura)
        return

    prompt_tokens = int(data.get("total_prompt_tokens") or 0)
    cost = float(data.get("cost_usd") or 0)
    baseline = float(data.get("baseline_cost_usd") or 0)
    saved = float(data.get("saved_usd") or 0)

    table = Table(title="EcoTokens - riepilogo", show_header=False)
    table.add_row("Richieste", f"{int(data['requests']):,}")
    table.add_row("Token di prompt", f"{prompt_tokens:,}")
    table.add_row("  di cui letti da cache", f"{int(data.get('cache_read_tokens') or 0):,}")
    table.add_row("  di cui scritti in cache", f"{int(data.get('cache_creation_tokens') or 0):,}")
    table.add_row("Token di output", f"{int(data.get('output_tokens') or 0):,}")
    table.add_row("Quota di prompt da cache", f"{data.get('cache_hit_ratio', 0) * 100:.1f}%")
    table.add_row("Costo effettivo", f"${cost:.4f}")
    table.add_row("Costo senza ottimizzazioni", f"${baseline:.4f}")
    style = "green" if saved >= 0 else "red"
    table.add_row("Risparmio", f"[{style}]${saved:.4f}[/]")

    # Il confronto che decide se convenga tenerlo acceso. Quello sopra si
    # misura contro un client che non usa affatto il prompt caching, cioe'
    # contro nessuno: chiunque integri l'API oggi mette un `cache_control` sul
    # proprio system prompt e ottiene lo sconto senza installare niente.
    ingenua = float(data.get("baseline_ingenua_usd") or 0)
    confrontabile = float(data.get("costo_confrontabile") or 0)
    confrontabili = int(data.get("richieste_confrontabili") or 0)
    if ingenua:
        merito = ingenua - confrontabile
        stile_merito = "green" if merito >= 0 else "red"
        table.add_row("", "")
        table.add_row(
            "Costo per chi marca il proprio system prompt", f"${ingenua:.4f}"
        )
        table.add_row(
            "[bold]Merito del gateway[/]",
            f"[{stile_merito} bold]${merito:+.4f}  "
            f"({merito / ingenua * 100:+.1f}% su {confrontabili:,} richieste)[/]",
        )
        # E subito sotto, la meta' che non e' la stessa risposta. Il profilo
        # spedito declassa il modello, i numeri pubblicati sono misurati senza
        # declassamento: senza questa riga le due cifre sembrano confrontabili
        # e non lo sono.
        chiesto = float(data.get("costo_modello_richiesto_usd") or 0)
        sostituite = int(data.get("richieste_con_sostituzione") or 0)
        if chiesto:
            da_sostituzione = chiesto - confrontabile
            table.add_row(
                "  di cui da sostituzione del modello",
                f"[yellow]${da_sostituzione:+.4f}  "
                f"(su {sostituite:,} richieste servite da un modello "
                f"diverso da quello chiesto)[/]",
            )
    table.add_row("Spesa di oggi", f"${today:.4f}")
    table.add_row("Spesa del mese", f"${month:.4f}")
    console.print(table)

    by_client = data.get("by_client") or []
    if by_client:
        clienti = Table(title="Per client")
        clienti.add_column("Client")
        clienti.add_column("Richieste", justify="right")
        clienti.add_column("Speso", justify="right")
        clienti.add_column("Risparmiato", justify="right")
        for riga in by_client:
            clienti.add_row(
                str(riga.get("client") or "-"),
                f"{int(riga.get('requests') or 0):,}",
                f"${float(riga.get('cost_usd') or 0):.4f}",
                f"${float(riga.get('saved_usd') or 0):.4f}",
            )
        console.print(clienti)
        console.print(
            "[dim]Solo il traffico non ancora compattato: `usage_daily` non "
            "conserva il nome del client, e attribuire a qualcuno la spesa di "
            "tutti sarebbe peggio che non attribuirla.[/]"
        )

    by_source = data.get("by_source") or []
    if by_source:
        sources = Table(title="Per origine della risposta")
        sources.add_column("Origine")
        sources.add_column("Richieste", justify="right")
        sources.add_column("Risparmio", justify="right")
        for row in by_source:
            sources.add_row(row["source"], f"{int(row['requests']):,}", f"${row['saved_usd']:.4f}")
        console.print(sources)

    _stampa_taratura(taratura)
    _stampa_scritture(scritture)

    by_model = data.get("by_model") or []
    if by_model:
        models = Table(title="Per modello")
        models.add_column("Modello")
        models.add_column("Richieste", justify="right")
        models.add_column("Costo", justify="right")
        for row in by_model:
            models.add_row(row["model"], f"{int(row['requests']):,}", f"${row['cost_usd']:.4f}")
        console.print(models)


def _stampa_taratura(taratura: list) -> None:
    """Quanto sbaglia lo stimatore locale rispetto al conteggio vero.

    Lo scarto medio da solo ingannerebbe: una stima che oscilla fra -30% e
    +40% ha media zero e non e' utilizzabile, mentre una che sbaglia del +5%
    sempre si corregge. Per questo accanto c'e' l'intervallo.
    """
    if not taratura:
        return
    metro = Table(title="Taratura dello stimatore (da count_tokens, senza costo aggiuntivo)")
    metro.add_column("Modello")
    metro.add_column("Campioni", justify="right")
    metro.add_column("Scarto medio", justify="right")
    metro.add_column("Intervallo", justify="right")
    for riga in taratura:
        medio = riga.get("scarto_medio") or 0.0
        minimo = riga.get("scarto_min") or 0.0
        massimo = riga.get("scarto_max") or 0.0
        stile = "green" if abs(medio) < 0.05 and (massimo - minimo) < 0.15 else "yellow"
        metro.add_row(
            riga["model"],
            f"{int(riga['campioni']):,}",
            f"[{stile}]{medio * 100:+.1f}%[/]",
            f"{minimo * 100:+.1f}% .. {massimo * 100:+.1f}%",
        )
    console.print(metro)


def _stampa_scritture(conto: dict) -> None:
    """Quante scritture in cache nessuna richiesta successiva ha riletto.

    Una scrittura costa 1,25x o 2x e una rilettura 0,1x: riletta una volta e'
    gia' in guadagno, mai riletta e' una perdita netta. Le due categorie di
    spreco restano separate perche' solo una delle due dipende da una
    decisione del gateway.
    """
    if not conto.get("scritture"):
        return
    tabella = Table(title="Scritture in cache mai rilette")
    tabella.add_column("Voce")
    tabella.add_column("Valore", justify="right")
    tabella.add_row("Token scritti", f"{int(conto['token_scritti']):,}")
    tabella.add_row("  ripagati da una rilettura", f"{int(conto['token_recuperati']):,}")

    in_mezzo = int(conto["token_sprecati_in_mezzo"])
    stile = "green" if in_mezzo == 0 else "yellow"
    tabella.add_row(
        "  orfani in mezzo (evitabili)", f"[{stile}]{in_mezzo:,}[/]"
    )
    tabella.add_row(
        "  orfani di coda (fine sessione)", f"{int(conto['token_sprecati_di_coda']):,}"
    )
    tabella.add_row("Sovrapprezzo pagato", f"${conto['costo_sprecato_usd']:.4f}")
    tabella.add_row("Sessioni osservate", f"{int(conto['sessioni']):,}")
    console.print(tabella)
    if in_mezzo:
        console.print(
            "[dim]Gli orfani \"in mezzo\" sono scritture che altre richieste hanno "
            "seguito senza mai rileggerle: di solito e' un confine di potatura che "
            "avanza troppo spesso. Vedi `ecotokens cachewrites`.[/]"
        )


@app.command()
def purge(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    everything: bool = typer.Option(False, "--everything", help="Svuota le cache, non solo le voci scadute"),
) -> None:
    """Pulisce le cache scadute e compatta il registro dei consumi."""
    settings = load_settings(config)

    async def _purge():
        database = Database(settings.storage.path)
        database.connect()
        store = Store(database)
        try:
            # La compattazione va fatta in ogni caso: il registro dei consumi
            # cresce con una riga per richiesta, e senza questo nessun comando
            # lo rimpicciolisce mai. I totali non ne risentono - vengono
            # aggregati prima di cancellare - ma il dettaglio dei giorni
            # vecchi sparisce, quindi lo si dice.
            compattati = await store.compatta_consumi(settings.storage.keep_detail_days)
            if everything:
                await store.clear_caches()
                return None, compattati
            return await store.prune_cache(settings.exact_cache.max_entries), compattati
        finally:
            database.close()

    total, compattati = asyncio.run(_purge())
    if compattati["compattate"]:
        console.print(
            f"[green]Consumi compattati:[/] {compattati['compattate']:,} richieste "
            f"di {compattati['giorni']} giorni prima del {compattati['confine']} "
            "ridotte a un riepilogo giornaliero."
        )
        console.print(
            "[dim]I totali di costo e risparmio restano identici; spariscono "
            "latenza, note e attribuzione per stadio di quei giorni.[/]"
        )
    if total is None:
        console.print("[green]Cache svuotate.[/]")
    else:
        console.print(f"[green]Pulizia completata.[/] Voci presenti prima della potatura: {total}")


@app.command()
def bench(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
    scenario: Optional[list[str]] = typer.Option(
        None,
        "--scenario",
        help="Limita a uno o piu' scenari (ripetibile). Senza, li esegue tutti.",
    ),
    label: str = typer.Option("misura", help="Etichetta della misura nello storico"),
    save: bool = typer.Option(True, help="Registra l'esito per il confronto nel tempo"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non chiedere conferma in modalita' live"),
) -> None:
    """Misura lo stesso carico con e senza gateway.

    Con ``--live`` la misura gira contro l'API vera e spende. Conviene partire
    da un solo scenario (``--scenario chat``) per calibrare a pochi centesimi lo
    scarto fra il simulatore e la realta', e decidere dopo se vale la spesa piena.
    """
    from .bench import (
        BASELINE_VARIANT,
        CORPUS_VERSION,
        FULL_VARIANT,
        open_results_store,
        run_benchmark,
        save_run,
    )
    from .workloads import all_scenarios, scenarios_by_name

    settings = load_settings(config)
    radice = Path.cwd()

    if scenario:
        try:
            scelti = scenarios_by_name(scenario, radice)
        except ValueError as errore:
            console.print(f"[red]{errore}[/]")
            raise typer.Exit(code=2)
        # Corpus distinto: confrontare un sottoinsieme con la serie completa
        # mostrerebbe progressi immaginari, perche' cambia il denominatore.
        corpus = f"sottoinsieme ({','.join(sorted(s.name for s in scelti))}) {CORPUS_VERSION}"
    else:
        scelti = all_scenarios(radice)
        corpus = f"scenari standard {CORPUS_VERSION}"

    richieste = sum(s.size for s in scelti) * 2  # riferimento + variante
    if live:
        esigi_credenziali()
        console.print("[yellow]Modalita' live: questa misura consuma token veri.[/]")
        console.print(
            f"Scenari: [cyan]{', '.join(s.name for s in scelti)}[/] - "
            f"[bold]{richieste}[/] richieste all'API."
        )
        if not yes and not typer.confirm("Procedo?", default=False):
            console.print("Annullato.")
            raise typer.Exit(code=1)

    async def _esegui():
        run = await run_benchmark(
            scenarios=scelti, label=label, live=live, project_root=radice
        )
        if save:
            database, store = open_results_store(settings.storage.path)
            try:
                await save_run(store, run, corpus=corpus)
            finally:
                database.close()
        return run

    run = asyncio.run(_esegui())

    tabella = Table(title=f"Con e senza gateway - {run.label} ({run.mode})")
    tabella.add_column("Scenario")
    tabella.add_column("Richieste", justify="right")
    tabella.add_column("Senza", justify="right")
    tabella.add_column("Con", justify="right")
    tabella.add_column("Risparmio", justify="right")
    tabella.add_column("Da cache", justify="right")

    for confronto in run.comparisons:
        stile = "green" if confronto.saved_ratio > 0 else "red"
        tabella.add_row(
            confronto.scenario,
            str(confronto.before.requests),
            f"${confronto.before.cost_usd:.4f}",
            f"${confronto.after.cost_usd:.4f}",
            f"[{stile}]{confronto.saved_ratio * 100:+.1f}%[/]",
            f"{confronto.after.cache_ratio * 100:.0f}%",
        )

    prima = run.totals(BASELINE_VARIANT)
    dopo = run.totals(FULL_VARIANT)
    quota = (prima.cost_usd - dopo.cost_usd) / prima.cost_usd if prima.cost_usd else 0
    tabella.add_section()
    tabella.add_row(
        "[bold]totale[/]",
        str(prima.requests),
        f"[bold]${prima.cost_usd:.4f}[/]",
        f"[bold]${dopo.cost_usd:.4f}[/]",
        f"[bold green]{quota * 100:+.1f}%[/]",
        f"{dopo.cache_ratio * 100:.0f}%",
    )
    console.print(tabella)
    if not live:
        console.print()
        console.print()
        console.print(
            "[dim]Misura simulata: la meccanica della cache e' fedele, i conteggi di "
            "token sono proporzionali. Per numeri reali: --live.[/]"
        )


@app.command()
def ablate(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
    save: bool = typer.Option(True, help="Registra l'esito per il confronto nel tempo"),
) -> None:
    """Attribuisce il risparmio a ciascuno stadio, accendendoli uno alla volta."""
    if live:
        esigi_credenziali()
    from .bench import (
        BASELINE_VARIANT,
        CORPUS_VERSION,
        guadagno_sul_caching_automatico,
        open_results_store,
        run_ablation,
        save_run,
        stage_contributions,
    )

    settings = load_settings(config)

    async def _esegui():
        run = await run_ablation(live=live, project_root=Path.cwd())
        if save:
            database, store = open_results_store(settings.storage.path)
            try:
                await save_run(store, run, corpus=f"scenari standard {CORPUS_VERSION}")
            finally:
                database.close()
        return run

    run = asyncio.run(_esegui())
    riferimento = run.totals(BASELINE_VARIANT)

    tabella = Table(title=f"Contributo di ogni stadio (riferimento ${riferimento.cost_usd:.4f})")
    tabella.add_column("Stadio")
    tabella.add_column("Contributo", justify="right")
    tabella.add_column("Quota", justify="right")
    tabella.add_column("Cumulato", justify="right")
    for voce in stage_contributions(run):
        stile = "green" if voce["saved_usd"] > 0 else "red" if voce["saved_usd"] < 0 else "dim"
        tabella.add_row(
            voce["stage"],
            f"[{stile}]${voce['saved_usd']:+.4f}[/]",
            f"{voce['saved_ratio'] * 100:+.1f}%",
            f"{voce['cumulative_ratio'] * 100:.1f}%",
        )
    console.print(tabella)

    # La scala qui sopra parte da "nessuna cache", che non e' piu' il punto di
    # partenza di nessuno: il caching automatico e' gratis. Chi sta valutando
    # se installare il gateway ha bisogno dell'altra domanda, e va risposta
    # senza costringerlo a fare la sottrazione da solo.
    guadagno = guadagno_sul_caching_automatico(run)
    confronto = Table(
        title=(
            "Quanto aggiunge a chi usa gia' il caching automatico "
            f"(riferimento ${guadagno['reference_usd']:.4f})"
        )
    )
    confronto.add_column("Carico")
    confronto.add_column("Con sola cache", justify="right")
    confronto.add_column("Dietro EcoTokens", justify="right")
    confronto.add_column("In meno", justify="right")
    for voce in guadagno["by_scenario"]:
        confronto.add_row(
            voce["scenario"],
            f"${voce['reference_usd']:.4f}",
            f"${voce['cost_usd']:.4f}",
            f"[green]{voce['saved_ratio'] * 100:.1f}%[/]",
        )
    senza = guadagno["senza_cambiare_la_risposta"]
    confronto.add_row(
        "[bold]totale[/]",
        f"[bold]${guadagno['reference_usd']:.4f}[/]",
        f"[bold]${senza['cost_usd']:.4f}[/]",
        f"[bold green]{senza['saved_ratio'] * 100:.1f}%[/]",
    )
    console.print(confronto)
    cambiando = guadagno["cambiando_la_risposta"]
    console.print(
        f"[dim]Senza toccare il contenuto delle risposte. Accendendo declassamento "
        f"ed effort minimo si arriva a {cambiando['saved_ratio'] * 100:.1f}%, ma "
        "quella e' un'altra risposta a un prezzo diverso: il banco misura quanto "
        "e' lunga, non se e' giusta.[/]"
    )


@app.command()
def ritenzione(
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Verifica se l'informazione che servira' sopravvive fino al prompt."""
    if live:
        esigi_credenziali()
    from .retention import VARIANTI, misura_ritenzione

    console.print(
        "[dim]Non misura se la risposta e' giusta - servirebbe un modello, e un "
        "modello che ne giudica un altro e' un metro con opinioni. Misura la "
        "domanda piu' piccola e deterministica che ci sta dentro: l'informazione "
        "necessaria e' arrivata fino al prompt? Se non c'e', nessun modello puo' "
        "rispondere.[/]\n"
    )
    settings = load_settings(None)

    async def _misura():
        esiti = await misura_ritenzione(live=live)
        database = Database(settings.storage.path)
        database.connect()
        try:
            await Store(database).save_retention(esiti)
        finally:
            database.close()
        return esiti

    esiti = asyncio.run(_misura())

    tabella = Table(title="Ritenzione: i fatti piantati sono ancora nel prompt?")
    tabella.add_column("Carico")
    tabella.add_column("Configurazione")
    tabella.add_column("Ritenzione", justify="right")
    tabella.add_column("Token", justify="right")
    tabella.add_column("Riassunti nuovi", justify="right")
    tabella.add_column("Perduti")
    for voce in esiti:
        quota = voce.quota
        stile = "green" if quota == 1 else "red" if quota == 0 else "yellow"
        tabella.add_row(
            voce.scenario,
            voce.variante,
            f"[{stile}]{quota * 100:.0f}%[/]",
            f"{voce.prompt_tokens:,}",
            str(voce.riassunti_nuovi),
            ", ".join(voce.perduti) or "[dim]-[/]",
        )
    console.print(tabella)

    for nome, descrizione in VARIANTI:
        console.print(f"[dim]  {nome}: {descrizione}[/]")
    console.print(
        "\n[yellow]I token non sono confrontabili fra varianti potate.[/] Due "
        "esecuzioni possono trovarsi in punti diversi del ciclo di compattazione, "
        "e chi riassume un turno prima ha un prompt molto piu' corto per una "
        "ragione che non c'entra con lo stadio in esame - guardare la colonna dei "
        "riassunti nuovi. La colonna che regge il confronto e' la ritenzione."
    )
    if not live:
        console.print(
            "[dim]Con il simulatore l'estrattore di memoria e' perfetto per "
            "ipotesi: i fatti entrano nel deposito senza passare da un modello. "
            "Il numero della memoria e' quindi un limite superiore - dice se un "
            "fatto estratto arriva al prompt, non se l'estrazione lo avrebbe "
            "trovato. Quella meta' si misura solo con --live.[/]"
        )


@app.command()
def memoria() -> None:
    """Confronta le due modalita' di recupero della memoria, sul costo."""
    from .retention import misura_memoria

    esiti = asyncio.run(misura_memoria())
    per: dict[int, dict[str, object]] = {}
    for voce in esiti:
        per.setdefault(voce.turni, {})[voce.modalita] = voce

    tabella = Table(title="Memoria: fatti in coda contro fatti nel prefisso in cache")
    tabella.add_column("Turni", justify="right")
    tabella.add_column("In coda (1x sempre)", justify="right")
    tabella.add_column("Nel prefisso (0,1x dopo)", justify="right")
    tabella.add_column("Differenza", justify="right")
    for turni in sorted(per):
        coda = per[turni]["pertinente"]
        prefisso = per[turni]["stabile"]
        delta = (coda.cost_usd - prefisso.cost_usd) / coda.cost_usd if coda.cost_usd else 0.0
        stile = "green" if delta > 0 else "red" if delta < 0 else "dim"
        tabella.add_row(
            str(turni),
            f"${coda.cost_usd:.5f}",
            f"${prefisso.cost_usd:.5f}",
            f"[{stile}]{delta * 100:+.1f}%[/]",
        )
    console.print(tabella)
    console.print(
        "[dim]La potatura resta spenta di proposito: accendendola le due esecuzioni "
        "finiscono in punti diversi del ciclo di compattazione e la differenza di "
        "costo diventa illeggibile.[/]"
    )
    console.print(
        "\n[yellow]Sul costo il prefisso perde[/], ed e' comunque il default. "
        "L'ipotesi di partenza diceva +21% e la misura dice il contrario: il blocco "
        "e' piccolo e la scrittura in cache si paga 1,25x. Vince su un altro asse - "
        "il recupero per pertinenza e' lessicale, e su fatti scritti telegrafici non "
        "trova niente. Vedi [cyan]ecotokens ritenzione[/], scenario parole-diverse."
    )


@app.command()
def streaming(
    scenario: str = typer.Option("chat", help="Quale carico del corpus usare"),
) -> None:
    """Verifica che il risparmio sia lo stesso quando la risposta arriva a pezzi."""
    from .bench import measure_streaming

    esiti = asyncio.run(measure_streaming(scenario))

    tabella = Table(title=f"Lo stesso carico ({scenario}) servito nei due modi")
    tabella.add_column("Modalita'")
    tabella.add_column("Richieste", justify="right")
    tabella.add_column("Token di prompt", justify="right")
    tabella.add_column("Da cache", justify="right")
    tabella.add_column("Costo", justify="right")
    for voce in esiti:
        tabella.add_row(
            voce.modalita,
            f"{voce.requests:,}",
            f"{voce.prompt_tokens:,}",
            f"{voce.cache_read_tokens:,}",
            f"${voce.cost_usd:.5f}",
        )
    console.print(tabella)

    if len(esiti) == 2 and esiti[0].cost_usd:
        scarto = abs(esiti[1].cost_usd - esiti[0].cost_usd) / esiti[0].cost_usd
        stile = "green" if scarto < 0.01 else "yellow"
        console.print(f"[{stile}]Scarto: {scarto * 100:.2f}%[/]")
    console.print(
        "[dim]Il percorso in streaming vive nella rotta HTTP, non in "
        "Gateway.complete: il resto del banco non lo tocca, e per questo il "
        "corpus non ne conteneva nemmeno una richiesta su cinquantuno - mentre "
        "la maggior parte delle interfacce di chat trasmette.[/]"
    )


@app.command()
def compaction(
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Confronta le strategie di compattazione su una conversazione lunga."""
    if live:
        esigi_credenziali()
    from .bench import measure_compaction

    esiti = asyncio.run(measure_compaction(live=live))
    riferimento = esiti[0].cost_usd

    tabella = Table(title="Compattazione del contesto: conviene, e a quali condizioni")
    tabella.add_column("Strategia")
    tabella.add_column("Costo", justify="right")
    tabella.add_column("di cui riassunti", justify="right")
    tabella.add_column("Quota da cache", justify="right")
    tabella.add_column("Riassunti", justify="right")
    tabella.add_column("vs non comprimere", justify="right")
    for voce in esiti:
        delta = (riferimento - voce.cost_usd) / riferimento if riferimento else 0.0
        stile = "green" if delta > 0 else "red" if delta < 0 else "dim"
        tabella.add_row(
            voce.name,
            f"${voce.cost_usd:.4f}",
            f"${voce.aux_cost_usd:.4f}",
            f"{voce.cache_ratio * 100:.1f}%",
            str(voce.summaries),
            f"[{stile}]{delta * 100:+.1f}%[/]",
        )
    console.print(tabella)
    console.print(
        "\n[dim]Il numero di riassunti misura la stabilita' del prefisso: uno per turno "
        "significa un prompt nuovo per turno, quindi cache mai riletta.[/]"
    )


@app.command()
def optimize(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Prova piu' configurazioni e consiglia quella che ha speso meno."""
    from .bench import CORPUS_VERSION, open_results_store, run_sweep, save_run

    settings = load_settings(config)
    esiti, run = asyncio.run(run_sweep(live=live, project_root=Path.cwd()))

    async def _registra():
        database, store = open_results_store(settings.storage.path)
        try:
            await save_run(store, run, corpus=f"ricerca configurazione {CORPUS_VERSION}")
        finally:
            database.close()

    asyncio.run(_registra())

    tabella = Table(title="Configurazioni provate, dalla piu' economica")
    tabella.add_column("Configurazione")
    tabella.add_column("Costo", justify="right")
    tabella.add_column("Risparmio", justify="right")
    tabella.add_column("Da cache", justify="right")
    for posizione, voce in enumerate(esiti):
        stile = "bold green" if posizione == 0 else ""
        tabella.add_row(
            f"[{stile}]{voce.name}[/]" if stile else voce.name,
            f"${voce.cost_usd:.4f}",
            f"{voce.saved_ratio * 100:+.1f}%",
            f"{voce.cache_ratio * 100:.0f}%",
        )
    console.print(tabella)

    migliore = esiti[0]
    console.print()
    console.print(f"[bold green]Consigliata:[/] {migliore.name}")
    if migliore.name != "predefinita":
        predefinita = next(v for v in esiti if v.name == "predefinita")
        guadagno = predefinita.cost_usd - migliore.cost_usd
        console.print(
            f"Spende ${guadagno:.4f} in meno della configurazione attuale "
            f"({guadagno / predefinita.cost_usd * 100:.1f}%)."
        )
    else:
        console.print("La configurazione predefinita e' gia' la migliore fra quelle provate.")


@app.command()
def prompt(
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Misura i livelli di riscrittura del prompt."""
    if live:
        esigi_credenziali()
    from .bench import measure_prompt_optimization

    esiti = asyncio.run(measure_prompt_optimization(live=live))
    origine = esiti[0].cost_usd

    tabella = Table(title="Accorciare il prompt: quanto vale, e dove")
    tabella.add_column("Livello")
    tabella.add_column("Costo", justify="right")
    tabella.add_column("Token tolti", justify="right")
    tabella.add_column("fuori cache", justify="right")
    tabella.add_column("Da cache", justify="right")
    tabella.add_column("vs originale", justify="right")
    for voce in esiti:
        delta = (origine - voce.cost_usd) / origine if origine else 0.0
        stile = "green" if delta > 0 else "red" if delta < 0 else "dim"
        nome = voce.name if voce.validated else f"{voce.name} [yellow](non validato)[/]"
        tabella.add_row(
            nome,
            f"${voce.cost_usd:.4f}",
            f"{voce.tokens_removed:,}",
            f"{voce.tokens_removed_uncached:,}",
            f"{voce.cache_ratio * 100:.1f}%",
            f"[{stile}]{delta * 100:+.1f}%[/]",
        )
    console.print(tabella)

    # La resa si legge sull'ultimo livello validato, non sul migliore: prendere
    # il massimo premierebbe la variante che ha tolto pochi token, e farebbe
    # sembrare la resa il doppio di quella che e'.
    validati = [v for v in esiti if v.validated and v.tokens_removed]
    resa = (
        (origine - validati[-1].cost_usd) / validati[-1].tokens_removed * 1000
        if validati
        else 0.0
    )
    console.print()
    console.print(
        f"[dim]Resa: ${resa:.5f} ogni mille token tolti, contro $0.00500 di prezzo "
        "pieno dell'input su Opus 5. La differenza e' lo sconto che la cache aveva "
        "gia' fatto su quei token: accorciare il prompt rende circa un quarto di "
        "quello che sembra.[/]"
    )


@app.command()
def substitutions(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    live: bool = typer.Option(
        False, "--live", help="Interroga messages.count_tokens (richiede credenziali)"
    ),
    model: str = typer.Option("claude-opus-5", help="Modello su cui contare"),
) -> None:
    """Verifica quali sinonimi piu' corti costano davvero meno token.

    Senza --live mostra soltanto i candidati e il loro stato: il conteggio dei
    token non e' deducibile a mano, il tokenizer di Claude non e' pubblico e
    l'unica autorita' e' l'API.
    """
    from .prompt_opt import SUBSTITUTIONS

    settings = load_settings(config)

    async def _verifica():
        database = Database(settings.storage.path)
        database.connect()
        store = Store(database)
        try:
            if live:
                import anthropic

                from .pricing import resolve_model

                client = anthropic.AsyncAnthropic(
                    default_headers=intestazioni_upstream() or None
                )
                bersaglio = resolve_model(model)
                for voce in SUBSTITUTIONS:
                    prima = await _conta(client, bersaglio, voce.source)
                    dopo = await _conta(client, bersaglio, voce.target)
                    await store.record_substitution_check(
                        source=voce.source,
                        target=voce.target,
                        model=bersaglio,
                        tokens_before=prima,
                        tokens_after=dopo,
                    )
            return await store.substitution_report()
        finally:
            database.close()

    report = asyncio.run(_verifica())

    tabella = Table(title=f"Sostituzioni lessicali ({len(SUBSTITUTIONS)} candidati)")
    tabella.add_column("Originale")
    tabella.add_column("Sostituto")
    tabella.add_column("Token", justify="right")
    tabella.add_column("Esito")

    per_sorgente = {riga["source"]: riga for riga in report}
    for voce in SUBSTITUTIONS:
        riga = per_sorgente.get(voce.source)
        if riga is None:
            tabella.add_row(voce.source, voce.target, "-", "[dim]non verificato[/]")
            continue
        delta = riga["tokens_before"] - riga["tokens_after"]
        if riga["verified"]:
            esito = f"[green]conferma: -{delta}[/]"
        else:
            esito = f"[red]scartata: {delta:+d}[/]"
        tabella.add_row(
            voce.source,
            voce.target,
            f"{riga['tokens_before']} -> {riga['tokens_after']}",
            esito,
        )
    console.print(tabella)

    if not live:
        console.print()
        console.print(
            "[yellow]Nessuna verifica eseguita.[/] I candidati accorciano il testo in "
            "caratteri, ma se accorcino anche in token lo dice solo il tokenizer vero."
        )
        console.print("Per interpellarlo:  [cyan]ecotokens substitutions --live[/]")
        console.print(
            "[dim]Finche' non sono verificate, lo stadio non le applica "
            "(prompt.only_verified = true).[/]"
        )


async def _conta(client, model: str, testo: str) -> int:
    """Token di un frammento, secondo l'API."""
    risposta = await client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": testo}],
    )
    return int(risposta.input_tokens)



@app.command()
def cachekey(
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Misura quanto vale normalizzare il testo prima di calcolare la chiave."""
    if live:
        esigi_credenziali()
    from .bench import measure_cache_key

    esiti = asyncio.run(measure_cache_key(live=live))

    tabella = Table(title="Chiave della cache esatta: byte grezzi o testo normalizzato")
    tabella.add_column("Carico")
    tabella.add_column("Chiave")
    tabella.add_column("Costo", justify="right")
    tabella.add_column("Hit", justify="right")
    tabella.add_column("Chiamate API", justify="right")
    for voce in esiti:
        tabella.add_row(
            voce.scenario,
            voce.key_kind,
            f"${voce.cost_usd:.4f}",
            f"{voce.hits}/{voce.requests}",
            str(voce.upstream_calls),
        )
    console.print(tabella)
    console.print()
    console.print(
        "[dim]E' l'ottimizzazione con la resa piu' alta del gateway: ogni altra leva "
        "sconta il prezzo di un token, un hit di cache lo azzera.[/]"
    )


@app.command()
def cachewrites(
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Conta le scritture in cache che nessuna richiesta successiva rilegge."""
    if live:
        esigi_credenziali()
    from .bench import measure_cache_writes

    esiti = asyncio.run(measure_cache_writes(live=live))

    tabella = Table(title="Scritture in cache: quante vengono davvero rilette")
    tabella.add_column("Tetto", no_wrap=True)
    tabella.add_column("Costo", justify="right", no_wrap=True)
    tabella.add_column("Scritti", justify="right", no_wrap=True)
    tabella.add_column("Ripagati", justify="right", no_wrap=True)
    tabella.add_column("Orfani\nin mezzo", justify="right", no_wrap=True)
    tabella.add_column("Orfani\ndi coda", justify="right", no_wrap=True)
    tabella.add_column("Extra", justify="right", no_wrap=True)

    migliore = min((v.cost_usd for v in esiti if v.breakpoints), default=0.0)
    for voce in esiti:
        conto = voce.audit
        costo = f"${voce.cost_usd:.4f}"
        if voce.breakpoints and abs(voce.cost_usd - migliore) < 1e-9:
            costo = f"[green]{costo}[/]"
        in_mezzo = str(conto.token_sprecati_in_mezzo)
        if conto.token_sprecati_in_mezzo:
            in_mezzo = f"[yellow]{in_mezzo}[/]"
        tabella.add_row(
            voce.etichetta,
            costo,
            f"{conto.token_scritti:,}".replace(",", " "),
            f"{conto.token_recuperati:,}".replace(",", " "),
            in_mezzo,
            str(conto.token_sprecati_di_coda),
            f"${conto.costo_sprecato_usd:.5f}",
        )
    console.print(tabella)
    console.print()
    console.print(
        "[dim]Le due colonne vanno lette insieme, e in quest'ordine: il costo prima, "
        "lo spreco poi. Lo spreco da solo si azzera spegnendo il pianificatore, che e' "
        "la riga piu' cara del gruppo.[/]"
    )
    console.print(
        "[dim]\"In mezzo\" e' l'unica quota su cui si possa intervenire: e' una scrittura "
        "che altre richieste hanno seguito senza rileggerla. \"Di coda\" e' l'ultima "
        "scrittura di una sessione, che nessuno poteva sapere fosse l'ultima - e che "
        "un'altra sessione con lo stesso prefisso potrebbe ancora rileggere.[/]"
    )


@app.command()
def ceiling(
    goal: float = typer.Option(
        99.0, "--goal", help="Risparmio da verificare, in percentuale"
    ),
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Dice fin dove puo' arrivare il risparmio, e cosa lo ferma."""
    if live:
        esigi_credenziali()
    from .ceiling import (
        measure_ceiling,
        measure_repetition_curve,
        ripetizioni_per_obiettivo,
    )

    obiettivo = max(0.0, min(0.9999, goal / 100.0))
    report = asyncio.run(measure_ceiling(live=live))

    scala = Table(title="Fin dove si arriva, accendendo una leva alla volta")
    scala.add_column("Leva")
    scala.add_column("Costo", justify="right", no_wrap=True)
    scala.add_column("Risparmio", justify="right", no_wrap=True)
    scala.add_column("In cambio di")
    for passo in report.steps:
        quota = passo.saved_ratio(report.baseline_usd)
        scala.add_row(
            passo.etichetta,
            f"${passo.cost_usd:.4f}",
            f"[green]{quota * 100:.1f}%[/]" if passo.sicura else f"{quota * 100:.1f}%",
            "[dim]niente che non sia gia' misurato[/]" if passo.sicura else passo.in_cambio,
        )
    console.print(scala)
    console.print()

    pavimento = report.floor
    if pavimento is None:
        return

    tetto = Table(title="Il pavimento: cio' che nessuna configurazione toglie")
    tetto.add_column("Voce")
    tetto.add_column("Costo", justify="right", no_wrap=True)
    tetto.add_column("Perche' resta")
    tetto.add_row(
        "Output generato",
        f"${pavimento.output_usd:.4f}",
        "nessuna cache lo sconta: non esisteva prima della richiesta",
    )
    tetto.add_row(
        "Input mai visto",
        f"${pavimento.input_nuovo_usd:.4f}",
        "contenuto nuovo, va trasmesso almeno una volta",
    )
    tetto.add_row(
        "Riletture da cache",
        f"${pavimento.riletture_usd:.4f}",
        "gia' scontate a 0,1x, ma non gratuite",
    )
    tetto.add_row("[bold]Totale[/]", f"[bold]${pavimento.totale_usd:.4f}[/]", "")
    console.print(tetto)
    console.print()

    massimo = report.tetto_teorico()
    tetto_spesa = report.baseline_usd * (1.0 - obiettivo)
    if report.raggiungibile(obiettivo):
        console.print(
            f"[green]Il {goal:.1f}% e' compatibile con il pavimento[/] "
            f"(servono <= ${tetto_spesa:.4f}, il pavimento e' ${pavimento.totale_usd:.4f})."
        )
    else:
        console.print(
            f"[yellow]Il {goal:.1f}% non e' raggiungibile su questo corpus.[/] "
            f"Servirebbero <= ${tetto_spesa:.4f}, ma il pavimento e' "
            f"${pavimento.totale_usd:.4f}: {pavimento.totale_usd / tetto_spesa:.1f} volte tanto."
        )
    console.print(f"Massimo teorico: [bold]{massimo * 100:.1f}%[/]")
    console.print()

    punti = asyncio.run(measure_repetition_curve(live=live))
    curva = Table(title="Il risparmio dipende dal traffico, non dal gateway")
    curva.add_column("Carico")
    curva.add_column("Richieste", justify="right", no_wrap=True)
    curva.add_column("Senza", justify="right", no_wrap=True)
    curva.add_column("Con", justify="right", no_wrap=True)
    curva.add_column("Risparmio", justify="right", no_wrap=True)
    for punto in punti:
        raggiunto = punto.saved_ratio >= obiettivo
        quota = f"{punto.saved_ratio * 100:.1f}%"
        curva.add_row(
            f"{punto.uniche} domande x{punto.ripetizioni}",
            str(punto.richieste),
            f"${punto.baseline_usd:.4f}",
            f"${punto.cost_usd:.4f}",
            f"[green]{quota}[/]" if raggiunto else quota,
        )
    console.print(curva)
    console.print()

    necessarie = ripetizioni_per_obiettivo(punti, obiettivo)
    if necessarie:
        console.print(
            f"[dim]Su richieste tutte uguali il {goal:.1f}% arriva a circa "
            f"{necessarie} ripetizioni: la cache esatta non sconta il prezzo di un "
            f"token, lo azzera. La prima richiesta pero' si paga sempre, quindi la "
            f"curva sale verso il 100% senza toccarlo.[/]"
        )
    console.print(
        "[dim]Il numero di testa della dashboard e' quello del corpus standard, che "
        "mescola carichi ripetitivi e carichi tutti diversi. Alzarlo aggiungendo "
        "ripetizioni al corpus si puo' fare in dieci minuti, e non misurerebbe piu' "
        "niente.[/]"
    )


@app.command()
def overhead() -> None:
    """Mostra il testo che il gateway aggiunge di suo ai prompt."""
    from .bench import gateway_overhead

    dati = gateway_overhead()
    totali = dati["totals"]

    tabella = Table(title="Testo aggiunto dal gateway, per occorrenza")
    tabella.add_column("Voce")
    tabella.add_column("Scopo")
    tabella.add_column("Prima", justify="right")
    tabella.add_column("Adesso", justify="right")
    tabella.add_column("Variazione", justify="right")
    for voce in dati["items"]:
        stile = "green" if voce["saved"] > 0 else "red" if voce["saved"] < 0 else "dim"
        tabella.add_row(
            voce["key"],
            voce["purpose"],
            str(voce["before"]),
            str(voce["after"]),
            f"[{stile}]{voce['saved']:+d}[/]",
        )
    tabella.add_section()
    quota = totali["saved"] / totali["before"] if totali["before"] else 0.0
    tabella.add_row(
        "[bold]totale[/]", "", str(totali["before"]), f"[bold]{totali['after']}[/]",
        f"[bold green]{totali['saved']:+d} ({quota * 100:.0f}%)[/]",
    )
    console.print(tabella)
    console.print()
    console.print(
        "[dim]Token per occorrenza, non per richiesta: sul totale di una fattura "
        "incide poco. Fatto perche' e' gratis e senza rischio, non perche' sposti l'ago.[/]"
    )



@app.command()
def pruning(
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Confronta le strategie di potatura del contesto."""
    if live:
        esigi_credenziali()
    from .bench import measure_pruning

    esiti = asyncio.run(measure_pruning(live=live, project_root=Path.cwd()))

    tabella = Table(title="Potatura del contesto: dove sta il confine")
    tabella.add_column("Carico")
    tabella.add_column("Strategia")
    tabella.add_column("Costo", justify="right")
    tabella.add_column("Da cache", justify="right")
    tabella.add_column("vs nessuna potatura", justify="right")
    scorso = None
    for voce in esiti:
        if scorso is not None and voce.scenario != scorso:
            tabella.add_section()
        scorso = voce.scenario
        stile = "green" if voce.delta_ratio > 0.001 else "red" if voce.delta_ratio < -0.001 else "dim"
        tabella.add_row(
            voce.scenario,
            voce.name,
            f"${voce.cost_usd:.4f}",
            f"{voce.cache_ratio * 100:.1f}%",
            f"[{stile}]{voce.delta_ratio * 100:+.1f}%[/]",
        )
    console.print(tabella)
    console.print()
    console.print(
        "[dim]Lo scatto si misura in turni, non in risultati: sei chiamate per turno "
        "ne consumano sei volte piu' in fretta di una.[/]"
    )



@app.command()
def quadro(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    out: str = typer.Option("ecotokens-quadro.html", help="File HTML da generare"),
) -> None:
    """Cruscotto compatto con tutti i parametri gia' misurati."""
    from .quadro import quadro_da_percorso, render_quadro

    settings = load_settings(config)
    dati = asyncio.run(quadro_da_percorso(settings))
    percorso = Path(out)
    percorso.write_text(render_quadro(dati), encoding="utf-8")
    console.print(f"[green]Quadro scritto in[/] {percorso.resolve()}")
    console.print(
        "[dim]Non misura niente: legge cio' che e' gia' registrato. I riquadri "
        "senza dati dicono quale comando li produce.[/]"
    )


@app.command()
def dashboard(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    out: str = typer.Option("ecotokens-dashboard.html", help="File HTML da generare"),
    measure: bool = typer.Option(
        True, help="Esegue misura e ablazione prima di generare (piu' lento, dati freschi)"
    ),
) -> None:
    """Genera la dashboard HTML con tutti i parametri misurati."""
    from .dashboard import build_dashboard_data, render_dashboard

    settings = load_settings(config)

    async def _raccogli():
        return await build_dashboard_data(settings, measure=measure, project_root=Path.cwd())

    console.print("Raccolta delle misure in corso...")
    data = asyncio.run(_raccogli())
    percorso = Path(out)
    percorso.write_text(render_dashboard(data), encoding="utf-8")
    console.print(f"[green]Dashboard scritta in[/] {percorso.resolve()}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
