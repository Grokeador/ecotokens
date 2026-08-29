"""Test del pannello di controllo.

E' l'unica pagina del progetto che **decide** invece di mostrare, e le tre
proprieta' che contano vengono da li'.

Cambia il gateway in esecuzione, non solo il file: un pannello che chiede di
riavviare per avere effetto viene usato una volta sola. Rifiuta i valori fuori
dai limiti **senza toccare niente**, perche' un'applicazione parziale lascia
una configurazione che nessuno ha scelto. E tocca solo cio' che e' dichiarato
modificabile: credenziali, indirizzo di ascolto e percorso del database restano
nel file, dove serve accesso alla macchina.
"""

from __future__ import annotations

import pytest

from ecotokens.config import Settings
from ecotokens.pannello import (
    FUORI_PORTATA,
    GRUPPI,
    TUTTI_I_CAMPI,
    ModificaRifiutata,
    prepara,
    render_pannello,
    scrivi_configurazione,
    stato,
    valore_corrente,
)


# --- cosa e' lecito toccare ------------------------------------------------


def test_nessun_campo_segreto_e_modificabile():
    """Una chiave non si scrive in un campo di un modulo web.

    Il pannello finirebbe per mostrarla a chiunque apra la pagina, e la
    scriverebbe in chiaro nel file di configurazione - che e' il posto da cui
    il progetto passa il tempo a tenerle fuori.
    """
    proibiti = ("api_key", "auth_token", "token", "password", "credential")
    for chiave in TUTTI_I_CAMPI:
        assert not any(p in chiave.lower() for p in proibiti), chiave


def test_ne_l_indirizzo_ne_il_database_sono_modificabili():
    """Il primo aprirebbe il gateway al mondo da una pagina raggiungibile via
    rete; il secondo sposterebbe i dati mentre la connessione e' aperta."""
    for chiave in ("server.host", "server.port", "storage.path"):
        assert chiave not in TUTTI_I_CAMPI


def test_ogni_esclusione_ha_una_motivazione():
    """Un elenco di esclusioni senza motivazioni sembra una mancanza."""
    assert FUORI_PORTATA
    for cosa, perche in FUORI_PORTATA:
        assert cosa and len(perche) > 40, cosa


def test_ogni_campo_modificabile_esiste_davvero():
    """Un campo che non corrisponde a niente comparirebbe nella pagina e
    fallirebbe solo al salvataggio."""
    settings = Settings()
    for chiave in TUTTI_I_CAMPI:
        valore_corrente(settings, chiave)  # non deve sollevare


def test_ogni_campo_dice_cosa_costa():
    """La voce del progetto: un pannello che elenca opzioni senza dire cosa
    fanno sposta la decisione sull'utente senza dargli niente per prenderla."""
    for campo in TUTTI_I_CAMPI.values():
        assert len(campo.spiegazione) > 20, campo.chiave


def test_cio_che_cambia_le_risposte_e_segnato():
    """Chi accende il declassamento deve saperlo mentre lo accende, non dopo."""
    segnati = {c.chiave for c in TUTTI_I_CAMPI.values() if c.cambia_risposte}
    assert {"profilo", "router.model_downgrade", "semantic_cache.enabled"} <= segnati


# --- validare senza applicare a meta' -------------------------------------


def test_un_valore_fuori_limite_non_cambia_niente():
    """L'alternativa sarebbe una configurazione che nessuno ha scelto.

    Il limite e' quello dichiarato nel campo, non quello di pydantic: cinque
    breakpoint verrebbero accettati dal modello e rifiutati dall'API a meta'
    richiesta, cioe' molto piu' tardi e molto meno chiaramente.
    """
    settings = Settings(profilo="prudente")
    with pytest.raises(ModificaRifiutata):
        prepara(settings, {"cache_planner.max_breakpoints": "9", "budget.enabled": "true"})
    assert settings.budget.enabled is False, "la prima modifica non deve essere passata"


def test_un_campo_sconosciuto_viene_rifiutato():
    with pytest.raises(ModificaRifiutata):
        prepara(Settings(), {"server.api_key": "provo-a-cambiarla"})


def test_una_scelta_non_prevista_viene_rifiutata():
    with pytest.raises(ModificaRifiutata):
        prepara(Settings(), {"profilo": "spericolato"})


def test_le_caselle_spente_arrivano_come_false():
    """Una casella non spuntata non viene inviata: senza il campo nascosto,
    spegnere qualcosa sarebbe indistinguibile dal non averlo toccato."""
    settings = Settings(profilo="prudente")
    settings.exact_cache.enabled = True
    nuove, cambiati = prepara(settings, {"exact_cache.enabled": "false"})
    assert nuove.exact_cache.enabled is False
    assert cambiati[0]["chiave"] == "exact_cache.enabled"


def test_solo_cio_che_e_diverso_risulta_cambiato():
    """Altrimenti il riepilogo elencherebbe venti voci a ogni salvataggio e
    nessuno lo leggerebbe."""
    settings = Settings(profilo="prudente")
    _, cambiati = prepara(settings, stato(settings))
    assert cambiati == []


# --- il profilo, che governa gli altri ------------------------------------


def test_cambiare_profilo_riscrive_i_campi_che_governa():
    """Passando l'intero dump ogni campo risulta "scritto a mano", quindi il
    profilo non cambierebbe niente: e' il caso in cui un pannello sembra
    funzionare e non fa nulla."""
    prudente = Settings(profilo="prudente")
    assert prudente.router.model_downgrade is False

    nuove, _ = prepara(prudente, {"profilo": "aggressivo"})
    assert nuove.profilo == "aggressivo"
    assert nuove.router.model_downgrade is True
    assert nuove.router.effort_policy == "sempre_basso"


def test_un_campo_toccato_a_mano_vince_sul_profilo():
    """Chi spegne il declassamento nello stesso salvataggio in cui accende il
    profilo aggressivo ha deciso, e la sua decisione resta."""
    nuove, _ = prepara(
        Settings(profilo="prudente"),
        {"profilo": "aggressivo", "router.model_downgrade": "false"},
    )
    assert nuove.profilo == "aggressivo"
    assert nuove.router.model_downgrade is False


# --- il file ---------------------------------------------------------------


def test_il_file_scritto_si_rilegge_uguale(tmp_path, monkeypatch):
    """Se non si rileggesse, il pannello prometterebbe una configurazione che
    al riavvio non c'e'."""
    from ecotokens.config import load_settings

    settings, _ = prepara(
        Settings(profilo="prudente"),
        {"budget.enabled": "true", "budget.daily_usd": "7.25",
         "memory.enabled": "true", "context.prune_step_turns": "9"},
    )
    percorso = tmp_path / "ecotokens.toml"
    scrivi_configurazione(settings, percorso)

    monkeypatch.chdir(tmp_path)
    riletto = load_settings(str(percorso))
    assert riletto.budget.daily_usd == 7.25
    assert riletto.budget.enabled is True
    assert riletto.memory.enabled is True
    assert riletto.context.prune_step_turns == 9
    assert riletto.profilo == "prudente"


def test_il_file_scritto_non_contiene_credenziali(tmp_path):
    """Nemmeno vuote: un campo `api_key = ""` invita a riempirlo la' dentro."""
    percorso = tmp_path / "ecotokens.toml"
    scrivi_configurazione(Settings(), percorso)
    testo = percorso.read_text(encoding="utf-8")
    assert "api_key" not in testo.split("# Le credenziali")[0]


def test_il_file_avverte_che_i_commenti_si_perdono(tmp_path):
    """Scoprirlo dopo sarebbe la versione da configurazione del `checkout`
    che cancella senza chiedere."""
    percorso = tmp_path / "ecotokens.toml"
    scrivi_configurazione(Settings(), percorso)
    assert "non sopravvivono" in percorso.read_text(encoding="utf-8")


# --- la pagina -------------------------------------------------------------


def test_la_pagina_mostra_tutti_i_campi():
    pagina = render_pannello(Settings())
    assert pagina.count('class="campo"') == len(TUTTI_I_CAMPI)
    for gruppo in GRUPPI:
        assert gruppo.nome in pagina


def test_la_pagina_non_ha_bisogno_di_javascript():
    """Una pagina che decide non deve dipendere da uno script che potrebbe non
    partire - e un modulo HTML funziona anche da curl."""
    pagina = render_pannello(Settings())
    assert "<script" not in pagina
    assert '<form method="post"' in pagina


def test_la_pagina_non_chiede_niente_alla_rete():
    import re

    pagina = render_pannello(Settings())
    assert "https://" not in pagina
    assert not re.search(r"(src|href)\s*=\s*[\"']//", pagina)


def test_il_riepilogo_dice_cosa_e_cambiato():
    esito = {
        "cambiati": [
            {"etichetta": "Profilo", "prima": "prudente", "dopo": "aggressivo",
             "cambia_risposte": True}
        ],
        "file": "ecotokens.toml",
    }
    pagina = render_pannello(Settings(), esito=esito)
    assert "prudente" in pagina and "aggressivo" in pagina
    assert "cambia le risposte" in pagina


# --- attraverso HTTP -------------------------------------------------------


def test_il_pannello_cambia_il_gateway_in_esecuzione(client, tmp_path, monkeypatch):
    """Non solo il file: un pannello che chiede di riavviare viene usato una
    volta sola."""
    monkeypatch.chdir(tmp_path)
    gateway = client.gateway
    assert gateway.settings.budget.enabled is False

    risposta = client.post(
        "/impostazioni",
        data={"budget.enabled": "true", "budget.daily_usd": "2.5"},
    )
    assert risposta.status_code == 200

    assert gateway.settings.budget.enabled is True
    assert gateway.settings.budget.daily_usd == 2.5
    # E la pipeline in esecuzione, non solo le impostazioni.
    stadio = next(s for s in gateway.pipeline.stages if s.name == "budget")
    assert stadio.enabled is True


def test_un_valore_rifiutato_lascia_il_gateway_com_era(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prima = client.gateway.settings.cache_planner.max_breakpoints

    risposta = client.post("/impostazioni", data={"cache_planner.max_breakpoints": "9"})
    assert risposta.status_code == 400
    assert "massimo 4" in risposta.text
    assert client.gateway.settings.cache_planner.max_breakpoints == prima


def test_il_pannello_e_protetto_dalla_chiave_del_gateway(settings, stub):
    """E' la pagina che decide quanto si spende: se le altre sono protette,
    questa a maggior ragione."""
    import anthropic
    import httpx2
    from fastapi.testclient import TestClient

    from ecotokens.server import create_app

    settings.server.api_key = "segreta"
    stub_app, _ = stub
    app = create_app(settings)
    app.state.gateway.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url="http://stub",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )
    with TestClient(app) as http:
        assert http.get("/impostazioni").status_code == 401
        assert http.post("/impostazioni", data={"budget.enabled": "true"}).status_code == 401
        autorizzata = http.get(
            "/impostazioni", headers={"Authorization": "Bearer segreta"}
        )
        assert autorizzata.status_code == 200
