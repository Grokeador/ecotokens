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
