"""Test della riscrittura del prompt.

Il vincolo verificato piu' volte qui e' l'idempotenza. Un client OpenAI
rispedisce l'intera cronologia a ogni turno, quindi lo stesso testo passa dalla
riscrittura molte volte: se il risultato cambiasse fra un passaggio e l'altro
cambierebbe il prefisso del prompt e salterebbe la cache. Si risparmierebbero
token pagandoli dieci volte tanto.
"""

from __future__ import annotations

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.prompt import PromptOptimizerStage
from ecotokens.prompt_opt import (
    SUBSTITUTIONS,
    OptimizerConfig,
    _catena_chiusa,
    normalize,
    optimize_text,
)
from ecotokens.translate.to_anthropic import build_anthropic_params


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


# --- trasformazioni -------------------------------------------------------


def test_normalizzazione_non_tocca_le_parole():
    testo = "Ciao   mondo.\n\n\n\nSeconda   riga.   "
    assert normalize(testo) == "Ciao mondo.\n\nSeconda riga."


def test_normalizzazione_lascia_stare_il_codice():
    """Dentro un blocco recintato l'indentazione e' significato, non spreco."""
    testo = "Esempio:\n\n```python\ndef f():\n    return   1\n```\n\nFine   qui."
    uscita = normalize(testo)
    assert "def f():\n    return   1" in uscita
    assert "Fine qui." in uscita


def test_normalizzazione_toglie_i_caratteri_invisibili():
    testo = "parola​" + "x" * 300
    assert "​" not in normalize(testo)


def test_normalizzazione_e_idempotente():
    testo = "Uno   due tre.\n\n\n\nQuattro   \n```\n  cinque\n```"
    una = normalize(testo)
    assert normalize(una) == una


def test_le_sostituzioni_non_si_innescano_a_vicenda():
    """Se ``a -> b`` e ``b -> c`` convivessero, applicare due volte darebbe
    risultati diversi e il prefisso non sarebbe piu' stabile."""
    assert _catena_chiusa(SUBSTITUTIONS) == []


def test_riscrittura_completa_idempotente():
    config = OptimizerConfig(strip_filler=True, substitute=True, only_verified=False)
    testo = (
        "E' importante notare che devi utilizzare il formato JSON. "
        "Per favore, al fine di evitare errori, in order to be safe, "
        "please note that you must utilize the schema."
    )
    prima = optimize_text(testo, config)
    seconda = optimize_text(prima.text, config)
    assert seconda.text == prima.text
    assert seconda.applied == []
    assert len(prima.text) < len(testo)


def test_niente_punteggiatura_orfana():
    """Togliere 'Per favore,' non deve lasciare una virgola a inizio frase."""
    config = OptimizerConfig(strip_filler=True)
    esito = optimize_text("Fai questo. Per favore, controlla il risultato.", config)
    assert ", controlla" not in esito.text
    assert "Controlla il risultato." in esito.text


def test_sostituzioni_solo_se_verificate():
    """Il valore predefinito e' non fidarsi: senza conferma dal conteggio vero
    non si applica nulla."""
    testo = "Devi utilizzare il formato corretto in order to evitare errori. " * 6
    prudente = optimize_text(testo, OptimizerConfig(substitute=True, only_verified=True))
    assert prudente.text == normalize(testo)

    confermata = optimize_text(
        testo,
        OptimizerConfig(substitute=True, only_verified=True, verified=frozenset({"utilizzare"})),
    )
    assert "usare" in confermata.text
    assert "in order to" in confermata.text, "le altre restano non applicate"


# --- stadio ---------------------------------------------------------------


async def test_lo_stadio_riscrive_system_e_user(settings):
    settings.prompt.strip_filler = True
    ctx = make_context(
        settings,
        [
            {"role": "system", "content": "E' importante notare che devi rispondere. " * 20},
            {"role": "user", "content": "Per favore, dimmi come funziona il sistema. " * 20},
        ],
    )
    await PromptOptimizerStage(settings).before(ctx)

    assert "e' importante notare che" not in ctx.params["system"][0]["text"].lower()
    assert "per favore" not in str(ctx.params["messages"][0]["content"]).lower()
    assert ctx.prompt_tokens_removed > 0


async def test_lo_stadio_non_tocca_i_messaggi_assistant(settings):
    """Riscriverli sarebbe falsificare il verbale: sono parole gia' dette."""
    settings.prompt.strip_filler = True
    detto = "E' importante notare che ho controllato il file. " * 20
    ctx = make_context(
        settings,
        [
            {"role": "user", "content": "domanda " * 60},
            {"role": "assistant", "content": detto},
            {"role": "user", "content": "seconda domanda " * 40},
        ],
    )
    await PromptOptimizerStage(settings).before(ctx)

    assistente = ctx.params["messages"][1]
    assert detto.strip() in str(assistente["content"])


async def test_lo_stadio_non_tocca_i_tool_result(settings):
    """Un tool result e' un'osservazione del mondo, non una nostra istruzione."""
    settings.prompt.strip_filler = True
    esito = "E' importante notare che il file contiene 42 righe.   " * 20
    ctx = make_context(
        settings,
        [
            {"role": "user", "content": "leggi il file " * 40},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": esito},
            {"role": "user", "content": "e adesso? " * 40},
        ],
    )
    await PromptOptimizerStage(settings).before(ctx)

    testo_completo = str(ctx.params["messages"])
    assert "E' importante notare che il file contiene 42 righe." in testo_completo


async def test_lo_stadio_e_idempotente_fra_i_turni(settings):
    """La prova che conta: la cronologia rispedita deve riscriversi identica.

    Un client OpenAI rimanda tutto a ogni turno. Se la riscrittura del turno 2
    producesse un testo diverso da quella del turno 1, il prefisso cambierebbe
    e la cache mancherebbe: si risparmierebbero token pagandoli dieci volte.
    """
    settings.prompt.strip_filler = True
    sistema = {"role": "system", "content": "Per favore, rispondi bene.   " * 30}
    domanda = {"role": "user", "content": "E' importante notare che voglio sapere. " * 20}

    primo = make_context(settings, [sistema, domanda])
    await PromptOptimizerStage(settings).before(primo)
    prefisso_primo = primo.params["system"][0]["text"]
    domanda_prima = str(primo.params["messages"][0]["content"])

    secondo = make_context(
        settings,
        [sistema, domanda, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "poi?"}],
    )
    await PromptOptimizerStage(settings).before(secondo)

    assert secondo.params["system"][0]["text"] == prefisso_primo
    assert str(secondo.params["messages"][0]["content"]) == domanda_prima


async def test_i_testi_corti_non_si_toccano(settings):
    """Sotto la soglia il guadagno e' rumore e il rischio resta intero."""
    settings.prompt.strip_filler = True
    ctx = make_context(settings, [{"role": "user", "content": "Per favore, ciao."}])
    await PromptOptimizerStage(settings).before(ctx)
    assert "Per favore" in str(ctx.params["messages"][0]["content"])
    assert ctx.prompt_tokens_removed == 0


async def test_distingue_i_token_tolti_dentro_e_fuori_dalla_cache(settings):
    """Un token tolto al prefisso in cache vale un decimo di uno tolto in coda."""
    settings.prompt.strip_filler = True
    ctx = make_context(
        settings,
        [
            {"role": "user", "content": "Per favore, prima domanda molto lunga. " * 20},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "Per favore, ultima domanda molto lunga. " * 20},
        ],
    )
    await PromptOptimizerStage(settings).before(ctx)

    assert ctx.prompt_tokens_removed > ctx.prompt_tokens_removed_uncached > 0
