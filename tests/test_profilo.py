"""Test dei due profili.

Il profilo e' l'unica impostazione del progetto che sposta il gateway da
"non tocca mai il contenuto di una risposta" a "lo tocca sempre". Vale venti
punti di risparmio misurato e un costo che il banco non sa misurare, quindi la
cosa importante da fissare non e' il guadagno - quello si vede - ma le
garanzie: cosa il profilo aggressivo continua a **non** fare.
"""

from __future__ import annotations

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.router import RouterStage
from ecotokens.translate.to_anthropic import build_anthropic_params


def make_context(settings: Settings, messaggi, **extra):
    payload = {"model": "claude-opus-5", "messages": messaggi}
    payload.update(extra)
    request = ChatCompletionRequest.model_validate(payload)
    traduzione = build_anthropic_params(request, settings)
    ctx = RequestContext(
        request=request,
        settings=settings,
        store=None,
        client=None,
        counter=None,
        completion_id="test",
        model=traduzione.model,
        params=traduzione.params,
        stream=False,
        client_effort=request.reasoning_effort,
    )
    return ctx


# --- cosa fa ogni profilo -------------------------------------------------


def test_il_profilo_predefinito_e_aggressivo():
    """Se cambiasse, cambierebbe in silenzio il comportamento di tutti."""
    assert Settings().profilo == "aggressivo"


def test_il_profilo_prudente_non_tocca_ne_modello_ne_effort():
    prudente = Settings(profilo="prudente")
    assert prudente.router.model_downgrade is False
    assert prudente.router.downgrade_policy == "semplici"
    assert prudente.router.effort_policy == "adattivo"


def test_i_due_profili_si_possono_scambiare_a_caldo():
    """Le due funzioni devono essere l'una l'inversa dell'altra."""
    settings = Settings(profilo="aggressivo")
    settings.applica_profilo_prudente()
    assert settings.router.model_downgrade is False
    settings.applica_profilo_aggressivo()
    assert settings.router.model_downgrade is True
    assert settings.router.downgrade_policy == "sempre"


def test_un_campo_scritto_a_mano_vince_sul_profilo():
    """Il profilo imposta dei default, non una politica inviolabile."""
    settings = Settings(profilo="aggressivo", router={"model_downgrade": False})
    assert settings.router.model_downgrade is False


# --- cosa il profilo aggressivo fa -----------------------------------------


async def test_aggressivo_declassa_anche_una_domanda_difficile():
    settings = Settings(profilo="aggressivo")
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Dimostra il teorema di Noether " * 40}],
    )
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-haiku-4-5"
    assert ctx.params["output_config"]["effort"] == "low"


async def test_aggressivo_declassa_anche_un_turno_con_tool():
    """Il veto sui tool cade: e' precisamente cio' che si sta comprando."""
    settings = Settings(profilo="aggressivo")
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Leggi il file e riassumilo"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
    )
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-haiku-4-5"


async def test_prudente_lascia_il_modello_dove_l_ha_chiesto_il_client():
    settings = Settings(profilo="prudente")
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-opus-5"


# --- le garanzie che restano ----------------------------------------------


async def test_un_effort_chiesto_dal_client_non_si_tocca_mai():
    """La garanzia che sopravvive a qualunque profilo.

    Il gateway puo' decidere al posto di chi non ha deciso. Su chi ha deciso,
    no: sovrascrivere una scelta esplicita non e' un'ottimizzazione, e'
    ignorare un'istruzione.
    """
    settings = Settings(profilo="aggressivo")
    ctx = make_context(
        settings,
        [{"role": "user", "content": "ciao"}],
        reasoning_effort="max",
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "max"


async def test_il_declassamento_avvisa_quando_alza_la_soglia_di_cache():
    """Haiku vuole 4096 token di prefisso contro i 512 di Opus.

    Fra le due soglie la cache non si forma e l'API non lo segnala: e' il tipo
    di perdita che si nota solo mesi dopo, sulla fattura. Il gateway deve
    lasciarne traccia nelle note della richiesta.
    """
    settings = Settings(profilo="aggressivo")
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    await RouterStage(settings).before(ctx)
    assert any("4096" in nota for nota in ctx.notes), ctx.notes


async def test_il_modello_resta_fisso_per_tutta_la_sessione():
    """Cambiarlo a meta' butterebbe via la cache accumulata fin li'."""
    settings = Settings(profilo="aggressivo")
    ctx = make_context(
        settings,
        [
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "ciao a te"},
            {"role": "user", "content": "e poi?"},
        ],
    )
    ctx.history_turns = 1
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-opus-5"
    assert any("conversazione gia' avviata" in nota for nota in ctx.notes), ctx.notes


# --- la misura che giustifica il profilo -----------------------------------


async def test_il_profilo_aggressivo_supera_il_95_percento():
    """Il numero per cui il profilo esiste.

    Non e' un test sul valore esatto - il corpus cresce con il codice, quindi
    la cifra si muove - ma sulla soglia: se il profilo aggressivo scendesse
    sotto il 95% qualcosa si sarebbe rotto, e conviene saperlo da un test
    invece che da una dashboard riletta per caso.
    """
    from ecotokens.bench import (
        _abilita_modello_economico,
        _run_scenario,
        make_settings,
    )
    from ecotokens.workloads import all_scenarios

    riferimento = 0.0
    completo = 0.0
    for scenario in all_scenarios():
        riferimento += (
            await _run_scenario(scenario, make_settings(None), "senza", live=False)
        ).cost_usd
        completo += (
            await _run_scenario(
                scenario, make_settings(_abilita_modello_economico), "con", live=False
            )
        ).cost_usd

    risparmio = (riferimento - completo) / riferimento
    assert risparmio >= 0.95, f"risparmio sceso al {risparmio * 100:.2f}%"


async def test_il_profilo_prudente_resta_ben_sotto_e_va_bene_cosi():
    """I venti punti di differenza sono il prezzo, e devono restare visibili.

    Se i due profili convergessero, vorrebbe dire che uno dei due ha smesso di
    fare il suo mestiere - e la scelta offerta all'utente sarebbe finta.
    """
    from ecotokens.bench import _abilita_prompt, _run_scenario, make_settings
    from ecotokens.workloads import scenario_chat

    scenario = scenario_chat(turns=6)
    riferimento = (
        await _run_scenario(scenario, make_settings(None), "senza", live=False)
    ).cost_usd
    prudente = (
        await _run_scenario(scenario, make_settings(_abilita_prompt), "pru", live=False)
    ).cost_usd

    quota = (riferimento - prudente) / riferimento
    assert 0.5 < quota < 0.95, f"il profilo prudente risparmia il {quota * 100:.1f}%"


def test_il_file_di_esempio_fa_quello_che_dichiara():
    """Il file di esempio e' il primo che chiunque copia: deve essere coerente.

    Nella prima stesura dichiarava `profilo = "aggressivo"` e poi scriveva a
    mano i valori prudenti sotto [router]. Siccome i campi espliciti vincono
    sul profilo - ed e' giusto che sia cosi' - chi lo avesse copiato avrebbe
    ottenuto il 75% credendo di avere il 95%, senza niente che glielo dicesse.
    Una configurazione che si contraddice e' peggio di una sbagliata: la
    sbagliata prima o poi si nota.
    """
    import tomllib
    from pathlib import Path

    grezzo = tomllib.loads(
        Path("ecotokens.example.toml").read_text(encoding="utf-8")
    )
    assert grezzo["profilo"] == "aggressivo"

    esempio = Settings.model_validate(grezzo)
    assert esempio.router.model_downgrade is True
    assert esempio.router.downgrade_policy == "sempre"
    assert esempio.router.effort_policy == "sempre_basso"

    # E cambiando la sola riga in testa si deve ottenere l'altro profilo.
    prudente = Settings.model_validate({**grezzo, "profilo": "prudente"})
    assert prudente.router.model_downgrade is False
    assert prudente.router.effort_policy == "adattivo"
