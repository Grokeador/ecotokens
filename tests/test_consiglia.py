"""Un consiglio deve poter essere sbagliato, altrimenti non e' un consiglio.

Il rischio di questo comando non e' di rompersi: e' di dire sempre la stessa
cosa con la faccia di un'analisi. Questi test verificano che i quattro regimi
si distinguano davvero - su profili costruiti a mano **e** su traffico vero
prodotto dal gateway - e che sotto la soglia di campione il comando taccia.
"""

from __future__ import annotations

import pytest

from ecotokens.config import Settings
from ecotokens.consiglia import CAMPIONE_MINIMO, analizza, classifica

from .conftest import chat_payload


def profilo(**valori):
    base = {
        "richieste": 100,
        "turni_medi": 1.0,
        "quota_turno_singolo": 1.0,
        "quota_da_cache": 0.0,
        "quota_potatura": 0.0,
        "prompt_medio": 2_000,
        "tasso_continuazione": 0.5,
    }
    base.update(valori)
    return base


# --- i quattro regimi si distinguono --------------------------------------


@pytest.mark.parametrize(
    "segnali,atteso",
    [
        ({"quota_potatura": 0.6}, "agentico"),
        ({"quota_da_cache": 0.5}, "ripetitivo"),
        ({"turni_medi": 8.0}, "chat"),
        ({}, "turno_singolo"),
    ],
)
def test_ogni_regime_ha_un_segnale_che_lo_riconosce(segnali, atteso):
    assert classifica(profilo(**segnali)) == atteso


def test_il_segnale_piu_specifico_vince():
    """Un ciclo agentico ha anche molti turni: se l'ordine dei controlli fosse
    invertito verrebbe classificato come chat, e il consiglio piu' importante -
    non declassare il modello - non comparirebbe."""
    misto = profilo(quota_potatura=0.6, turni_medi=20.0, quota_da_cache=0.3)
    assert classifica(misto) == "agentico"


# --- il silenzio quando il campione non basta -----------------------------


def test_sotto_la_soglia_non_si_classifica():
    rapporto = analizza(profilo(richieste=CAMPIONE_MINIMO - 1), Settings())
    assert not rapporto.sufficiente
    assert rapporto.regime is None
    assert rapporto.consigli == []


def test_esattamente_alla_soglia_si_classifica():
    """Il confronto e' `<`, non `<=`: un test che non lo fissa lascerebbe
    scivolare la soglia di uno senza che nessuno se ne accorga."""
    rapporto = analizza(profilo(richieste=CAMPIONE_MINIMO), Settings())
    assert rapporto.sufficiente


# --- i consigli dipendono dalla configurazione ----------------------------


def _titoli(rapporto):
    return [c.titolo for c in rapporto.consigli]


def _azioni(rapporto):
    return [c.azione for c in rapporto.consigli if c.azione]


def test_col_declassamento_acceso_il_consiglio_di_spegnerlo_compare():
    config = Settings(profilo="aggressivo")
    rapporto = analizza(profilo(quota_potatura=0.6), config)
    assert any("model_downgrade = false" in azione for azione in _azioni(rapporto))


def test_col_declassamento_spento_quel_consiglio_sparisce():
    """Il controllo che impedisce al comando di dire sempre tutto."""
    config = Settings(profilo="prudente")
    rapporto = analizza(profilo(quota_potatura=0.6), config)
    assert not any("model_downgrade = false" in azione for azione in _azioni(rapporto))


def test_il_profilo_aggressivo_avverte_su_come_leggere_il_merito():
    rapporto = analizza(profilo(), Settings(profilo="aggressivo"))
    assert any("aggressivo" in titolo for titolo in _titoli(rapporto))


def test_le_conversazioni_corte_richiamano_il_pareggio_del_27_8_percento():
    rapporto = analizza(profilo(tasso_continuazione=0.10), Settings())
    consigli = [c for c in rapporto.consigli if "finiscono presto" in c.titolo]
    assert consigli, _titoli(rapporto)
    assert "27,8%" in consigli[0].perche


def test_sopra_il_pareggio_non_lo_richiama():
    rapporto = analizza(profilo(tasso_continuazione=0.60), Settings())
    assert not any("finiscono presto" in titolo for titolo in _titoli(rapporto))


def test_ogni_regime_produce_almeno_un_consiglio():
    """Un regime riconosciuto e poi senza niente da dire sarebbe peggio di non
    riconoscerlo: l'utente concluderebbe che va tutto bene."""
    for segnali in ({"quota_potatura": 0.6}, {"quota_da_cache": 0.5},
                    {"turni_medi": 8.0}, {}):
        rapporto = analizza(profilo(**segnali), Settings())
        assert rapporto.consigli, segnali


# --- e su traffico vero ---------------------------------------------------


async def test_traffico_a_turno_singolo_viene_riconosciuto(client):
    """La prova che conta: i numeri vengono da richieste davvero passate di qui,
    non da un dizionario costruito nel test.

    L'atteso e' il regime **preciso**, non "uno dei quattro": quest'ultima
    asserzione e' vera sempre, e un test sempre vero non protegge niente.
    """
    for indice in range(CAMPIONE_MINIMO + 2):
        client.post(
            "/v1/chat/completions",
            json=chat_payload(
                messages=[
                    {"role": "system", "content": "Sei un assistente. " * 200},
                    {"role": "user", "content": f"domanda diversa numero {indice}"},
                ]
            ),
        )

    segnali = await client.gateway.store.profilo_traffico()
    assert segnali["richieste"] >= CAMPIONE_MINIMO
    assert segnali["turni_medi"] == 1.0
    assert segnali["quota_turno_singolo"] == 1.0

    rapporto = analizza(segnali, client.gateway.settings)
    assert rapporto.regime == "turno_singolo"
    # Ed e' il regime in cui il gateway deve ammettere di non far risparmiare.
    assert any("non fa risparmiare" in c.titolo for c in rapporto.consigli), _titoli(
        rapporto
    )


async def test_traffico_ripetitivo_viene_riconosciuto(client):
    """Il caso opposto, dalla stessa sorgente: la stessa domanda molte volte
    deve finire servita dalla cache esatta e cambiare la classificazione."""
    for _ in range(CAMPIONE_MINIMO + 2):
        client.post("/v1/chat/completions", json=chat_payload())

    segnali = await client.gateway.store.profilo_traffico()
    assert segnali["quota_da_cache"] > 0.2, segnali

    rapporto = analizza(segnali, client.gateway.settings)
    assert rapporto.regime == "ripetitivo"
    assert any("cache esatta" in c.titolo for c in rapporto.consigli), _titoli(rapporto)
