"""La stima locale dei token, blocco per blocco.

E' aritmetica, e per questo era coperta a meta': l'aritmetica sembra sempre
ovvia finche' non si guarda un blocco che nessuno aveva considerato. Ma su
questa stima si reggono due decisioni che costano soldi - se un prefisso supera
la soglia minima del modello, e se il tetto di spesa deve fermare la richiesta -
e sbagliarla per difetto le fa prendere entrambe al contrario.

Il conto finale non passa mai di qui: quello viene sempre da `response.usage`.
Questa stima serve a decidere **prima**, quando l'usage non esiste ancora.
"""

from __future__ import annotations

import pytest

from ecotokens.tokens import (
    TokenCounter,
    estimate_content_tokens,
    estimate_messages_tokens,
    estimate_prompt_tokens,
    estimate_tokens,
    estimate_tools_tokens,
    prefix_tokens_upto,
    strip_cache_control,
)


# --- il caso base ----------------------------------------------------------


def test_una_stringa_vuota_non_costa_niente():
    assert estimate_tokens("") == 0
    assert estimate_content_tokens(None) == 0


def test_la_stima_cresce_con_il_testo():
    corto = estimate_tokens("una frase breve")
    lungo = estimate_tokens("una frase breve " * 50)
    assert 0 < corto < lungo


def test_la_stima_approssima_per_eccesso():
    """Il verso conta piu' del valore: sottostimare fa credere che un prefisso
    stia sotto la soglia quando la supera, e viceversa. Sbagliare per eccesso
    fa rinunciare a un breakpoint; per difetto fa pagare una scrittura che non
    si forma."""
    testo = "parola " * 100
    # A ~4 caratteri per token una stima prudente sta sopra questa soglia.
    assert estimate_tokens(testo) >= len(testo) / 4


# --- i blocchi, uno per uno ------------------------------------------------


@pytest.mark.parametrize(
    "blocco, cosa",
    [
        ({"type": "text", "text": "ciao"}, "testo"),
        ({"type": "thinking", "thinking": "ragionamento " * 30}, "pensiero"),
        ({"type": "tool_use", "name": "cerca", "input": {"q": "x" * 200}}, "chiamata"),
        (
            {"type": "tool_result", "content": [{"type": "text", "text": "r" * 400}]},
            "risultato",
        ),
        ({"type": "image", "source": {"type": "base64", "data": "A" * 4000}}, "immagine"),
        ({"type": "document", "source": {"type": "base64", "data": "B" * 4000}}, "documento"),
        ({"type": "qualcosa_di_nuovo", "campo": "valore"}, "tipo mai visto"),
    ],
)
def test_ogni_tipo_di_blocco_ha_un_peso(blocco, cosa):
    """Un blocco che stima zero sparisce dal conto del prefisso, e la soglia
    minima viene valutata su un numero piu' piccolo del vero."""
    assert estimate_content_tokens(blocco) > 0, cosa


def test_un_immagine_per_url_costa_una_stima_dichiarata():
    """La sua dimensione non e' conoscibile prima di scaricarla. Zero sarebbe
    la risposta comoda e sbagliata: un prompt con tre immagini risulterebbe
    sotto soglia mentre le supera abbondantemente."""
    per_url = estimate_content_tokens(
        {"type": "image", "source": {"type": "url", "url": "https://esempio/x.png"}}
    )
    assert per_url > 1000


def test_un_documento_senza_dati_non_esplode():
    assert estimate_content_tokens({"type": "document", "source": {}}) > 0


def test_un_contenuto_che_non_e_ne_testo_ne_lista_viene_comunque_contato():
    assert estimate_content_tokens(12345) > 0


# --- prompt interi ---------------------------------------------------------


def test_il_prompt_e_la_somma_delle_sue_parti():
    params = {
        "tools": [{"name": "cerca", "input_schema": {"type": "object"}}],
        "system": [{"type": "text", "text": "istruzioni " * 50}],
        "messages": [
            {"role": "user", "content": "domanda"},
            {"role": "assistant", "content": [{"type": "text", "text": "risposta"}]},
        ],
    }
    totale = estimate_prompt_tokens(params)
    assert totale == (
        estimate_tools_tokens(params["tools"])
        + estimate_content_tokens(params["system"])
        + estimate_messages_tokens(params["messages"])
    )
    assert totale > 0


def test_senza_tool_i_tool_non_pesano():
    assert estimate_tools_tokens(None) == 0
    assert estimate_tools_tokens([]) == 0


def test_il_prefisso_fino_a_un_messaggio_cresce_col_messaggio():
    """E' il conto su cui il pianificatore decide se un breakpoint supera la
    soglia minima del modello: sotto soglia la cache non si crea e l'API tace."""
    params = {
        "system": [{"type": "text", "text": "istruzioni " * 40}],
        "messages": [{"role": "user", "content": f"turno {i} " * 20} for i in range(5)],
    }
    pesi = [prefix_tokens_upto(params, indice) for indice in range(6)]
    assert pesi == sorted(pesi)
    assert pesi[0] > 0, "il system pesa anche a zero messaggi"
    assert pesi[-1] == estimate_prompt_tokens(params)


# --- i marker non devono entrare nel conteggio -----------------------------


def test_i_marker_di_cache_spariscono_prima_del_conteggio():
    """`count_tokens` non deve vederli: contano i token, non la strategia. Se
    entrassero, la chiave di memoizzazione cambierebbe a ogni riposizionamento
    dei marker e la memoizzazione non servirebbe piu' a niente."""
    dentro = [
        {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "b"},
    ]
    fuori = strip_cache_control(dentro)
    assert fuori == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert "cache_control" in dentro[0], "l'originale non va toccato"


def test_i_marker_si_tolgono_anche_in_profondita():
    annidato = {"messages": [{"content": [{"cache_control": {}, "text": "x"}]}]}
    assert strip_cache_control(annidato) == {"messages": [{"content": [{"text": "x"}]}]}


# --- il contatore esatto, e cosa fa quando l'API non risponde --------------


class ClientFinto:
    def __init__(self, valore=None, esplode=False):
        self.valore = valore
        self.esplode = esplode
        self.chiamate = 0
        self.messages = self

    async def count_tokens(self, **kwargs):
        self.chiamate += 1
        if self.esplode:
            raise RuntimeError("API irraggiungibile")

        class Risposta:
            input_tokens = self.valore

        return Risposta()


async def test_il_conteggio_esatto_viene_memoizzato():
    """Ogni `count_tokens` e' un giro di rete. Ripeterlo sullo stesso prompt
    costa latenza per un numero che non puo' essere cambiato."""
    client = ClientFinto(valore=1234)
    counter = TokenCounter(client)
    params = {"messages": [{"role": "user", "content": "ciao"}]}

    assert await counter.count("claude-opus-5", params) == 1234
    assert await counter.count("claude-opus-5", params) == 1234
    assert client.chiamate == 1


async def test_se_l_api_non_risponde_si_ripiega_sulla_stima():
    """Un preventivo mancato non deve bloccare la richiesta: il tetto di spesa
    decide su una stima invece che su un conteggio, e lo si sa."""
    counter = TokenCounter(ClientFinto(esplode=True))
    params = {"messages": [{"role": "user", "content": "domanda " * 50}]}
    assert await counter.count("claude-opus-5", params) == estimate_prompt_tokens(params)


async def test_la_memoizzazione_ha_un_tetto():
    """Un dizionario che cresce senza limite in un processo che gira per
    settimane e' una perdita di memoria."""
    client = ClientFinto(valore=7)
    counter = TokenCounter(client, max_entries=4)
    for indice in range(20):
        await counter.count(
            "claude-opus-5", {"messages": [{"role": "user", "content": f"m{indice}"}]}
        )
    assert len(counter._cache) <= 4


async def test_modelli_diversi_non_condividono_la_voce():
    """I tokenizzatori possono differire, e servire il numero di un modello per
    un altro sarebbe un errore invisibile."""
    client = ClientFinto(valore=42)
    counter = TokenCounter(client)
    params = {"messages": [{"role": "user", "content": "ciao"}]}
    await counter.count("claude-opus-5", params)
    await counter.count("claude-haiku-4-5", params)
    assert client.chiamate == 2
