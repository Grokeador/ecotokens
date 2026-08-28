"""Test della cache semantica.

E' l'unico stadio del progetto che puo' restituire una risposta **sbagliata**:
serve una risposta gia' data a una domanda solo simile. Per questo e' spento di
default - ma restava anche il codice meno provato del gateway, 112 istruzioni
al 22%, che e' la combinazione peggiore: spedito, rischioso e non verificato.

Il modello di embedding vero si scarica dalla rete, e un test che tocca la rete
non e' un test. Da qui la cucitura: lo stadio accetta un embedder qualunque,
purche' abbia `embed(testi)`. Quello usato qui e' deterministico e scritto a
mano, cosi' le somiglianze sono decise dal test invece che da un modello.
"""

from __future__ import annotations

import math

import pytest

from ecotokens.config import Settings
from ecotokens.pipeline.base import SOURCE_API, SOURCE_SEMANTIC_CACHE, RequestContext
from ecotokens.pipeline.semantic_cache import SemanticCacheStage
from ecotokens.pricing import Usage
from ecotokens.store.db import Database
from ecotokens.store.repos import Store


class EmbedderFinto:
    """Vettori decisi dal test, non da un modello.

    Ogni testo noto ha un angolo assegnato sul cerchio unitario: due testi a
    pochi gradi di distanza hanno coseno vicino a 1, due opposti vicino a 0.
    Cosi' la soglia si puo' provare esattamente invece che per tentativi.
    """

    def __init__(self, angoli: dict[str, float]) -> None:
        self.angoli = angoli
        self.chiamate: list[str] = []

    def embed(self, testi):
        for testo in testi:
            self.chiamate.append(testo)
            angolo = self.angoli.get(testo, 0.0)
            yield [math.cos(angolo), math.sin(angolo)]


@pytest.fixture
async def store():
    database = Database(":memory:")
    database.connect()
    yield Store(database)
    database.close()


def impostazioni(soglia: float = 0.97) -> Settings:
    settings = Settings(profilo="prudente")
    settings.semantic_cache.enabled = True
    settings.semantic_cache.similarity_threshold = soglia
    return settings


def contesto(settings, store, domanda: str, *, chiave: str = "k1", **extra) -> RequestContext:
    params = {
        "model": "claude-opus-5",
        "system": [{"type": "text", "text": "sei un assistente"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": domanda}]}],
    }
    params.update(extra)
    ctx = RequestContext(
        request=None,
        settings=settings,
        store=store,
        client=None,
        counter=None,
        completion_id="test",
        model="claude-opus-5",
        params=params,
        stream=False,
    )
    ctx.cache_key = chiave
    return ctx


async def memorizza(store, stadio, settings, domanda: str, chiave: str, testo: str) -> None:
    """Registra una risposta come farebbe una richiesta andata all'API."""
    await store.put_cached(
        key=chiave,
        model="claude-opus-5",
        response={
            "id": chiave,
            "model": "claude-opus-5",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": testo},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        },
        usage=Usage(input_tokens=100, output_tokens=10),
        ttl_seconds=3600,
    )
    ctx = contesto(settings, store, domanda, chiave=chiave)
    ctx.source = SOURCE_API
    ctx.upstream_response = {"id": chiave}
    await stadio.after(ctx, message=object())


# --- la garanzia che conta di piu' -----------------------------------------


async def test_una_domanda_diversa_non_viene_servita_dalla_cache(store):
    """Il rischio proprio di questo stadio, e la sua unica difesa.

    "Quanto fa 2+2" e "quanto fa 2+3" sono vicinissime nello spazio degli
    embedding e hanno risposte giuste diverse. Se la soglia non tenesse, lo
    stadio non sarebbe un'ottimizzazione: sarebbe un generatore di errori.
    """
    settings = impostazioni(soglia=0.97)
    # 0,4 radianti di distanza: coseno ~0,921, sotto la soglia.
    embedder = EmbedderFinto({"quanto fa 2+2": 0.0, "quanto fa 2+3": 0.4})
    stadio = SemanticCacheStage(settings, embedder=embedder)

    await memorizza(store, stadio, settings, "quanto fa 2+2", "k1", "4")

    ctx = contesto(settings, store, "quanto fa 2+3", chiave="k2")
    await stadio.before(ctx)
    assert ctx.short_circuit is None
    assert ctx.source != SOURCE_SEMANTIC_CACHE


async def test_una_domanda_quasi_identica_viene_servita(store):
    settings = impostazioni(soglia=0.97)
    # 0,1 radianti: coseno ~0,995, sopra la soglia.
    embedder = EmbedderFinto({"come sta il tempo": 0.0, "come e' il tempo": 0.1})
    stadio = SemanticCacheStage(settings, embedder=embedder)

    await memorizza(store, stadio, settings, "come sta il tempo", "k1", "sereno")

    ctx = contesto(settings, store, "come e' il tempo", chiave="k2")
    await stadio.before(ctx)
    assert ctx.source == SOURCE_SEMANTIC_CACHE
    assert ctx.short_circuit is not None
    assert ctx.cost_usd == 0.0
    assert ctx.saved_usd > 0, "un hit deve entrare nel conto del risparmio"


async def test_la_soglia_e_rispettata_al_bordo(store):
    """Chi la alza deve ottenere davvero piu' prudenza, non un'impressione."""
    domande = {"a": 0.0, "b": 0.2}  # coseno ~0,980
    for soglia, atteso in ((0.95, SOURCE_SEMANTIC_CACHE), (0.99, SOURCE_API)):
        settings = impostazioni(soglia=soglia)
        stadio = SemanticCacheStage(settings, embedder=EmbedderFinto(domande))
        database = Database(":memory:")
        database.connect()
        try:
            locale = Store(database)
            await memorizza(locale, stadio, settings, "a", "k1", "risposta")
            ctx = contesto(settings, locale, "b", chiave="k2")
            ctx.source = SOURCE_API
            await stadio.before(ctx)
            assert ctx.source == atteso, f"soglia {soglia}"
        finally:
            database.close()


# --- cosa non viene mai servito dalla cache --------------------------------


async def test_le_richieste_con_tool_non_si_servono_mai_da_cache(store):
    """Il risultato dipende da uno stato esterno che la cache non conosce."""
    settings = impostazioni()
    embedder = EmbedderFinto({"leggi il file": 0.0})
    stadio = SemanticCacheStage(settings, embedder=embedder)

    await memorizza(store, stadio, settings, "leggi il file", "k1", "fatto")

    ctx = contesto(
        settings,
        store,
        "leggi il file",
        chiave="k2",
        tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
    )
    await stadio.before(ctx)
    assert ctx.short_circuit is None


async def test_un_prompt_di_sistema_diverso_isola_le_voci(store):
    """La stessa domanda sotto istruzioni diverse e' un'altra domanda.

    Senza questo isolamento un assistente legale e uno medico che condividono
    il gateway si scambierebbero le risposte.
    """
    settings = impostazioni()
    embedder = EmbedderFinto({"cosa devo fare": 0.0})
    stadio = SemanticCacheStage(settings, embedder=embedder)

    await memorizza(store, stadio, settings, "cosa devo fare", "k1", "prima risposta")

    ctx = contesto(settings, store, "cosa devo fare", chiave="k2")
    ctx.params["system"] = [{"type": "text", "text": "sei un assistente diverso"}]
    await stadio.before(ctx)
    assert ctx.short_circuit is None


async def test_una_domanda_vuota_non_produce_ne_letture_ne_scritture(store):
    settings = impostazioni()
    embedder = EmbedderFinto({})
    stadio = SemanticCacheStage(settings, embedder=embedder)

    ctx = contesto(settings, store, "   ", chiave="k1")
    await stadio.before(ctx)
    assert ctx.short_circuit is None
    assert not embedder.chiamate, "non ha senso calcolare l'embedding del nulla"


# --- la normalizzazione ----------------------------------------------------


async def test_il_testo_viene_normalizzato_prima_dell_embedding(store):
    """Stessa normalizzazione della cache esatta.

    Gli embedding assorbono quasi tutto uno spazio doppio, ma non del tutto:
    normalizzare toglie una fonte di rumore che sposta il coseno senza che
    nessuna parola sia cambiata.
    """
    settings = impostazioni()
    embedder = EmbedderFinto({})
    stadio = SemanticCacheStage(settings, embedder=embedder)

    ctx = contesto(settings, store, "ciao    come   stai", chiave="k1")
    await stadio.before(ctx)
    assert embedder.chiamate == ["ciao come stai"]


# --- i backend -------------------------------------------------------------


def test_senza_numpy_o_fastembed_lo_stadio_si_spegne_invece_di_rompersi():
    """Uno stadio opzionale che non trova le sue dipendenze non deve fermare il gateway."""
    settings = impostazioni()
    stadio = SemanticCacheStage(settings)  # nessun embedder, fastembed assente
    assert stadio.enabled is False


def test_un_embedder_proprio_basta_a_farlo_funzionare():
    """Chi ha gia' un servizio di embedding non deve installarne un secondo."""
    settings = impostazioni()
    stadio = SemanticCacheStage(settings, embedder=EmbedderFinto({}))
    assert stadio.enabled is True


def test_spento_non_carica_niente():
    settings = Settings(profilo="prudente")
    settings.semantic_cache.enabled = False
    stadio = SemanticCacheStage(settings)
    assert stadio.enabled is False
