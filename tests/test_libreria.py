"""EcoTokens come libreria: una riga dentro il programma, invece di un programma.

La promessa e' forte, e per questo va difesa da test invece che dal README:
**il codice di chi la usa non cambia**. Stessa firma, stesso tipo di risposta,
stesso comportamento - cambia solo il conto.

La promessa ha un modo di rompersi che i test devono cogliere prima degli
utenti, ed e' il piu' insidioso di tutti: una risposta servita dalla cache
tornava come dizionario invece che come `Message`. Il codice di chi la usa
avrebbe funzionato **tranne** quando la cache colpisce, cioe' avrebbe fallito
solo ogni tanto e per una ragione che non si vede nel proprio codice.

Il secondo test in ordine di importanza e' quello sulla chiave: la libreria non
la legge, non la scrive e non la trasporta. La tiene il client, che resta di
chi lo ha costruito. E' l'unica ragione per cui questa forma esiste accanto al
gateway.
"""

from __future__ import annotations

import anthropic
import httpx2
import pytest

from ecotokens import Economico
from ecotokens.simulator import create_stub


@pytest.fixture
def client_simulato():
    stub_app, stato = create_stub()
    yield anthropic.AsyncAnthropic(
        api_key="prova",
        base_url="http://simulatore",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    ), stato


def _richiesta(testo: str = "ciao") -> dict:
    return {
        "model": "claude-opus-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": testo}],
    }


# --- la promessa: il codice di chi la usa non cambia ----------------------


async def test_restituisce_un_message_come_l_sdk(client_simulato):
    grezzo, _ = client_simulato
    async with Economico(grezzo) as client:
        risposta = await client.messages.create(**_richiesta())

    assert isinstance(risposta, anthropic.types.Message)
    assert risposta.content[0].text
    assert risposta.usage.input_tokens > 0


async def test_anche_una_risposta_dalla_cache_e_un_Message(client_simulato):
    """Il difetto che questo test esiste per impedire: il codice di chi la usa
    funzionerebbe **tranne** quando la cache colpisce, cioe' fallirebbe solo
    ogni tanto e per una ragione invisibile dal proprio codice."""
    grezzo, stato = client_simulato
    async with Economico(grezzo) as client:
        client.impostazioni.exact_cache.enabled = True
        client._gateway.riconfigura(client.impostazioni)

        prima = await client.messages.create(**_richiesta("domanda identica"))
        chiamate = len(stato.requests)
        dopo = await client.messages.create(**_richiesta("domanda identica"))

    assert len(stato.requests) == chiamate, "la seconda non doveva arrivare all'API"
    assert isinstance(dopo, anthropic.types.Message)
    assert dopo.content[0].text == prima.content[0].text


async def test_conta_i_token_senza_ottimizzare(client_simulato):
    """Contare non consuma: passa dritto, e deve continuare a funzionare."""
    grezzo, _ = client_simulato
    async with Economico(grezzo) as client:
        esito = await client.messages.count_tokens(
            model="claude-opus-5", messages=[{"role": "user", "content": "ciao"}]
        )
    assert esito.input_tokens > 0


async def test_lo_streaming_dice_di_no_invece_di_fingere(client_simulato):
    """Uno streaming che salta gli stadi e dice di risparmiare sarebbe peggio
    di uno che non c'e': il primo mente, il secondo si vede."""
    grezzo, _ = client_simulato
    async with Economico(grezzo) as client:
        with pytest.raises(NotImplementedError) as errore:
            client.messages.stream(**_richiesta())
    assert "originale" in str(errore.value), "deve dire come farlo comunque"


# --- la chiave non passa di qui -------------------------------------------


async def test_la_libreria_non_tocca_la_chiave(client_simulato, monkeypatch):
    """La ragione per cui questa forma esiste accanto al gateway.

    Il client e' di chi lo costruisce, e la libreria gli si mette intorno senza
    guardarci dentro. Se un giorno qualcuno leggesse `ANTHROPIC_API_KEY` da
    qui, questo test lo direbbe."""
    grezzo, _ = client_simulato
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    async with Economico(grezzo) as client:
        assert client.originale is grezzo
        risposta = await client.messages.create(**_richiesta())
    assert risposta.content[0].text


# --- niente sorprese sul disco --------------------------------------------


async def test_non_crea_file_se_non_glielo_chiedi(client_simulato, tmp_path, monkeypatch):
    """Una libreria che si crea un database al primo uso e' una libreria che
    sorprende, e la sorpresa si scopre in produzione."""
    grezzo, _ = client_simulato
    monkeypatch.chdir(tmp_path)

    async with Economico(grezzo) as client:
        await client.messages.create(**_richiesta())

    assert list(tmp_path.iterdir()) == [], f"ha lasciato: {list(tmp_path.iterdir())}"


async def test_con_memoria_il_file_lo_crea(client_simulato, tmp_path):
    grezzo, _ = client_simulato
    percorso = tmp_path / "ricordi.db"
    async with Economico(grezzo, memoria=percorso) as client:
        await client.messages.create(**_richiesta())
    assert percorso.exists()


# --- che serva a qualcosa --------------------------------------------------


async def test_il_pianificatore_marca_davvero_il_prefisso(client_simulato):
    """Lo stadio che vale il +76% misurato dal vivo. In libreria non dipende da
    niente di condiviso, quindi deve rendere identico: se il prefisso non viene
    marcato, questa forma non serve a niente e va saputo qui."""
    grezzo, stato = client_simulato
    lungo = "istruzione operativa dettagliata " * 400
    async with Economico(grezzo) as client:
        await client.messages.create(
            model="claude-opus-5",
            max_tokens=64,
            system=lungo,
            messages=[{"role": "user", "content": "ciao"}],
        )

    inviata = stato.requests[-1]
    blocchi = inviata.get("system") or []
    marcati = [b for b in blocchi if isinstance(b, dict) and b.get("cache_control")]
    assert marcati, f"nessun breakpoint piazzato: {inviata.keys()}"


async def test_le_statistiche_riportano_la_spesa(client_simulato):
    grezzo, _ = client_simulato
    async with Economico(grezzo) as client:
        await client.messages.create(**_richiesta())
        stats = await client.statistiche()
    assert stats["requests"] >= 1
    assert float(stats["cost_usd"]) > 0


async def test_l_ultimo_dettaglio_e_leggibile(client_simulato):
    """Il conto non si vede nella risposta - che deve restare identica a quella
    dell'SDK - quindi deve vedersi da qualche altra parte."""
    grezzo, _ = client_simulato
    async with Economico(grezzo) as client:
        await client.messages.create(**_richiesta())
        assert client.ultimo, "nessun dettaglio dopo una richiesta"
        assert "model" in client.ultimo or "notes" in client.ultimo


# --- chiusura --------------------------------------------------------------


async def test_chiudere_due_volte_non_esplode(client_simulato):
    grezzo, _ = client_simulato
    client = Economico(grezzo)
    await client.messages.create(**_richiesta())
    await client.aclose()
    await client.aclose()
