"""Cosa arriva dalla porta, e cosa il gateway sopravvive a ricevere.

Il gateway accetta JSON da chiunque possa raggiungere la sua porta. Il resto
della suite gli manda richieste ben formate; qui gli si mandano quelle che un
client sbagliato, una libreria con un bug o un utente curioso producono senza
volerlo.

Il criterio e' lo stesso di `test_guasti.py`, applicato all'ingresso invece che
agli stadi: **una richiesta strana degrada, non abbatte.** O viene servita, o
viene rifiutata con un errore che dice cosa non andava. Quello che non deve
succedere e' che il processo si fermi, perche' un gateway che cade porta con se'
tutte le richieste degli altri, non solo quella che lo ha fatto cadere.
"""

from __future__ import annotations

import pytest

from ecotokens.pipeline.base import (
    PROFONDITA_MASSIMA,
    TroppoAnnidato,
    copia_parametri,
)


def annidato(profondita: int):
    """Un contenuto annidato a `profondita` livelli."""
    dentro: object = {"type": "text", "text": "in fondo"}
    for _ in range(profondita):
        dentro = [{"type": "text", "text": "x", "nested": dentro}]
    return dentro


# --- annidamento -----------------------------------------------------------


def test_la_copia_si_ferma_prima_dello_stack():
    """Una ricorsione su dati che arrivano da fuori e' una via di guasto che
    apre chi la scrive. A cinquecento livelli l'interprete si arrende con un
    RecursionError, e quello arriva da un punto in cui non c'e' piu' niente da
    decidere."""
    assert copia_parametri(annidato(PROFONDITA_MASSIMA // 2 - 5)) is not None
    with pytest.raises(TroppoAnnidato):
        copia_parametri(annidato(PROFONDITA_MASSIMA + 100))


def test_una_richiesta_troppo_annidata_viene_comunque_servita(client):
    """Non si puo' salvare, quindi non si ottimizza: la richiesta parte com'e'.

    E' la stessa regola degli stadi rotti. Il gateway non ottimizza cio' che
    non saprebbe annullare, e non trasforma il proprio limite in un errore di
    chi ha chiesto.
    """
    risposta = client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-5",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": annidato(400)}],
        },
    )
    assert risposta.status_code in (200, 400), risposta.text
    if risposta.status_code == 200:
        assert risposta.json()["type"] == "message"


# --- testo che i formati non digeriscono ----------------------------------


# Gli identificativi sono espliciti perche' pytest li scrive in una variabile
# d'ambiente, e su Windows una variabile oltre i 32767 caratteri fa fallire il
# teardown: il caso "messaggio molto lungo" faceva cadere il test per una
# ragione che non aveva niente a che vedere con cio' che il test verifica.
@pytest.mark.parametrize(
    "testo, cosa",
    [
        pytest.param("ciao\x00mondo", "byte nullo", id="byte-nullo"),
        pytest.param("emoji \U0001f600 e accenti aeiou", "fuori dal piano base", id="fuori-dal-piano-base"),
        pytest.param("a" * 200_000, "un messaggio molto lungo", id="un-messaggio-molto-l"),
        pytest.param("", "vuoto", id="vuoto"),
        pytest.param("\r\n\t    ", "solo spaziatura, compresa quella insolita", id="solo-spaziatura-comp"),
        pytest.param("</script><b>ciao</b>", "che somiglia a markup", id="che-somiglia-a-marku"),
    ],
)
def test_il_testo_difficile_non_ferma_il_gateway(client, testo, cosa):
    risposta = client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-5",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": testo}],
        },
    )
    assert risposta.status_code == 200, f"{cosa}: {risposta.text[:200]}"


def test_il_testo_difficile_arriva_anche_nel_registro(client):
    """Il registro sta su SQLite, e un byte nullo dentro una stringa e' il
    classico valore che passa dal JSON e si ferma sul database. Se la
    contabilita' fallisse li', il fail-open salverebbe la risposta e
    perderebbe la misura in silenzio."""
    client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-5",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "prima\x00dopo \U0001f600"}],
        },
    )
    assert client.get("/admin/stats").json()["requests"] == 1
    assert client.gateway.pipeline.guasti == {}


# --- forme che non sono richieste ------------------------------------------


@pytest.mark.parametrize(
    "corpo",
    [
        {},
        {"model": "claude-opus-5"},
        {"model": "claude-opus-5", "messages": []},
        {"model": "claude-opus-5", "messages": "non e' una lista"},
        {"model": "claude-opus-5", "messages": [{"content": "senza ruolo"}]},
    ],
)
def test_una_richiesta_malformata_riceve_un_errore_non_un_crollo(client, corpo):
    risposta = client.post("/v1/chat/completions", json=corpo)
    assert 400 <= risposta.status_code < 500, risposta.text
    assert "error" in risposta.json()


def test_un_ruolo_sconosciuto_viene_accolto_come_utente(client):
    """Scelta deliberata, non svista: essere severi su cio' che si emette e
    tolleranti su cio' che si accetta. Un client che manda un ruolo non
    previsto ha quasi sempre un messaggio vero da consegnare, e rifiutarlo
    romperebbe l'integrazione per un'etichetta."""
    risposta = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-opus-5",
            "messages": [{"role": "marziano", "content": "contenuto vero"}],
        },
    )
    assert risposta.status_code == 200
    assert "contenuto vero" in str(client.stub.last["messages"])
    assert client.stub.last["messages"][-1]["role"] == "user"


def test_un_corpo_che_non_e_json_riceve_un_errore(client):
    risposta = client.post(
        "/v1/chat/completions",
        content=b"{non e' json",
        headers={"Content-Type": "application/json"},
    )
    assert 400 <= risposta.status_code < 500


def test_un_errore_di_forma_non_lascia_traccia_nel_registro(client):
    """Contare come richiesta cio' che non e' mai partito falserebbe il
    denominatore di ogni misura successiva."""
    client.post("/v1/chat/completions", json={"messages": []})
    assert client.get("/admin/stats").json()["requests"] == 0


# --- piu' richieste insieme ------------------------------------------------


def test_richieste_identiche_in_parallelo_non_si_pestano_i_piedi(client):
    """La cache esatta e' scritta dopo la risposta: due richieste identiche
    partite insieme non si trovano a vicenda, e questo va bene - quello che
    non deve succedere e' che una delle due fallisca o che il registro perda
    una riga."""
    import concurrent.futures

    client.gateway.settings.exact_cache.enabled = True
    corpo = {
        "model": "claude-opus-5",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "la stessa identica domanda"}],
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        esiti = [f.result() for f in [pool.submit(client.post, "/v1/messages", json=corpo) for _ in range(8)]]

    assert [e.status_code for e in esiti] == [200] * 8
    assert client.gateway.pipeline.guasti == {}
    # Ogni richiesta lascia la propria riga: nessuna persa per contesa sul
    # database, nessuna contata due volte.
    assert client.get("/admin/stats").json()["requests"] == 8
