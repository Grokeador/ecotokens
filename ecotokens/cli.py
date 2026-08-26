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

from .config import load_settings
from .store.db import Database
from .store.repos import Store

app = typer.Typer(add_completion=False, help="Gateway locale per Claude con economia di token")
console = Console()


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

    logging.basicConfig(
        level=settings.server.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console.print(f"[bold green]EcoTokens[/] in ascolto su [cyan]http://{host}:{port}/v1[/]")
    console.print("Nei client basta impostare questo indirizzo come base_url.\n")

    from .server import create_app

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level=settings.server.log_level,
        reload=reload,
    )


@app.command()
def stats(config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione")) -> None:
    """Mostra consumi, costi e risparmio registrati."""
    settings = load_settings(config)

    async def _collect():
        database = Database(settings.storage.path)
        database.connect()
        store = Store(database)
        try:
            return await store.stats(), await store.current_spend()
        finally:
            database.close()

    data, (today, month) = asyncio.run(_collect())

    if not data.get("requests"):
        console.print("[yellow]Nessuna richiesta registrata finora.[/]")
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
    table.add_row("Spesa di oggi", f"${today:.4f}")
    table.add_row("Spesa del mese", f"${month:.4f}")
    console.print(table)

    by_source = data.get("by_source") or []
    if by_source:
        sources = Table(title="Per origine della risposta")
        sources.add_column("Origine")
        sources.add_column("Richieste", justify="right")
        sources.add_column("Risparmio", justify="right")
        for row in by_source:
            sources.add_row(row["source"], f"{int(row['requests']):,}", f"${row['saved_usd']:.4f}")
        console.print(sources)

    by_model = data.get("by_model") or []
    if by_model:
        models = Table(title="Per modello")
        models.add_column("Modello")
        models.add_column("Richieste", justify="right")
        models.add_column("Costo", justify="right")
        for row in by_model:
            models.add_row(row["model"], f"{int(row['requests']):,}", f"${row['cost_usd']:.4f}")
        console.print(models)


@app.command()
def purge(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    everything: bool = typer.Option(False, "--everything", help="Svuota le cache, non solo le voci scadute"),
) -> None:
    """Rimuove le voci di cache scadute (o tutte, con --everything)."""
    settings = load_settings(config)

    async def _purge():
        database = Database(settings.storage.path)
        database.connect()
        store = Store(database)
        try:
            if everything:
                await store.clear_caches()
                return None
            return await store.prune_cache(settings.exact_cache.max_entries)
        finally:
            database.close()

    total = asyncio.run(_purge())
    if total is None:
        console.print("[green]Cache svuotate.[/]")
    else:
        console.print(f"[green]Pulizia completata.[/] Voci presenti prima della potatura: {total}")


@app.command()
def bench(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
    label: str = typer.Option("misura", help="Etichetta della misura nello storico"),
    save: bool = typer.Option(True, help="Registra l'esito per il confronto nel tempo"),
) -> None:
    """Misura lo stesso carico con e senza gateway."""
    from .bench import BASELINE_VARIANT, FULL_VARIANT, open_results_store, run_benchmark, save_run

    settings = load_settings(config)
    if live:
        console.print("[yellow]Modalita' live: questa misura consuma token veri.[/]")

    async def _esegui():
        run = await run_benchmark(label=label, live=live, project_root=Path.cwd())
        if save:
            database, store = open_results_store(settings.storage.path)
            try:
                await save_run(store, run, corpus="scenari standard")
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
    from .bench import BASELINE_VARIANT, open_results_store, run_ablation, save_run, stage_contributions

    settings = load_settings(config)

    async def _esegui():
        run = await run_ablation(live=live, project_root=Path.cwd())
        if save:
            database, store = open_results_store(settings.storage.path)
            try:
                await save_run(store, run, corpus="scenari standard")
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


@app.command()
def optimize(
    config: Optional[str] = typer.Option(None, help="Percorso del file di configurazione"),
    live: bool = typer.Option(False, "--live", help="Usa l'API vera invece del simulatore (spende)"),
) -> None:
    """Prova piu' configurazioni e consiglia quella che ha speso meno."""
    from .bench import open_results_store, run_sweep, save_run

    settings = load_settings(config)
    esiti, run = asyncio.run(run_sweep(live=live, project_root=Path.cwd()))

    async def _registra():
        database, store = open_results_store(settings.storage.path)
        try:
            await save_run(store, run, corpus="ricerca configurazione")
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
