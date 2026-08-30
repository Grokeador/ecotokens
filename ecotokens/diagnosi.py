"""`ecotokens diagnosi`: cosa non funzionera', prima che smetta di funzionare.

Il gateway ha una particolarita' scomoda: **quasi tutti i suoi modi di essere
mal configurato non danno errore.** Una chiave assente si manifesta come un
500 sulla prima richiesta vera; una cartella non scrivibile come un registro
che resta vuoto; l'estensione FTS5 assente come una memoria che non trova mai
niente; un modello sotto la soglia minima come una cache che non si forma - e
quest'ultimo caso l'API non lo segnala affatto.

Sono tutti guasti silenziosi, e un guasto silenzioso costa piu' di uno
rumoroso: si scopre dopo, quando si e' gia' concluso qualcosa di sbagliato.
Questo comando li rende rumorosi a comando.

**Non legge mai il valore di una credenziale**, solo da dove arriva. Un
comando di diagnosi finisce incollato nelle segnalazioni di errore, ed e'
esattamente il posto in cui una chiave non deve trovarsi.
"""

from __future__ import annotations

import os
import platform
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OK = "ok"
AVVISO = "avviso"
GRAVE = "grave"


@dataclass
class Esito:
    """Un controllo, il suo esito e cosa fare se non va."""

    nome: str
    stato: str
    dettaglio: str
    rimedio: str = ""

    @property
    def va_bene(self) -> bool:
        return self.stato == OK


@dataclass
class Diagnosi:
    esiti: list[Esito] = field(default_factory=list)

    def aggiungi(self, nome: str, stato: str, dettaglio: str, rimedio: str = "") -> None:
        self.esiti.append(Esito(nome, stato, dettaglio, rimedio))

    @property
    def gravi(self) -> list[Esito]:
        return [e for e in self.esiti if e.stato == GRAVE]

    @property
    def avvisi(self) -> list[Esito]:
        return [e for e in self.esiti if e.stato == AVVISO]

    @property
    def codice_uscita(self) -> int:
        """0 se tutto va, 1 se c'e' un avviso, 2 se c'e' qualcosa di grave.

        Serve a poterlo mettere in uno script di avvio: `ecotokens diagnosi ||
        exit` ferma un servizio che sarebbe partito rotto.
        """
        if self.gravi:
            return 2
        return 1 if self.avvisi else 0


# --- i singoli controlli ---------------------------------------------------


def _ambiente(d: Diagnosi) -> None:
    from . import __version__

    versione = sys.version_info
    if versione < (3, 11):
        d.aggiungi(
            "Python",
            GRAVE,
            f"{platform.python_version()}: troppo vecchio",
            "Serve Python 3.11 o successivo: il codice usa la sintassi `X | None`.",
        )
    else:
        d.aggiungi(
            "Python", OK, f"{platform.python_version()} su {platform.system()}"
        )
    d.aggiungi("EcoTokens", OK, f"versione {__version__}")


# Piu' corta di cosi' nessuna credenziale Anthropic puo' essere: il solo
# prefisso `sk-ant-api03-` ne occupa tredici. Il limite sta basso di proposito -
# serve a distinguere «non e' arrivato niente» da «non conosco questo formato»,
# non a convalidare la chiave. Convalidarla e' un lavoro del server, e l'unico
# modo onesto di farlo da qui e' `verifica --live`.
_LUNGHEZZA_MINIMA = 20


def _forma_sospetta(valore: str) -> str:
    """Cosa non va nella *forma* della credenziale, senza leggerne il valore.

    Trovato misurando, non ragionando: dopo un incolla fallito la variabile
    conteneva **un carattere** e questo controllo diceva OK, perche' guardava
    se la variabile esistesse. Un guasto silenzioso in piu' proprio nel comando
    che esiste per renderli rumorosi - e sarebbe riemerso come 401 alla prima
    richiesta vera, cioe' il difetto che il modulo dichiara di prevenire.

    Restituisce una descrizione del difetto, o la stringa vuota se non c'e'
    niente da dire. Non stampa mai il valore: la lunghezza compare solo quando
    e' cosi' corta da non essere una credenziale.
    """
    if valore != valore.strip():
        return "ha spazi o un a capo ai bordi"
    if valore[0] in "\"'" or valore[-1] in "\"'":
        return "e' racchiusa fra virgolette"
    if len(valore) < _LUNGHEZZA_MINIMA:
        quanti = "un carattere" if len(valore) == 1 else f"{len(valore)} caratteri"
        return f"e' lunga {quanti}: l'incolla non e' passato"
    return ""


def _credenziali(d: Diagnosi, settings: Any) -> None:
    """Da dove arriva la chiave Anthropic. **Mai quale sia.**"""
    if settings.upstream.api_key:
        d.aggiungi(
            "Credenziali Anthropic",
            GRAVE,
            "presenti nel file di configurazione",
            "Toglierle da li'. Un file di configurazione finisce in un "
            "repository, in un backup o in un allegato: e' il modo piu' facile "
            "di pubblicare una chiave per sbaglio. Usare la variabile "
            "d'ambiente ANTHROPIC_API_KEY.",
        )
        return

    fonti = [
        nome
        for nome in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        if os.environ.get(nome)
    ]
    if fonti:
        difetto = _forma_sospetta(os.environ[fonti[0]])
        if difetto:
            d.aggiungi(
                "Credenziali Anthropic",
                GRAVE,
                f"da {', '.join(fonti)}, ma {difetto}",
                "La variabile c'e', il suo contenuto no. Succede quando "
                "l'incolla non passa (in molte console `Ctrl+V` non funziona "
                "dentro `Read-Host`: serve il tasto destro) o quando le "
                "virgolette di `setx` finiscono dentro il valore. Rifare il "
                "passaggio e riaprire il terminale.",
            )
            return
        d.aggiungi("Credenziali Anthropic", OK, f"da {', '.join(fonti)}")
        return

    # Nessuna variabile: puo' esserci comunque un profilo, e a saperlo e' solo
    # l'SDK. Si guarda cosa ha risolto, non come.
    try:
        import anthropic

        client = anthropic.AsyncAnthropic()
        risolto = any(
            getattr(client, nome, None) is not None
            for nome in ("api_key", "auth_token", "credentials")
        )
    except Exception:
        risolto = False

    if risolto:
        d.aggiungi("Credenziali Anthropic", OK, "da un profilo `ant auth login`")
    else:
        d.aggiungi(
            "Credenziali Anthropic",
            GRAVE,
            "nessuna trovata",
            'setx ANTHROPIC_API_KEY "la-tua-chiave", poi riaprire il terminale. '
            "Senza, il gateway parte e risponde 401 alla prima richiesta vera.",
        )


def _configurazione(d: Diagnosi, percorso: str | None) -> None:
    candidati = [percorso] if percorso else ["ecotokens.toml"]
    trovato = next((c for c in candidati if c and Path(c).is_file()), None)
    if trovato:
        d.aggiungi("Configurazione", OK, f"letta da {Path(trovato).resolve()}")
    else:
        d.aggiungi(
            "Configurazione",
            OK,
            "nessun file: valgono i valori predefiniti",
            "Va bene cosi' per provare. Per cambiare qualcosa: la pagina "
            "/impostazioni scrive il file da sola.",
        )


def _database(d: Diagnosi, settings: Any) -> None:
    percorso = settings.storage.path
    if percorso == ":memory:":
        d.aggiungi(
            "Registro",
            AVVISO,
            "in memoria: si perde tutto alla chiusura",
            "Va bene per una prova. Per tenere lo storico, indicare un file in "
            "storage.path.",
        )
        return

    file = Path(percorso)
    cartella = file.parent if file.parent != Path("") else Path(".")
    try:
        cartella.mkdir(parents=True, exist_ok=True)
        prova = cartella / ".ecotokens-prova-scrittura"
        prova.write_text("x", encoding="utf-8")
        prova.unlink()
    except OSError as errore:
        d.aggiungi(
            "Registro",
            GRAVE,
            f"{cartella} non e' scrivibile: {errore}",
            "Senza registro il gateway funziona ma non misura niente, e ogni "
            "pagina resta vuota senza spiegare perche'.",
        )
        return

    esistente = file.is_file()
    dimensione = file.stat().st_size / 1024 if esistente else 0
    d.aggiungi(
        "Registro",
        OK,
        f"{file.resolve()}" + (f", {dimensione:.0f} KB" if esistente else " (da creare)"),
    )


def _sqlite(d: Diagnosi) -> None:
    connessione = sqlite3.connect(":memory:")
    try:
        try:
            connessione.execute("CREATE VIRTUAL TABLE prova USING fts5(testo)")
            fts5 = True
        except sqlite3.OperationalError:
            fts5 = False
    finally:
        connessione.close()

    if fts5:
        d.aggiungi("SQLite", OK, f"{sqlite3.sqlite_version}, FTS5 disponibile")
    else:
        d.aggiungi(
            "SQLite",
            AVVISO,
            f"{sqlite3.sqlite_version}, senza FTS5",
            "Il recupero dei fatti di memoria ripiega su un confronto piu' "
            "grezzo. Non e' un guasto, ma trova di meno - e la memoria e' gia' "
            "lo stadio con il margine piu' sottile.",
        )


def _porta(d: Diagnosi, settings: Any) -> None:
    host, porta = settings.server.host, settings.server.port
    prova = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    prova.settimeout(0.2)
    try:
        prova.bind(("127.0.0.1" if host == "0.0.0.0" else host, porta))
        d.aggiungi("Porta", OK, f"{host}:{porta} libera")
    except OSError:
        d.aggiungi(
            "Porta",
            AVVISO,
            f"{host}:{porta} gia' occupata",
            "O il gateway e' gia' in esecuzione - e allora va bene - o la usa "
            "qualcun altro e `serve` fallira'.",
        )
    finally:
        prova.close()


def _esposizione(d: Diagnosi, settings: Any) -> None:
    """La porta inoltra con la chiave Anthropic dell'utente: non e' un servizio
    che espone dei dati, e' uno che espone una carta di credito."""
    locale = settings.server.host in ("127.0.0.1", "localhost", "::1")
    if locale:
        d.aggiungi("Esposizione", OK, f"solo locale ({settings.server.host})")
    elif settings.server.api_key:
        d.aggiungi(
            "Esposizione",
            AVVISO,
            f"{settings.server.host}, protetta da chiave del gateway",
            "Serve anche TLS e un proxy davanti: la chiave viaggia in chiaro "
            "su HTTP. Vedi il README, sezione Esporre il gateway.",
        )
    else:
        d.aggiungi(
            "Esposizione",
            GRAVE,
            f"{settings.server.host} senza chiave del gateway",
            "Chiunque sulla rete puo' spendere a tuo nome e leggere i prompt "
            "gia' passati. `serve` si rifiuta di partire cosi', ed e' giusto.",
        )


def _stadi(d: Diagnosi, settings: Any) -> None:
    from .server import Gateway

    pipeline = Gateway._build_pipeline(settings)
    accesi = [s.name for s in pipeline.stages if getattr(s, "enabled", True)]
    spenti = [s.name for s in pipeline.stages if not getattr(s, "enabled", True)]
    d.aggiungi(
        "Stadi attivi",
        OK,
        f"{len(accesi)} accesi: {', '.join(accesi)}"
        + (f" | spenti: {', '.join(spenti)}" if spenti else ""),
    )


def _extra(d: Diagnosi, settings: Any) -> None:
    try:
        import fastembed  # noqa: F401

        presente = True
    except ImportError:
        presente = False

    if presente:
        d.aggiungi("Cache semantica", OK, "fastembed installato")
    elif settings.semantic_cache.enabled:
        d.aggiungi(
            "Cache semantica",
            GRAVE,
            "accesa in configurazione ma fastembed non e' installato",
            "pip install ecotokens[semantic] - oppure spegnerla, perche' cosi' "
            "resta accesa sulla carta e ferma nei fatti.",
        )
    else:
        d.aggiungi(
            "Cache semantica",
            OK,
            "spenta, e fastembed non serve",
            "E' spenta di proposito: servire una risposta *simile* e' un "
            "rischio di correttezza, non un'ottimizzazione neutra.",
        )


def esegui(settings: Any, *, percorso_config: str | None = None) -> Diagnosi:
    """Tutti i controlli, in ordine di quanto fa male sbagliarli."""
    d = Diagnosi()
    _ambiente(d)
    _configurazione(d, percorso_config)
    _credenziali(d, settings)
    _esposizione(d, settings)
    _database(d, settings)
    _sqlite(d)
    _porta(d, settings)
    _stadi(d, settings)
    _extra(d, settings)
    return d
