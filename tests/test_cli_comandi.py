"""Test dei comandi della riga di comando.

Nascono da un difetto reale: `ecotokens optimize` sollevava `NameError` sul
nome `CORPUS_VERSION`, mai importato. L'errore stava in fondo al comando, dopo
la misura, quindi si manifestava solo dopo due minuti di lavoro andato perso -
e nessun test lo copriva, perche' testare quei comandi significava eseguire
l'intero banco.

La via d'uscita e' sostituire la misura con una finta: cosi' resta esercitato
tutto il codice *attorno*, che e' dove vivono gli errori di questo tipo.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ecotokens import cli

runner = CliRunner()

# Ricavati dall'app, non scritti a mano. La lista precedente era una copia, e
# come tutte le copie era invecchiata: mancavano sette comandi, fra cui tre
# aggiunti lo stesso giorno in cui il test avrebbe dovuto coprirli. Un elenco
# che non si aggiorna da solo copre cio' che c'era, non cio' che c'e'.
COMANDI = sorted(
    comando.name or comando.callback.__name__.replace("_", "-")
    for comando in cli.app.registered_commands
)


def test_l_elenco_dei_comandi_non_e_vuoto():
    """Se l'introspezione cambiasse forma, i test parametrizzati sparirebbero
    in silenzio e la copertura andrebbe a zero senza che nulla diventi rosso."""
    assert len(COMANDI) >= 20, COMANDI


@pytest.mark.parametrize("nome", COMANDI)
def test_ogni_comando_ha_un_aiuto(nome):
    """Intercetta le firme malformate, che typer rifiuta alla costruzione."""
    esito = runner.invoke(cli.app, [nome, "--help"])
    assert esito.exit_code == 0, esito.output


# Comandi che leggono soltanto: si possono eseguire davvero, senza rete, senza
# spesa e senza toccare niente. Sono anche quelli che un utente prova per
# primi, ed e' li' che un errore di collegamento fa la figura peggiore.
SOLA_LETTURA = ["assunzioni", "diagnosi", "stats", "overhead", "cachewrites"]


@pytest.mark.parametrize("nome", SOLA_LETTURA)
def test_i_comandi_di_sola_lettura_girano_davvero(nome, tmp_path, monkeypatch):
    """`--help` prova che la firma e' valida, non che il comando funzioni.

    Un import mancante dentro il corpo, un campo rinominato, una query che non
    combacia piu' con lo schema: niente di tutto questo si vede da `--help`, e
    tutto si vede alla prima riga eseguita.
    """
    monkeypatch.chdir(tmp_path)
    esito = runner.invoke(cli.app, [nome])
    # `diagnosi` esce con 1 o 2 quando trova qualcosa da segnalare - qui la
    # chiave Anthropic manca di sicuro - e non e' un errore del comando.
    assert esito.exit_code in (0, 1, 2), esito.output
    assert esito.exception is None or isinstance(esito.exception, SystemExit), (
        f"{nome} si e' rotto: {esito.exception!r}"
    )


def test_verifica_si_rifiuta_di_girare_contro_il_simulatore():
    """Una schermata di spunte verdi che non puo' fallire non e' una verifica,
    ed e' la forma di errore che questo progetto ha gia' commesso tre volte."""
    esito = runner.invoke(cli.app, ["verifica"])
    assert esito.exit_code == 2
    assert "--live" in esito.output


def test_optimize_arriva_in_fondo(monkeypatch, tmp_path):
    """La regressione: il comando deve concludersi, non solo cominciare.

    La misura vera qui non serve - serve che il codice dopo la misura esista
    davvero. Con la ricerca sostituita da una finta il test dura un istante e
    copre proprio il tratto in cui l'errore si nascondeva.
    """
    from ecotokens import bench

    run = bench.BenchRun(id="finto", label="prova", mode="simulato", created_at=0.0)

    async def finta_ricerca(**_kwargs):
        return [bench.SweepEntry(name="predefinita", cost_usd=1.0, saved_ratio=0.5,
                                 cache_ratio=0.8)], run

    monkeypatch.setattr(bench, "run_sweep", finta_ricerca)
    monkeypatch.setattr(cli, "load_settings", lambda _c: _impostazioni(tmp_path))

    esito = runner.invoke(cli.app, ["optimize"])
    assert esito.exit_code == 0, esito.output
    assert "predefinita" in esito.output


def _impostazioni(tmp_path):
    from ecotokens.config import Settings

    settings = Settings()
    settings.storage.path = str(tmp_path / "misure.db")
    return settings


# --- non si espone il gateway senza chiave --------------------------------


def test_su_localhost_parte_senza_chiave():
    """Il caso normale: la porta resta sulla macchina, non serve niente."""
    from ecotokens.cli import esigi_chiave_se_esposto

    for host in ("127.0.0.1", "localhost", "::1"):
        esigi_chiave_se_esposto(host, None)  # non deve sollevare


def test_un_indirizzo_raggiungibile_senza_chiave_ferma_l_avvio():
    """Non e' un servizio che espone dei dati: e' uno che espone una carta.

    La porta inoltra all'API con la chiave Anthropic dell'utente, quindi chi la
    trova spende a suo nome. Un avviso fra i log di avvio non lo legge nessuno,
    e il costo di sbagliare qui non e' simmetrico: si rifiuta di partire.
    """
    import typer

    from ecotokens.cli import esigi_chiave_se_esposto

    for host in ("0.0.0.0", "192.168.1.10"):
        with pytest.raises(typer.Exit) as uscita:
            esigi_chiave_se_esposto(host, None)
        assert uscita.value.exit_code == 2, host


def test_con_la_chiave_si_puo_esporre():
    """Il divieto e' sulla combinazione, non sull'indirizzo: chi ha messo una
    chiave ha fatto la scelta consapevolmente."""
    from ecotokens.cli import esigi_chiave_se_esposto

    esigi_chiave_se_esposto("0.0.0.0", "una-chiave")  # non deve sollevare


# --- la versione, in un posto solo ----------------------------------------


def test_la_versione_e_una_sola():
    """Era scritta a mano in tre punti: il pacchetto, il titolo dell'app e
    `/health`. Tre copie di un numero sono tre occasioni perche' due diventino
    vecchie senza che nessuno se ne accorga, e chi legge `/health` avrebbe
    visto una versione che il pacchetto non aveva piu'.
    """
    import tomllib
    from pathlib import Path as P

    from ecotokens import __version__

    dichiarata = tomllib.loads(P("pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == dichiarata["project"]["version"]

    sorgente = P("ecotokens/server.py").read_text(encoding="utf-8")
    assert '"0.1.0"' not in sorgente, "versione ricomparsa a mano nel server"


def test_health_riporta_la_versione_del_pacchetto(client):
    from ecotokens import __version__

    assert client.get("/health").json()["version"] == __version__


def test_il_changelog_documenta_la_versione_corrente():
    """Una versione senza una riga nel registro e' una versione che nessuno sa
    cosa contenga."""
    from pathlib import Path as P

    from ecotokens import __version__

    changelog = P("CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{__version__}]" in changelog
