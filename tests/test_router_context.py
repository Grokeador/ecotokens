"""Test del router e della gestione del contesto."""

from __future__ import annotations

import pytest

from ecotokens.api.schemas import ChatCompletionRequest
from ecotokens.config import Settings
from ecotokens.pipeline.base import RequestContext
from ecotokens.pipeline.context import CONTEXT_MANAGEMENT_BETA, ContextStage
from ecotokens.pipeline.router import RouterStage
from ecotokens.store.db import Database
from ecotokens.store.repos import Store
from ecotokens.translate.to_anthropic import build_anthropic_params


class FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [FakeBlock(text)]


class FakeMessages:
    """Client finto per il riassunto: conta quante volte viene chiamato."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return FakeMessage(f"riassunto numero {self.calls}")


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


@pytest.fixture
def settings() -> Settings:
    """Profilo prudente: qui si prova la politica adattiva del router.

    Quella incondizionata del profilo aggressivo e' un'altra politica, con
    altre garanzie, e ha i suoi test in `test_profilo.py`.
    """
    return Settings(profilo="prudente")


@pytest.fixture
def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


def make_context(settings, messages, *, store=None, client=None, session=None, **overrides):
    payload = {"model": "claude-opus-5", "messages": messages}
    payload.update(overrides)
    request = ChatCompletionRequest.model_validate(payload)
    translation = build_anthropic_params(request, settings)
    return RequestContext(
        request=request,
        settings=settings,
        store=store,
        client=client,
        counter=None,
        completion_id="test",
        model=translation.model,
        params=translation.params,
        stream=False,
        client_effort=request.reasoning_effort,
        session=session,
    )


# --- router --------------------------------------------------------------


async def test_effort_abbassato_su_richiesta_semplice(settings):
    ctx = make_context(settings, [{"role": "user", "content": "Che ore sono a Roma?"}])
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "low"


async def test_effort_a_meta_strada_con_i_tool(settings):
    """Prima qui c'era un rifiuto in blocco, e copriva il 45% del traffico.

    Toglierlo del tutto varrebbe l'11,4% del costo, ma il banco misura la
    lunghezza della risposta, non la sua qualita': un effort basso su un turno
    agentico puo' produrre la chiamata sbagliata, e un tentativo in piu' costa
    piu' di quanto l'effort abbia risparmiato. Il valore predefinito prende
    quindi la meta' sicura del premio.
    """
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Che ore sono?"}],
        tools=[{"type": "function", "function": {"name": "ora", "parameters": {"type": "object"}}}],
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "medium"


async def test_il_veto_in_blocco_sui_tool_si_puo_ripristinare(settings):
    settings.router.effort_with_tools = "off"
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Che ore sono?"}],
        tools=[{"type": "function", "function": {"name": "ora", "parameters": {"type": "object"}}}],
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "high"
    assert any("puo' chiamare un tool" in nota for nota in ctx.notes)


async def test_tool_choice_none_non_conta_come_turno_agentico(settings):
    """I tool sono dichiarati ma inutilizzabili: nessuna scelta da fare."""
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Che ore sono?"}],
        tools=[{"type": "function", "function": {"name": "ora", "parameters": {"type": "object"}}}],
        tool_choice="none",
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "low"


async def test_il_modello_non_cambia_mai_su_un_turno_con_tool(settings):
    """Il veto resta dov'e' piu' pericoloso: un modello diverso sceglie i tool
    in modo diverso, e l'errore non si paga in token ma in tentativi."""
    settings.router.model_downgrade = True
    ctx = make_context(
        settings,
        [{"role": "user", "content": "Che ore sono?"}],
        tools=[{"type": "function", "function": {"name": "ora", "parameters": {"type": "object"}}}],
    )
    ctx.history_turns = 0
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-opus-5"


async def test_effort_invariato_se_lo_chiede_il_client(settings):
    ctx = make_context(
        settings, [{"role": "user", "content": "Che ore sono?"}], reasoning_effort="max"
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "max"


async def test_effort_invariato_su_richiesta_complessa(settings):
    ctx = make_context(
        settings, [{"role": "user", "content": "Analizza questo problema e spiega perche'"}]
    )
    await RouterStage(settings).before(ctx)
    assert ctx.params["output_config"]["effort"] == "high"


async def test_modello_non_cambia_di_default(settings):
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-opus-5"


async def test_modello_non_cambia_a_meta_conversazione(settings):
    """Cambiarlo azzererebbe la cache accumulata, che costa piu' del risparmio."""
    settings.router.model_downgrade = True
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    ctx.history_turns = 3
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-opus-5"
    assert any("cache e' legata al modello" in nota for nota in ctx.notes)


async def test_modello_declassato_a_inizio_sessione(settings):
    settings.router.model_downgrade = True
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    ctx.history_turns = 0
    await RouterStage(settings).before(ctx)
    assert ctx.model == "claude-haiku-4-5"
    assert ctx.params["model"] == "claude-haiku-4-5"
    assert any("soglia" in nota or "richiede almeno" in nota for nota in ctx.notes)


# --- contesto ------------------------------------------------------------


async def test_potatura_non_attiva_sotto_la_soglia(settings):
    ctx = make_context(settings, [{"role": "user", "content": "ciao"}])
    await ContextStage(settings).before(ctx)
    assert "context_management" not in ctx.params
    assert ctx.betas == []


async def test_potatura_attiva_con_prompt_grande(settings):
    # Finestra ridotta artificialmente per non dover generare un milione di token.
    settings.context.trigger_ratio = 0.0001
    ctx = make_context(settings, [{"role": "user", "content": "testo " * 500}])
    await ContextStage(settings).before(ctx)

    assert ctx.params["context_management"]["edits"] == [{"type": "clear_tool_uses_20250919"}]
    assert CONTEXT_MANAGEMENT_BETA in ctx.betas


async def test_riassunto_calcolato_una_volta_e_riusato(settings, store):
    """Il punto cruciale: il riassunto deve restare identico tra i turni.

    Se cambiasse a ogni richiesta cambierebbe il prefisso del prompt, e la
    cache mancherebbe sempre: la compattazione costerebbe piu' di quanto fa
    risparmiare.
    """
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2
    settings.context.recompute_every_messages = 4
    settings.context.min_gain_tokens = 0

    messaggi = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]
    sessione = await store.create_session("fp", "claude-opus-5")
    client = FakeClient()

    primo = make_context(settings, messaggi, store=store, client=client, session=sessione)
    await ContextStage(settings).before(primo)
    testo_primo = primo.params["messages"][0]["content"][0]["text"]

    secondo = make_context(settings, messaggi, store=store, client=client, session=sessione)
    await ContextStage(settings).before(secondo)
    testo_secondo = secondo.params["messages"][0]["content"][0]["text"]

    assert "riassunto numero 1" in testo_primo
    assert testo_primo == testo_secondo, "il prefisso deve restare identico byte per byte"
    assert client.messages.calls == 1, "il riassunto non va ricalcolato a ogni turno"
    assert any("riusato il riassunto" in nota for nota in secondo.notes)


async def test_riassunto_fallito_non_rompe_la_richiesta(settings, store):
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2
    settings.context.recompute_every_messages = 4
    settings.context.min_gain_tokens = 0

    class ClientRotto:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("API non raggiungibile")

    messaggi = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]
    sessione = await store.create_session("fp2", "claude-opus-5")
    ctx = make_context(settings, messaggi, store=store, client=ClientRotto(), session=sessione)
    prima = len(ctx.params["messages"])

    await ContextStage(settings).before(ctx)

    assert len(ctx.params["messages"]) == prima, "la conversazione resta integrale"
    assert any("non riuscito" in nota for nota in ctx.notes)


async def test_il_taglio_non_insegue_la_conversazione(settings, store):
    """La regressione che il test precedente non coglieva.

    Con una cronologia ferma il riassunto si riusava anche prima. Il difetto si
    vedeva solo facendo crescere la conversazione: il taglio seguiva la coda,
    si spostava di due messaggi a ogni turno, e il riassunto veniva rifatto -
    diverso - a ogni richiesta. Prefisso nuovo ogni volta, cache mai riletta.
    """
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2
    settings.context.recompute_every_messages = 8
    settings.context.min_gain_tokens = 0

    sessione = await store.create_session("fp3", "claude-opus-5")
    client = FakeClient()

    messaggi = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]

    prefissi = []
    for aggiunta in range(4):  # la conversazione cresce di due messaggi per turno
        storia = messaggi + [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"nuovo {index} " * 20}
            for index in range(aggiunta * 2)
        ]
        ctx = make_context(settings, storia, store=store, client=client, session=sessione)
        await ContextStage(settings).before(ctx)
        prefissi.append(ctx.params["messages"][0]["content"][0]["text"])

    assert len(set(prefissi)) == 1, "il prefisso deve restare identico mentre la chat cresce"
    assert client.messages.calls == 1, "un solo riassunto per scatto, non uno per turno"


async def test_il_riassunto_riparte_da_quello_precedente(settings, store):
    """Incrementale: al riassuntore vanno solo i messaggi nuovi."""
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2
    settings.context.recompute_every_messages = 4
    settings.context.min_gain_tokens = 0
    settings.context.incremental_summary = True

    sessione = await store.create_session("fp4", "claude-opus-5")

    class ClientSpia(FakeClient):
        def __init__(self):
            super().__init__()
            self.corpi = []
            originale = self.messages.create

            async def create(**kwargs):
                self.corpi.append(kwargs["messages"][0]["content"][0]["text"])
                return await originale(**kwargs)

            self.messages.create = create

    client = ClientSpia()
    storia = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]
    ctx = make_context(settings, storia, store=store, client=client, session=sessione)
    await ContextStage(settings).before(ctx)

    # Cresce oltre lo scatto successivo: il taglio avanza e serve un riassunto nuovo.
    storia = storia + [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"nuovo {index} " * 20}
        for index in range(6)
    ]
    ctx = make_context(settings, storia, store=store, client=client, session=sessione)
    await ContextStage(settings).before(ctx)

    assert len(client.corpi) == 2
    assert "<appunti-finora>" in client.corpi[1], "il secondo riassunto parte dal primo"
    assert "messaggio 0" not in client.corpi[1], "la cronologia vecchia non si rilegge"


async def test_la_trascrizione_al_riassuntore_e_troncata(settings, store):
    """Rispedire per intero un file letto significa pagarlo due volte."""
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2
    settings.context.recompute_every_messages = 4
    settings.context.min_gain_tokens = 0
    settings.context.transcript_block_chars = 200

    sessione = await store.create_session("fp5", "claude-opus-5")
    corpi = []

    class ClientSpia(FakeClient):
        def __init__(self):
            super().__init__()
            originale = self.messages.create

            async def create(**kwargs):
                corpi.append(kwargs["messages"][0]["content"][0]["text"])
                return await originale(**kwargs)

            self.messages.create = create

    storia = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"blocco {index} " * 400}
        for index in range(12)
    ]
    ctx = make_context(settings, storia, store=store, client=ClientSpia(), session=sessione)
    await ContextStage(settings).before(ctx)

    assert "[…]" in corpi[0], "i blocchi lunghi vanno troncati al centro"
    integrale = sum(len(m["content"]) for m in storia[:8])
    assert len(corpi[0]) < integrale / 4


async def test_non_si_comprime_quando_non_conviene(settings, store):
    """Il riassunto costa una chiamata: comprimere poco non la ripaga."""
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2
    settings.context.recompute_every_messages = 4
    settings.context.min_gain_tokens = 100_000

    sessione = await store.create_session("fp6", "claude-opus-5")
    client = FakeClient()
    storia = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]
    ctx = make_context(settings, storia, store=store, client=client, session=sessione)
    prima = len(ctx.params["messages"])
    await ContextStage(settings).before(ctx)

    assert len(ctx.params["messages"]) == prima
    assert client.messages.calls == 0
    assert any("compattazione saltata" in nota for nota in ctx.notes)


async def test_il_costo_del_riassunto_viene_addebitato(settings, store):
    """Uno stadio che sembra gratuito viene acceso quando non conviene."""
    settings.context.trigger_ratio = 0.0001
    settings.context.hard_ratio = 0.0001
    settings.context.keep_recent_messages = 2
    settings.context.recompute_every_messages = 4
    settings.context.min_gain_tokens = 0

    class FakeUsage:
        input_tokens = 5_000
        output_tokens = 400
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    class ClientConUsage(FakeClient):
        def __init__(self):
            super().__init__()
            originale = self.messages.create

            async def create(**kwargs):
                messaggio = await originale(**kwargs)
                messaggio.usage = FakeUsage()
                return messaggio

            self.messages.create = create

    sessione = await store.create_session("fp7", "claude-opus-5")
    storia = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"messaggio {index} " * 20}
        for index in range(12)
    ]
    ctx = make_context(settings, storia, store=store, client=ClientConUsage(), session=sessione)
    await ContextStage(settings).before(ctx)

    assert ctx.aux_cost_usd > 0
    assert ctx.aux_usage.input_tokens == 5_000
    assert ctx.total_cost_usd == ctx.cost_usd + ctx.aux_cost_usd
