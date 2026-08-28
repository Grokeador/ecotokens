"""Test dell'analisi del tetto.

Questo modulo esiste per rispondere a una domanda che prima o poi arriva sempre
- "portalo al 99%" - con un'aritmetica invece che con un tentativo. Il rischio
proprio di uno strumento cosi' e' di essere accomodante: basta valutare il
pavimento un po' troppo in basso e qualunque obiettivo diventa raggiungibile
sulla carta. I test qui sotto fissano il verso dell'errore.
"""

from __future__ import annotations

import pytest

from ecotokens.ceiling import (
    MODELLO_MINIMO,
    CeilingFloor,
    CeilingReport,
    CeilingStep,
    RepetitionPoint,
    _pavimento,
    _su_modello,
    ripetizioni_per_obiettivo,
    sovrapprezzo_scrittura,
)
from ecotokens.workloads import Scenario


def gradino(**campi) -> CeilingStep:
    base = dict(
        etichetta="prova",
        descrizione="",
        in_cambio="",
        cost_usd=1.0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        full_price_tokens=0,
    )
    base.update(campi)
    return CeilingStep(**base)


# --- il pavimento ---------------------------------------------------------


def test_il_pavimento_valuta_le_scritture_a_prezzo_pieno_non_a_125():
    """Deve essere un limite che nessuno puo' battere, non una stima.

    Una scrittura si paga 1,25x. Contarla cosi' alzerebbe il pavimento e
    renderebbe irraggiungibili obiettivi che invece si raggiungono: l'errore
    andrebbe nel verso sbagliato, cioe' scoraggiare un lavoro che avrebbe
    funzionato.
    """
    pavimento = _pavimento(gradino(cache_write_tokens=1_000_000), MODELLO_MINIMO)
    # Haiku 4.5: $1/Mtok di input. A 1,25x sarebbe $1.25.
    assert pavimento.input_nuovo_usd == pytest.approx(1.0)


def test_il_pavimento_non_sconta_l_output():
    """Nessuna cache tocca i token generati: non esistevano prima."""
    pavimento = _pavimento(gradino(output_tokens=1_000_000), MODELLO_MINIMO)
    assert pavimento.output_usd == pytest.approx(5.0)  # $5/Mtok su Haiku 4.5
    assert pavimento.input_nuovo_usd == 0.0


def test_le_riletture_restano_nel_pavimento_ma_scontate():
    pavimento = _pavimento(gradino(cache_read_tokens=1_000_000), MODELLO_MINIMO)
    assert pavimento.riletture_usd == pytest.approx(0.1)


def test_il_pavimento_e_sempre_sotto_il_costo_vero():
    """La proprieta' che rende utile il numero.

    Se il pavimento potesse superare il costo misurato, direbbe che una
    configurazione gia' osservata e' impossibile.
    """
    passo = gradino(
        cost_usd=2.0,
        output_tokens=10_000,
        cache_write_tokens=100_000,
        cache_read_tokens=400_000,
        full_price_tokens=1_000,
    )
    assert _pavimento(passo, MODELLO_MINIMO).totale_usd < passo.cost_usd


# --- il verdetto ----------------------------------------------------------


def test_un_obiettivo_sotto_il_pavimento_e_dichiarato_irraggiungibile():
    report = CeilingReport(
        baseline_usd=10.0, floor=CeilingFloor(0.3, 0.2, 0.1)  # pavimento 0.6
    )
    assert report.raggiungibile(0.90) is True  # serve <= 1.0
    assert report.raggiungibile(0.95) is False  # serve <= 0.5
    assert report.tetto_teorico() == pytest.approx(0.94)


def test_senza_pavimento_non_si_promette_niente():
    """Un report incompleto deve dire di no, non tacere e sembrare un si'."""
    report = CeilingReport(baseline_usd=10.0)
    assert report.raggiungibile(0.5) is False
    assert report.tetto_teorico() == 0.0


def test_il_massimo_sicuro_ignora_le_leve_che_costano_qualita():
    report = CeilingReport(baseline_usd=10.0)
    report.steps = [
        gradino(etichetta="sicura", cost_usd=4.0),
        gradino(etichetta="spinta", cost_usd=1.0, in_cambio="la qualita' delle risposte"),
    ]
    assert report.migliore.etichetta == "spinta"
    assert report.massimo_sicuro.etichetta == "sicura"


# --- il cambio di modello -------------------------------------------------


def test_cambiare_modello_non_tocca_il_resto_della_richiesta():
    originale = Scenario(
        name="x",
        description="",
        requests=[{"model": "claude-opus-5", "messages": [{"role": "user", "content": "ciao"}]}],
    )
    copia = _su_modello([originale], MODELLO_MINIMO)[0]
    assert copia.requests[0]["model"] == MODELLO_MINIMO
    assert copia.requests[0]["messages"] == originale.requests[0]["messages"]
    # E l'originale non deve essere stato modificato sotto i piedi.
    assert originale.requests[0]["model"] == "claude-opus-5"


# --- quante ripetizioni servono -------------------------------------------


def _punto(richieste: int, per_richiesta: float, costo: float) -> RepetitionPoint:
    return RepetitionPoint(
        uniche=1,
        ripetizioni=richieste,
        richieste=richieste,
        baseline_usd=per_richiesta * richieste,
        cost_usd=costo,
    )


def test_le_ripetizioni_necessarie_crescono_con_l_obiettivo():
    punti = [_punto(50, 0.03, 0.03)]
    assert ripetizioni_per_obiettivo(punti, 0.90) < ripetizioni_per_obiettivo(punti, 0.99)


def test_le_ripetizioni_necessarie_bastano_davvero():
    """Il numero restituito deve raggiungere l'obiettivo, non sfiorarlo."""
    per_richiesta, costo_con = 0.03, 0.03
    punti = [_punto(50, per_richiesta, costo_con)]
    for obiettivo in (0.90, 0.95, 0.99):
        n = ripetizioni_per_obiettivo(punti, obiettivo)
        risparmio = 1.0 - costo_con / (n * per_richiesta)
        assert risparmio >= obiettivo


def test_senza_un_punto_ripetitivo_non_si_estrapola():
    """Meglio nessuna risposta che una inventata da carichi tutti diversi."""
    punti = [
        RepetitionPoint(uniche=4, ripetizioni=3, richieste=12, baseline_usd=1.0, cost_usd=0.1)
    ]
    assert ripetizioni_per_obiettivo(punti, 0.99) is None


def test_il_sovrapprezzo_di_una_scrittura_e_la_differenza_non_il_totale():
    assert sovrapprezzo_scrittura(1_000_000, MODELLO_MINIMO, "5m") == pytest.approx(0.25)
    assert sovrapprezzo_scrittura(1_000_000, MODELLO_MINIMO, "1h") == pytest.approx(1.0)


# --- la misura vera, in piccolo -------------------------------------------


async def test_la_curva_sale_al_crescere_delle_ripetizioni():
    """La proprieta' che giustifica tutta la sezione.

    Non i valori - dipendono dal corpus - ma il verso: piu' il carico si
    ripete, piu' la cache esatta lo copre, e il risparmio sale.
    """
    from ecotokens.bench import _abilita_prompt, _run_scenario, make_settings
    from ecotokens.workloads import scenario_ripetitivo

    async def risparmio(uniche: int, ripetizioni: int) -> float:
        scenario = scenario_ripetitivo(uniche=uniche, ripetizioni=ripetizioni)
        prima = await _run_scenario(scenario, make_settings(None), "senza", live=False)
        dopo = await _run_scenario(
            scenario, make_settings(_abilita_prompt), "con", live=False
        )
        return (prima.cost_usd - dopo.cost_usd) / prima.cost_usd

    raro = await risparmio(6, 2)
    fitto = await risparmio(2, 10)
    assert fitto > raro


# --- lo sconto batch ------------------------------------------------------


async def test_lo_sconto_batch_entra_nel_pavimento():
    """Un tetto che ignora uno sconto del listino non e' un tetto.

    La Message Batches API sconta della meta' input e output. Il gateway non la
    usa - le richieste diventerebbero asincrone - ma escluderla dal pavimento
    lo alzerebbe artificialmente, e un limite gonfiato dichiara impossibili
    obiettivi che invece si raggiungono. L'errore andrebbe nel verso peggiore:
    far rinunciare a un lavoro che avrebbe funzionato.
    """
    from ecotokens.ceiling import SCONTO_BATCH, measure_ceiling

    report = await measure_ceiling()
    assert report.floor is not None

    etichette = [passo.etichetta for passo in report.steps]
    assert any("batch" in e for e in etichette), etichette

    batch = report.steps[-1]
    precedente = report.steps[-2]
    assert batch.cost_usd == pytest.approx(precedente.cost_usd * SCONTO_BATCH)
    # E lo sconto non e' gratis: deve portare scritto cosa costa.
    assert not batch.sicura and batch.in_cambio

    # Il pavimento resta sotto il costo del gradino piu' economico misurato.
    assert report.floor.totale_usd < min(p.cost_usd for p in report.steps)


async def test_il_99_resta_escluso_anche_con_ogni_sconto_sommato():
    """La conclusione che il comando esiste per sostenere.

    Non e' un test sul valore esatto - dipende dal corpus, che cresce - ma
    sulla relazione: finche' il pavimento supera l'uno per cento del
    riferimento, il 99% non e' un obiettivo difficile ma un obiettivo escluso.
    """
    from ecotokens.ceiling import measure_ceiling

    report = await measure_ceiling()
    assert report.raggiungibile(0.99) is False
    assert report.floor.totale_usd > report.baseline_usd * 0.01
