"""Test del quadro: il cruscotto compatto.

Ha un mestiere diverso dalle altre due pagine, e due proprieta' che lo
definiscono. Si apre **subito**, perche' non misura niente: legge le misure
gia' registrate. E dice **quando** ognuna e' stata presa, perche' un cruscotto
che mostra la misura di tre settimane fa senza dirlo e' peggio di uno vuoto -
chi lo legge crede di sapere com'e' adesso.

I test qui sotto fissano quelle due, piu' la regola che le tiene insieme: un
riquadro senza dati dice quale comando li produce, invece di mostrare zeri.
Uno zero e' una misura, il vuoto no.
"""

from __future__ import annotations

import time

import pytest

from ecotokens.config import Settings

from .conftest import chat_payload
from ecotokens.pricing import Usage
from ecotokens.quadro import _eta, build_quadro_data, render_quadro
from ecotokens.store.db import Database
from ecotokens.store.repos import Store


@pytest.fixture
async def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


class GatewayFinto:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store


async def dati(store, profilo: str = "prudente"):
    return await build_quadro_data(Settings(profilo=profilo), store)


# --- non misura, legge ----------------------------------------------------


async def test_su_un_database_vuoto_non_esplode_e_non_inventa(store):
    """Il caso di chi apre la pagina il primo giorno."""
    d = await dati(store)
    pagina = render_quadro(d)

    assert pagina.startswith("<!doctype html>")
    assert d["confronto"] == {}
    assert d["stadi"] == {}
    assert d["ritenzione"] == {}


async def test_i_riquadri_vuoti_dicono_quale_comando_li_riempie(store):
    """Uno zero e' una misura, il vuoto no: confonderli fa leggere
    "nessuno spreco" dove si dovrebbe leggere "non lo sappiamo"."""
    pagina = render_quadro(await dati(store))

    assert "Mai misurato" in pagina
    assert "ecotokens bench" in pagina
    assert "ecotokens ablate" in pagina
    assert "ecotokens ritenzione" in pagina
    # E nessun numero inventato al posto loro.
    assert "0.0%" not in pagina.split("INTERRUTTORI")[0]


async def test_la_pagina_non_chiede_niente_alla_rete(store):
    """Un cruscotto locale non ha motivo di uscire."""
    import re

    pagina = render_quadro(await dati(store))
    assert "https://" not in pagina
    assert not re.search(r"(src|href)\s*=\s*[\"']//", pagina)


# --- l'eta' di ogni misura ------------------------------------------------


def test_una_misura_mai_presa_lo_dice():
    assert _eta(None) == "mai misurato"
    assert _eta(0) == "mai misurato"


@pytest.mark.parametrize(
    "minuti_fa, atteso",
    [(5, "5 min fa"), (90, "1 h fa"), (60 * 24 * 3, "3 g fa")],
)
def test_l_eta_si_legge_in_unita_sensate(minuti_fa, atteso):
    """Perche' "1787926439" non dice a nessuno se il numero e' vecchio."""
    assert _eta(time.time() - minuti_fa * 60) == atteso


async def test_ogni_riquadro_porta_la_propria_eta(store):
    pagina = render_quadro(await dati(store))
    intestazioni = pagina.count('<span class="eta">')
    riquadri = pagina.count('<section class="box')
    assert intestazioni == riquadri == 9


# --- la ritenzione registrata ---------------------------------------------


class EsitoFinto:
    def __init__(self, scenario, variante, tenuti, persi):
        self.scenario = scenario
        self.variante = variante
        self.sopravvissuti = ["x"] * tenuti
        self.perduti = ["y"] * persi
        self.prompt_tokens = 100
        self.riassunti_nuovi = 1


async def test_la_ritenzione_registrata_finisce_nel_quadro(store):
    """Serve registrarla: misurarla dura mezzo minuto, e una pagina di
    controllo che si fa aspettare non viene guardata."""
    await store.save_retention(
        [
            EsitoFinto("a", "intatto", 3, 0),
            EsitoFinto("a", "potato", 0, 3),
            EsitoFinto("b", "intatto", 2, 0),
            EsitoFinto("b", "potato", 1, 1),
        ]
    )

    d = await dati(store)
    assert len(d["ritenzione"]["rows"]) == 4

    pagina = render_quadro(d)
    # intatto: 5 su 5. potato: 1 su 5.
    assert "100%" in pagina and "20%" in pagina


async def test_solo_l_ultimo_giro_di_ritenzione(store):
    """Mescolare due giri darebbe medie fra configurazioni diverse: il modo
    classico di ottenere un numero plausibile e privo di significato."""
    await store.save_retention([EsitoFinto("a", "potato", 0, 3)])
    time.sleep(0.01)
    await store.save_retention([EsitoFinto("a", "potato", 3, 0)])

    righe = (await store.latest_retention())["rows"]
    assert len(righe) == 1
    assert righe[0]["kept"] == 3, "l'ultimo giro, non la media dei due"


# --- gli interruttori -----------------------------------------------------


async def test_gli_stadi_spenti_portano_il_motivo_anche_qui(store):
    """La stessa regola della console: "spento" senza ragione fa chiedere."""
    d = await dati(store)
    spenti = [v for v in d["config"] if not v["acceso"]]
    assert spenti, "col profilo prudente qualcosa e' spento"
    for voce in spenti:
        assert voce["dettaglio"], f"{voce['nome']} spento senza motivo"


async def test_il_profilo_aggressivo_si_vede_anche_senza_misure(store):
    """Il profilo e' l'interruttore che governa gli altri, e va visto sempre.

    Prima stava solo nel riquadro del verdetto, che senza un banco eseguito e'
    vuoto: su un database appena creato la pagina non diceva la cosa piu'
    importante che ha da dire, cioe' che parte del risparmio non e' la stessa
    risposta pagata meno.
    """
    d = await dati(store, profilo="aggressivo")
    assert d["confronto"] == {}, "nessuna misura registrata, apposta"

    per_nome = {v["nome"]: v for v in d["config"]}
    assert per_nome["cambio modello"]["acceso"] is True
    assert per_nome["effort"]["dettaglio"] == "sempre_basso"

    pagina = render_quadro(d)
    assert "profilo aggressivo" in pagina
    assert "altra risposta" in pagina


async def test_col_profilo_prudente_la_pagina_dice_che_il_contenuto_non_cambia(store):
    pagina = render_quadro(await dati(store, profilo="prudente"))
    assert "profilo prudente" in pagina
    assert "nessuno stadio tocca il contenuto" in pagina


# --- il traffico vero -----------------------------------------------------


async def test_il_traffico_registrato_compare(store):
    await store.record_usage(
        session_id="s1",
        model="claude-opus-5",
        source="api",
        usage=Usage(input_tokens=1000, output_tokens=100),
        cost_usd=0.01,
        baseline_cost_usd=0.05,
        saved_usd=0.04,
        stage_notes={"router": ["ha declassato"]},
        stages_enabled=["router", "context"],
    )

    d = await dati(store)
    assert d["traffico"]["requests"] == 1
    pagina = render_quadro(d)
    assert "80.0%" in pagina, "risparmio del traffico vero"
    # E il conteggio per stadio, che distingue "muto" da "spento".
    assert "1/1" in pagina and "0/1" in pagina


async def test_il_quadro_mostra_il_merito_accanto_al_risparmio(client):
    """Il risparmio totale si misura contro un client che non usa affatto la
    cache: comprende quindi lo sconto che Anthropic fa a chiunque. Mostrarlo da
    solo attribuisce al gateway anche quello."""
    from ecotokens.quadro import build_quadro_data, render_quadro

    client.post("/v1/chat/completions", json=chat_payload())
    dati = await build_quadro_data(client.gateway.settings, client.gateway.store)
    pagina = render_quadro(dati)

    assert "merito del gateway" in pagina
    assert "vs chi marca il proprio system prompt" in pagina
    assert "vs nessuna cache" in pagina
