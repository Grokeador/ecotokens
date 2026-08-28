"""EcoTokens: gateway locale per Claude che riduce la spesa in token.

La versione sta in un posto solo, `pyproject.toml`, e da li' viene letta a
runtime. Era scritta a mano in tre punti - il pacchetto, il titolo dell'app
FastAPI e la risposta di `/health` - e tre copie di un numero sono tre
occasioni perche' due di esse diventino vecchie senza che nessuno se ne
accorga: chi legge `/health` avrebbe visto una versione che il pacchetto non
aveva piu'.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _version_installata

try:
    __version__ = _version_installata("ecotokens")
except PackageNotFoundError:  # pragma: no cover - solo fuori da un'installazione
    # Girando dai sorgenti senza `pip install`, i metadati non esistono. Si
    # legge allora il pyproject, che resta l'unica fonte: inventare qui un
    # numero di ripiego significherebbe reintrodurre la copia che si vuole
    # togliere.
    import tomllib
    from pathlib import Path

    _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        __version__ = tomllib.loads(_pyproject.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        __version__ = "0+sconosciuta"

__all__ = ["__version__"]
