"""Due risparmi di natura diversa non vanno sommati senza dirlo.

Mettere in cache lascia la risposta **identica**: e' guadagno secco. Declassare
il modello no - e' un'altra risposta a un prezzo diverso, e il banco lo dice
gia' di se stesso ("misura quanto e' lunga, non se e' giusta").

Il punto che rende la distinzione urgente: il profilo **spedito** ha il
declassamento acceso, mentre i numeri pubblicati dal progetto sono misurati col
profilo prudente (`bench._spegni_tutto` chiama `applica_profilo_prudente`).
Senza separare le due meta', un utente confronta la propria pagina con il
README e trova due cifre che misurano cose diverse - e la sua e' piu' grossa,
il che rende il confronto ancora meno sospetto.
"""

from __future__ import annotations

from .conftest import chat_payload


def _stats(client):
    return client.get("/admin/stats").json()


def test_senza_declassamento_non_si_registra_nessuna_sostituzione(client, settings):
    settings.applica_profilo_prudente()
    client.post("/v1/chat/completions", json=chat_payload())

    stats = _stats(client)
    assert stats["richieste_con_sostituzione"] == 0
    assert stats["costo_modello_richiesto_usd"] == 0


def test_con_il_declassamento_la_sostituzione_e_registrata_a_parte(client, settings):
    settings.router.enabled = True
    settings.router.model_downgrade = True
    settings.router.downgrade_policy = "sempre"
    client.post("/v1/chat/completions", json=chat_payload(model="claude-opus-5"))

    stats = _stats(client)
    assert stats["richieste_con_sostituzione"] == 1
    # Il consumo prezzato alle tariffe del modello chiesto costa piu' di quello
    # che si e' pagato davvero: e' esattamente la parte di risparmio che non e'
    # merito della cache.
    assert stats["costo_modello_richiesto_usd"] > stats["cost_usd"]


def test_il_motivo_e_scritto_nella_risposta(client, settings):
    """Chi legge la nota deve capire che non e' la stessa risposta a meno prezzo."""
    settings.router.enabled = True
    settings.router.model_downgrade = True
    settings.router.downgrade_policy = "sempre"
    risposta = client.post(
        "/v1/chat/completions", json=chat_payload(model="claude-opus-5")
    ).json()

    note = risposta["ecotokens"]["notes"]
    assert any("sostituzione del modello" in nota for nota in note), note
    assert any("un'altra risposta" in nota for nota in note), note


def test_zero_vuol_dire_non_registrato_non_zero(client, settings):
    """Le righe vecchie hanno zero in quella colonna. Contarle come
    'sostituzione a costo pari' gonfierebbe `richieste_con_sostituzione` con
    tutto il traffico precedente all'aggiornamento."""
    settings.applica_profilo_prudente()
    for _ in range(3):
        client.post("/v1/chat/completions", json=chat_payload())

    stats = _stats(client)
    assert stats["requests"] == 3
    assert stats["richieste_con_sostituzione"] == 0
