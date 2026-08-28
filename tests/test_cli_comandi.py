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

COMANDI = [
    "serve", "stats", "purge", "bench", "ablate", "optimize", "dashboard",
    "compaction", "prompt", "substitutions", "cachekey", "cachewrites",
    "ceiling", "overhead", "pruning",
]


@pytest.mark.parametrize("nome", COMANDI)
def test_ogni_comando_ha_un_aiuto(nome):
    """Intercetta le firme malformate, che typer rifiuta alla costruzione."""
    esito = runner.invoke(cli.app, [nome, "--help"])
    assert esito.exit_code == 0, esito.output


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
