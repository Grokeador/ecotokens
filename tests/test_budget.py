"""Test del tetto di spesa.

E' la rete di sicurezza del progetto: l'unico stadio il cui scopo non e'
risparmiare ma **impedire**. Una rete che non regge non si nota finche' non
serve, e quando serve e' tardi - il denaro e' gia' uscito.

Era coperto al 62%, e la meta' mancante era proprio quella che blocca.
"""

from __future__ import annotations

import pytest

from ecotokens.config import Settings
from ecotokens.pipeline.base import PipelineAbort, RequestContext
from ecotokens.pipeline.budget import BudgetStage


class StoreFinto:
    """Solo cio' che lo stadio guarda: quanto si e' gia' speso."""

    def __init__(self, oggi: float = 0.0, mese: float = 0.0) -> None:
        self._speso = (oggi, mese)

    async def current_spend(self) -> tuple[float, float]:
        return self._speso


class ContatoreFinto:
    def __init__(self, tokens: int) -> None:
        self.tokens = tokens
        self.chiamate = 0

    async def count(self, model: str, params: dict) -> int:
        self.chiamate += 1
        return self.tokens


def contesto(settings: Settings, store, counter=None) -> RequestContext:
    return RequestContext(
        request=None,
        settings=settings,
        store=store,
        client=None,
        counter=counter,
        completion_id="test",
        model="claude-opus-5",
        params={"model": "claude-opus-5", "messages": []},
        stream=False,
    )


def impostazioni(**budget) -> Settings:
    settings = Settings(profilo="prudente")
    settings.budget.enabled = True
    settings.budget.daily_usd = budget.get("daily_usd", 5.0)
    settings.budget.monthly_usd = budget.get("monthly_usd", 100.0)
    settings.budget.precount = budget.get("precount", False)
    return settings


# --- quando deve bloccare --------------------------------------------------


async def test_sotto_il_tetto_lascia_passare():
    settings = impostazioni(daily_usd=5.0)
    ctx = contesto(settings, StoreFinto(oggi=1.0, mese=1.0))
    await BudgetStage(settings).before(ctx)  # non deve sollevare


async def test_un_tetto_raggiunto_e_un_tetto_esaurito():
    """Il confronto e' >=, non >, e non e' un dettaglio.

    Con > una spesa esattamente pari al tetto lascerebbe passare ancora una
    richiesta, e il tetto risulterebbe sistematicamente superato di una.
    """
    settings = impostazioni(daily_usd=5.0)
    ctx = contesto(settings, StoreFinto(oggi=5.0))
    with pytest.raises(PipelineAbort):
        await BudgetStage(settings).before(ctx)


async def test_un_tetto_a_zero_impedisce_ogni_spesa():
    """E' il modo piu' diretto di mettere il gateway in sola lettura."""
    settings = impostazioni(daily_usd=0.0)
    ctx = contesto(settings, StoreFinto(oggi=0.0))
    with pytest.raises(PipelineAbort):
        await BudgetStage(settings).before(ctx)


async def test_il_tetto_mensile_blocca_anche_se_quello_di_oggi_e_intatto():
    """Sono due domande diverse: oggi non ho speso, ma il mese e' finito."""
    settings = impostazioni(daily_usd=5.0, monthly_usd=100.0)
    ctx = contesto(settings, StoreFinto(oggi=0.0, mese=100.0))
    with pytest.raises(PipelineAbort) as esito:
        await BudgetStage(settings).before(ctx)
    assert "mensile" in str(esito.value)


async def test_il_messaggio_dice_quanto_si_e_speso_e_quanto_si_poteva():
    """Un blocco senza numeri costringe a indovinare se alzare il tetto."""
    settings = impostazioni(daily_usd=5.0)
    ctx = contesto(settings, StoreFinto(oggi=7.5))
    with pytest.raises(PipelineAbort) as esito:
        await BudgetStage(settings).before(ctx)
    messaggio = str(esito.value)
    assert "7.5" in messaggio and "5.00" in messaggio


# --- il preventivo ---------------------------------------------------------


async def test_col_preventivo_si_blocca_prima_di_sforare_non_dopo():
    """La richiesta grossa che sfora e' proprio quella da fermare.

    Senza preventivo il tetto viene rispettato in media e superato sull'ultima
    richiesta, che e' spesso la piu' cara: e' il caso in cui una rete di
    sicurezza serve davvero.
    """
    settings = impostazioni(daily_usd=5.0, precount=True)
    # 4,90 gia' spesi, e questa richiesta ne costerebbe 0,50: da sola non
    # sforerebbe nessun controllo fatto a posteriori.
    counter = ContatoreFinto(tokens=100_000)  # 100k * $5/Mtok = $0,50
    ctx = contesto(settings, StoreFinto(oggi=4.90), counter)
    with pytest.raises(PipelineAbort) as esito:
        await BudgetStage(settings).before(ctx)
    assert "aggiungerebbe" in str(esito.value)
    assert counter.chiamate == 1


async def test_senza_preventivo_non_si_chiama_il_contatore():
    """Ogni chiamata a count_tokens e' un round-trip: non si fa per abitudine."""
    settings = impostazioni(daily_usd=5.0, precount=False)
    counter = ContatoreFinto(tokens=100_000)
    ctx = contesto(settings, StoreFinto(oggi=1.0), counter)
    await BudgetStage(settings).before(ctx)
    assert counter.chiamate == 0


async def test_il_preventivo_conta_solo_l_input():
    """L'output non e' prevedibile prima della risposta.

    Stimarlo per eccesso bloccherebbe richieste legittime, che e' il modo piu'
    facile di rendere inutilizzabile una rete di sicurezza: la si spegne.
    """
    settings = impostazioni(daily_usd=1.0, precount=True)
    # 100k token di input su Opus 5 valgono $0,50. Se si contasse anche un
    # output plausibile si supererebbe $1 e questa passerebbe a bloccare.
    counter = ContatoreFinto(tokens=100_000)
    ctx = contesto(settings, StoreFinto(oggi=0.0), counter)
    await BudgetStage(settings).before(ctx)
    assert ctx.estimated_prompt_tokens == 100_000


# --- l'avviso --------------------------------------------------------------


async def test_avvisa_quando_il_budget_sta_per_finire():
    """Il salto fra "tutto bene" e "bloccato" deve avere un gradino in mezzo."""
    settings = impostazioni(daily_usd=10.0)
    ctx = contesto(settings, StoreFinto(oggi=9.5))
    await BudgetStage(settings).before(ctx)
    assert any("quasi esaurito" in nota for nota in ctx.notes), ctx.notes


async def test_non_avvisa_quando_il_budget_e_ampio():
    settings = impostazioni(daily_usd=10.0)
    ctx = contesto(settings, StoreFinto(oggi=1.0))
    await BudgetStage(settings).before(ctx)
    assert not any("quasi esaurito" in nota for nota in ctx.notes)


# --- l'interruttore --------------------------------------------------------


def test_spento_lo_stadio_non_entra_nella_catena():
    settings = Settings(profilo="prudente")
    settings.budget.enabled = False
    assert BudgetStage(settings).enabled is False
