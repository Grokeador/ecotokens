"""EcoTokens: gateway locale per Claude che riduce la spesa in token.

La versione sta in un posto solo, `pyproject.toml`, e da li' viene letta a
runtime. Era scritta a mano in tre punti - il pacchetto, il titolo dell'app
FastAPI e la risposta di `/health` - e tre copie di un numero sono tre
occasioni perche' due di esse diventino vecchie senza che nessuno se ne
accorga: chi legge `/health` avrebbe visto una versione che il pacchetto non
aveva piu'.
"""

from __future__ import annotations

def _leggi_versione() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installata

    try:
        return installata("ecotokens")
    except PackageNotFoundError:
        # Girando dai sorgenti senza `pip install`, i metadati non esistono. Si
        # legge allora il pyproject, che resta l'unica fonte: inventare qui un
        # numero di ripiego reintrodurrebbe la copia che si vuole togliere.
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        try:
            return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            return "0+sconosciuta"


def __getattr__(nome: str) -> str:
    """La versione si legge alla prima richiesta, non all'import.

    `importlib.metadata` costa 265 ms, e quasi nessun comando ha bisogno di
    sapere la versione: farla pagare a chiunque importi il pacchetto era un
    peggioramento introdotto per una comodita' di scrittura.
    """
    if nome == "__version__":
        valore = _leggi_versione()
        globals()["__version__"] = valore
        return valore
    raise AttributeError(f"module {__name__!r} has no attribute {nome!r}")


__all__ = ["__version__"]
