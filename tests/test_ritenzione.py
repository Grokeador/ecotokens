"""Test dello strumento che misura se l'informazione arriva fino al prompt.

E' lo strumento che mancava al progetto, e la sua utilita' dipende interamente
dal fatto che non abbia opinioni: cerca una stringa in un prompt, e basta. Ogni
tentativo di renderlo piu' furbo - sinonimi, distanza fra stringhe, un modello
che giudica - reintrodurrebbe la soglia che lo rende inutile.

I test qui sotto fissano quella disciplina, piu' i due difetti trovati mentre
lo si costruiva: le domande fatte in parallelo invece che in fila creavano
sessioni diverse, e il confronto dei token fra varianti potate non regge.
"""

from __future__ import annotations

import pytest

from ecotokens.retention import (
    VARIANTI,
    _configura,
    _presente,
    _testo_del_prompt,
    misura_ritenzione,
    scenari_di_ritenzione,
)


# --- la ricerca, che deve restare stupida ---------------------------------


def test_il_segno_si_trova_a_meno_di_maiuscole_e_spazi():
    assert _presente("Il valore e'  Python   3.13 nel progetto", "python 3.13")
    assert _presente("PORTA: 8443", "8443")


def test_nessuna_indulgenza_oltre_quella():
    """Un segno assente e' assente. Senza questa durezza la misura non serve.

    Accettare sinonimi o quasi-corrispondenze significherebbe scegliere una
    soglia, e una soglia e' un giudizio: la misura varrebbe quanto chi l'ha
    tarata, che e' esattamente cio' da cui questo strumento doveva liberarci.
    """
    assert not _presente("la porta di ascolto", "8443")
    assert not _presente("versione 3.14", "3.13")


def test_il_testo_del_prompt_guarda_ovunque():
    """Un fatto puo' arrivare dal system, dalla coda o da dentro un riassunto.

    Cercarlo in un posto solo darebbe un falso negativo proprio sullo stadio
    che ha funzionato.
    """
    params = {
        "system": [{"type": "text", "text": "istruzioni"}, {"type": "text", "text": "Porta: 8443"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "domanda"}]},
            {"role": "system", "content": "<note>\n- Python 3.13\n</note>"},
        ],
    }
    testo = _testo_del_prompt(params)
    assert "8443" in testo and "Python 3.13" in testo and "domanda" in testo


def test_le_etichette_strutturali_non_finiscono_nel_testo():
    """Altrimenti un segno come "text" o "user" risulterebbe sempre presente."""
    testo = _testo_del_prompt(
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "ciao"}]}]}
    )
    assert testo.strip() == "ciao"


# --- le configurazioni messe a confronto ----------------------------------


def test_la_variante_intatta_e_davvero_intatta():
    """E' il limite superiore: se potasse, non sarebbe un riferimento."""
    settings = _configura("intatto")
    assert settings.context.enabled is False
    assert settings.memory.enabled is False


@pytest.mark.parametrize("variante", ["potato", "potato + memoria", "potato + memoria stabile"])
def test_le_varianti_potate_potano_davvero(variante):
    """Soglie basse di proposito: senza, non scatterebbe niente.

    Sarebbe il difetto piu' facile da non vedere - una misura che dice "non si
    perde nulla" perche' non ha fatto nulla.
    """
    settings = _configura(variante)
    assert settings.context.enabled is True
    assert settings.context.trigger_ratio < 0.01


def test_la_memoria_stabile_e_distinta_da_quella_pertinente():
    assert _configura("potato + memoria").memory.retrieval == "pertinente"
    assert _configura("potato + memoria stabile").memory.retrieval == "stabile"


def test_ogni_variante_dichiarata_e_configurabile():
    for nome, descrizione in VARIANTI:
        assert descrizione, f"{nome} senza descrizione"
        _configura(nome)


# --- gli scenari ----------------------------------------------------------


def test_ogni_fatto_e_piantato_prima_di_essere_chiesto():
    for scenario in scenari_di_ritenzione():
        for impianto in scenario.impianti:
            assert impianto.turno < scenario.turni, (
                f"{scenario.name}: {impianto.segno} piantato troppo tardi"
            )


def test_il_segno_e_contenuto_nel_fatto():
    """Cercare qualcosa che non e' mai stato piantato darebbe sempre 0%."""
    for scenario in scenari_di_ritenzione():
        for impianto in scenario.impianti:
            assert impianto.segno in impianto.fatto


def test_il_riempimento_non_contiene_informazione():
    """Se ne contenesse, un riassunto potrebbe tenerla al posto dei fatti."""
    for scenario in scenari_di_ritenzione():
        for impianto in scenario.impianti:
            assert impianto.segno not in scenario.riempimento


# --- la misura, di corsa --------------------------------------------------


async def test_potare_senza_memoria_perde_tutto_e_la_memoria_lo_rimette():
    """Il risultato per cui lo strumento e' stato scritto.

    Non e' un test fragile su una percentuale: e' un ordinamento. La variante
    intatta non puo' perdere niente per costruzione, quella potata perde, e la
    memoria deve rimettere almeno quanto la potatura ha tolto.
    """
    esiti = {(e.scenario, e.variante): e for e in await misura_ritenzione()}
    for scenario in scenari_di_ritenzione():
        intatto = esiti[(scenario.name, "intatto")]
        potato = esiti[(scenario.name, "potato")]
        stabile = esiti[(scenario.name, "potato + memoria stabile")]

        assert intatto.quota == 1.0, f"{scenario.name}: il riferimento deve tenere tutto"
        assert potato.quota < intatto.quota, f"{scenario.name}: la potatura non ha potato"
        assert stabile.quota >= potato.quota, f"{scenario.name}: la memoria ha peggiorato"


async def test_la_memoria_stabile_regge_dove_quella_lessicale_cede():
    """Lo scenario che ha cambiato il default, in forma di test.

    Fatti telegrafici e domande con altre parole: la ricerca lessicale non ha
    su cosa fare match. E' la conseguenza non voluta di una decisione giusta -
    scrivere i fatti corti - e senza questo scenario nessuno se ne sarebbe
    accorto, perche' tutti gli altri usavano le stesse parole nei due posti.
    """
    esiti = {(e.scenario, e.variante): e for e in await misura_ritenzione()}
    pertinente = esiti[("parole-diverse", "potato + memoria")]
    stabile = esiti[("parole-diverse", "potato + memoria stabile")]

    assert pertinente.quota == 0.0
    assert stabile.quota == 1.0
