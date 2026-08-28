"""Test del testo aggiunto dal gateway e della chiave della cache esatta.

Due ottimizzazioni molto diverse fra loro, tenute insieme da un fatto: nessuna
delle due tocca il significato di quello che l'utente ha scritto. La prima
accorcia testo nostro, la seconda decide quando due richieste sono la stessa.
"""

from __future__ import annotations

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.exact_cache import compute_cache_key
from ecotokens.translate.to_anthropic import build_anthropic_params
from ecotokens.wording import CATALOG, catalog_totals


@pytest.fixture
def settings() -> Settings:
    return Settings()


def make_context(settings, messages, **overrides):
    payload = {"model": "claude-opus-5", "messages": messages}
    payload.update(overrides)
    request = ChatCompletionRequest.model_validate(payload)
    translation = build_anthropic_params(request, settings)
    return RequestContext(
        request=request,
        settings=settings,
        store=None,
        client=None,
        counter=None,
        completion_id="test",
        model=translation.model,
        params=translation.params,
        stream=False,
    )


# --- testo del gateway ----------------------------------------------------


def test_ogni_voce_pagata_a_ogni_richiesta_e_piu_corta_di_prima():
    """Se cresce cio' che si rispedisce sempre, e' una regressione."""
    cresciute = [
        voce.key for voce in CATALOG if voce.su_ogni_richiesta and voce.saved < 0
    ]
    assert cresciute == [], f"voci diventate piu' lunghe: {cresciute}"


def test_un_istruzione_interna_puo_allungarsi_se_accorcia_cio_che_produce():
    """Le regole dell'estrattore si pagano una volta, i fatti a ogni richiesta.

    Sono cresciute di proposito: chiedono fatti telegrafici. Un fatto scritto
    come frase costa ~25 token e uno telegrafico ~4, e i fatti si rispediscono
    a ogni richiesta successiva mentre le regole no. Il conto si chiude alla
    seconda richiesta. L'invariante "tutto piu' corto" avrebbe bocciato proprio
    questo, ed e' il motivo per cui la tavola distingue i due ritmi di paga.
    """
    regole = next(voce for voce in CATALOG if voce.key == "regole-memoria")
    assert regole.su_ogni_richiesta is False
    assert "telegrafico" in regole.text


def test_il_catalogo_e_coerente_con_i_totali():
    totali = catalog_totals()
    assert totali["saved"] == totali["before"] - totali["after"]
    assert totali["after"] < totali["before"]


def test_i_delimitatori_restano_leggibili():
    """Corti si', ma un tag deve ancora dire cosa contiene."""
    for voce in CATALOG:
        if voce.text.startswith("<"):
            assert len(voce.text) >= 5, f"{voce.key}: delimitatore troppo criptico"


# --- chiave della cache ---------------------------------------------------


def test_richieste_uguali_a_meno_di_spazi_condividono_la_chiave(settings):
    """Il caso realistico: un template incoerente, un copia e incolla."""
    pulita = make_context(settings, [{"role": "user", "content": "Qual e' la politica di reso?"}])
    sciatta = make_context(
        settings, [{"role": "user", "content": "\nQual  e'  la politica di reso?   \n\n"}]
    )
    assert compute_cache_key(pulita) == compute_cache_key(sciatta)


def test_richieste_davvero_diverse_non_condividono_la_chiave(settings):
    """La normalizzazione allarga la cache, non la rende cieca."""
    prima = make_context(settings, [{"role": "user", "content": "Qual e' la politica di reso?"}])
    seconda = make_context(settings, [{"role": "user", "content": "Qual e' la politica di reso in Francia?"}])
    assert compute_cache_key(prima) != compute_cache_key(seconda)


def test_la_normalizzazione_della_chiave_si_puo_spegnere(settings):
    settings.exact_cache.normalize_key = False
    pulita = make_context(settings, [{"role": "user", "content": "Qual e' la politica di reso?"}])
    sciatta = make_context(
        settings, [{"role": "user", "content": "Qual  e'  la politica di reso?   "}]
    )
    assert compute_cache_key(pulita) != compute_cache_key(sciatta)


def test_il_modello_resta_parte_della_chiave(settings):
    """Due modelli diversi danno risposte diverse: non vanno confusi."""
    messaggi = [{"role": "user", "content": "Qual e' la politica di reso?"}]
    opus = make_context(settings, messaggi)
    haiku = make_context(settings, messaggi, model="claude-haiku-4-5")
    assert compute_cache_key(opus) != compute_cache_key(haiku)


def test_i_blocchi_non_testuali_non_vengono_toccati(settings):
    """Un'immagine non si normalizza: la chiave deve restare calcolabile."""
    ctx = make_context(
        settings,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Che cosa   vedi?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                    },
                ],
            }
        ],
    )
    assert compute_cache_key(ctx)
