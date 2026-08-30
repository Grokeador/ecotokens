"""Il registro delle assunzioni deve restare vero da solo.

Un elenco di «cose che diamo per vere» ha un modo di fallire tutto suo: non
diventa sbagliato, diventa **incompleto**. Si aggiunge una costante al
simulatore, non si aggiunge la voce, e l'elenco continua a dire dieci mentre le
assunzioni sono undici - con l'aggravante che adesso c'e' un documento che
sembra autorevole a sostenere il numero sbagliato.

Questi test tengono l'elenco agganciato al codice, cosi' che a farlo invecchiare
serva ignorare un test rosso invece che dimenticarsi di un file.
"""

from __future__ import annotations

import importlib

import pytest

from ecotokens.assunzioni import (
    ASSUNZIONI,
    DICHIARATA,
    DOCUMENTATA,
    VERIFICATA,
    per_fonte,
    riepilogo,
)


def test_ogni_voce_dice_cosa_cambierebbe():
    """E' la domanda che rende utile una voce. Senza, l'elenco e' una lista di
    dettagli tecnici che nessuno sa come pesare."""
    for voce in ASSUNZIONI:
        assert len(voce.cosa_cambia) > 60, voce.nome
        assert len(voce.come_verificarla) > 20, voce.nome
        assert voce.fonte in (DOCUMENTATA, DICHIARATA, VERIFICATA), voce.nome


def test_ogni_voce_punta_a_codice_che_esiste():
    """Un riferimento a un nome rimosso trasforma il registro in archeologia."""
    for voce in ASSUNZIONI:
        for riferimento in voce.dove.split(", "):
            modulo, _, resto = riferimento.partition(".")
            oggetto = importlib.import_module(f"ecotokens.{modulo}")
            for pezzo in filter(None, resto.split(".")):
                # Un riferimento come `pricing.MODELS.cache_min_tokens` nomina
                # un campo delle *voci* del dizionario, non del dizionario: e'
                # come si legge naturalmente, e il test scende di conseguenza.
                if isinstance(oggetto, dict):
                    oggetto = next(iter(oggetto.values()))
                assert hasattr(oggetto, pezzo), f"{voce.nome}: manca {riferimento}"
                oggetto = getattr(oggetto, pezzo)


@pytest.mark.parametrize(
    "costante",
    [
        "LOOKBACK_BLOCKS",
        "KEPT_TOOL_RESULTS",
        "EFFORT_OUTPUT_MULTIPLIER",
        "OUTPUT_TIPICO",
    ],
)
def test_ogni_modello_del_simulatore_e_dichiarato(costante):
    """Il simulatore e' fatto di assunzioni: se una non compare nell'elenco,
    l'elenco dice un numero piu' basso del vero."""
    assert any(costante in voce.dove for voce in ASSUNZIONI), costante


def test_il_riepilogo_non_puo_mentire_sul_conto_delle_verificate():
    """La riga che va sotto qualunque numero del progetto. Il giorno in cui una
    verifica `--live` avviene davvero, questo test la costringe a comparire nel
    conto invece che in una frase."""
    testo = riepilogo()
    assert f"{len(per_fonte(VERIFICATA))} verificate" in testo
    assert f"{len(ASSUNZIONI)} assunzioni" in testo


def test_le_dichiarate_sono_segnate_come_tali():
    """La differenza fra "sta nella documentazione" e "l'abbiamo scelto noi" e'
    tutto il valore dell'elenco: la prima puo' essere invecchiata, la seconda
    puo' essere inventata."""
    dichiarate = {v.nome for v in per_fonte(DICHIARATA)}
    assert "Effetto dell'effort sui token generati" in dichiarate
    assert "Quanti tool result conserva la potatura" in dichiarate
    # E le tariffe no: quelle sono pubblicate.
    assert "Tariffe dei modelli" not in dichiarate


# Il 30 agosto 2026 questo test si e' acceso, ed e' successo per la ragione
# giusta: `ecotokens verifica --live` e' stato eseguito contro l'API vera per
# la prima volta. Restano tre voci, non una di piu', e ognuna deve portare
# scritto cosa ha risposto l'API.
VERIFICATE_ATTESE = {
    "Soglie minime di cache",
    "Quattro breakpoint al massimo",
    "I parametri rimossi danno 400",
}


def test_solo_cio_che_e_stato_misurato_e_verificato():
    """Non e' un test sul codice: e' un promemoria che si accende da solo.

    Marcare una voce come verificata senza aver eseguito la misura e' l'unico
    modo in cui questo file puo' diventare peggio che inutile - un documento
    che sembra autorevole a sostegno di un'affermazione che nessuno ha
    controllato. L'elenco sopra si allunga **dopo** una misura, mai prima.
    """
    verificate = {v.nome for v in per_fonte(VERIFICATA)}
    assert verificate == VERIFICATE_ATTESE, (
        "L'insieme delle voci verificate e' cambiato. Aggiungerne una senza "
        "aver eseguito `verifica --live` e' esattamente cio' che questo test "
        "impedisce; se la misura c'e' stata, aggiornare anche il README."
    )


def test_una_voce_verificata_dice_cosa_ha_risposto_l_api():
    """«Verificata» senza l'esito e' una spunta verde: dice che qualcuno ha
    guardato, non cosa ha visto. La data serve perche' l'API cambia."""
    for voce in per_fonte(VERIFICATA):
        assert voce.esito_dal_vivo, voce.nome
        assert "2026" in voce.esito_dal_vivo, voce.nome


def test_chi_non_e_verificata_non_finge_di_esserlo():
    for voce in ASSUNZIONI:
        if voce.fonte != VERIFICATA:
            assert not voce.esito_dal_vivo, voce.nome


def test_la_console_dice_che_i_numeri_non_vengono_dall_api_vera():
    """E' l'unico posto in cui un utente incontra le percentuali senza aver
    letto il codice."""
    from ecotokens.console import NON_MISURATO

    testi = " ".join(v["title"] + v["body"] for v in NON_MISURATO)
    assert "simulatore" in testi
    assert "assunzioni" in testi
