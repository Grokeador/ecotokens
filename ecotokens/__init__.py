"""EcoTokens: fa costare meno le richieste a Claude.

Due forme, stesso motore. Come **libreria**, dentro il tuo programma:

    from ecotokens import Economico
    client = Economico(anthropic.AsyncAnthropic())

Come **programma a parte** (`ecotokens serve`), per coprire applicazioni
che non hai scritto o mettere un tetto di spesa comune a piu' di una.


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


def __getattr__(nome: str):
    """La versione si legge alla prima richiesta, non all'import.

    `importlib.metadata` costa 265 ms, e quasi nessun comando ha bisogno di
    sapere la versione: farla pagare a chiunque importi il pacchetto era un
    peggioramento introdotto per una comodita' di scrittura.
    """
    if nome == "__version__":
        valore = _leggi_versione()
        globals()["__version__"] = valore
        return valore
    # `Economico` arriva per la stessa strada, e per una ragione simile: il
    # modulo che lo contiene importa `server`, che tira dentro FastAPI e
    # uvicorn. Farli caricare a chiunque scriva `import ecotokens` - compreso
    # chi vuole solo sapere la versione - sarebbe un peso pagato da tutti per
    # comodita' di uno.
    if nome == "Economico":
        from .libreria import Economico as classe

        globals()["Economico"] = classe
        return classe
    raise AttributeError(f"module {__name__!r} has no attribute {nome!r}")


__all__ = ["__version__", "Economico"]
