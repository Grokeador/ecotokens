"""Cosa fa il gateway quando qualcosa si rompe.

Tutto il resto della suite prova il gateway che funziona. Ma un gateway e' un
pezzo **in mezzo**: la domanda che decide se vale la pena installarlo non e'
quanto risparmia quando va tutto bene, e' se puo' far fallire una richiesta
che senza di lui sarebbe passata. Un ottimizzatore che rompe non e' un
ottimizzatore lento: e' un guasto in piu' che prima non c'era.

Da qui il criterio di questo file, che vale come regola per tutti gli stadi:
**un guasto interno degrada, non abbatte.** Se la memoria non riesce a
recuperare un fatto, la richiesta parte senza quel fatto. Se il pianificatore
di cache sbaglia, la richiesta parte senza breakpoint e costa di piu'. Nessuno
di questi e' un motivo per restituire 500 a chi voleva una risposta.

L'unica eccezione e' il budget, che **deve** abbattere: e' l'unico stadio il
cui scopo e' impedire una spesa.
"""

from __future__ import annotations

import pytest

from ecotokens.pipeline.base import BaseStage, PipelineAbort

from .conftest import chat_payload


class StadioRotto(BaseStage):
    """Uno stadio che solleva. Non e' un caso di scuola: e' cio' che succede a
    ogni bug di uno stadio, e gli stadi sono la parte del progetto che cambia
    piu' spesso."""

    def __init__(self, name: str = "rotto", *, dove: str = "before") -> None:
        self.name = name
        self.dove = dove
        self.chiamato = 0

    async def before(self, ctx):
        if self.dove == "before":
            self.chiamato += 1
            raise RuntimeError("bug nello stadio")

    async def after(self, ctx, message):
        if self.dove == "after":
            self.chiamato += 1
            raise RuntimeError("bug nello stadio")


class StadioVandalo(BaseStage):
    """Rompe **dopo** aver gia' riscritto la richiesta a meta'.

    E' il caso che rende insufficiente il semplice `try/except`: proseguire
    con i parametri come li ha lasciati significa spedire un prompt che
    nessuno ha composto - meta' riscritto da uno stadio che non ha finito.
    """

    name = "vandalo"
    riscrive = True

    async def before(self, ctx):
        ctx.params["messages"] = [{"role": "user", "content": "prompt mutilato"}]
        ctx.params["system"] = "sostituito a meta'"
        raise RuntimeError("rotto dopo aver toccato i parametri")


# --- un bug interno non deve diventare un errore per il client -------------


@pytest.mark.parametrize("dove", ["before", "after"])
def test_uno_stadio_che_solleva_non_fa_fallire_la_richiesta(client, dove):
    rotto = StadioRotto(dove=dove)
    client.gateway.pipeline.stages.insert(1, rotto)

    risposta = client.post("/v1/chat/completions", json=chat_payload())

    assert risposta.status_code == 200, "un bug interno e' diventato un errore del client"
    assert rotto.chiamato == 1
    assert risposta.json()["choices"][0]["message"]["content"]


def test_ogni_stadio_puo_rompersi_da_solo_senza_portarsi_dietro_gli_altri(client):
    """Uno per uno, tutti quelli accesi. Un test parametrizzato sui nomi noti
    diventerebbe falso il giorno in cui si aggiunge uno stadio; questo scopre
    da solo quali ci sono."""
    nomi = [s.name for s in client.gateway.pipeline.stages if getattr(s, "enabled", True)]
    assert len(nomi) >= 5, "la pipeline di test e' troppo vuota perche' questo provi qualcosa"

    for indice, nome in enumerate(nomi):
        originale = client.gateway.pipeline.stages[indice]
        client.gateway.pipeline.stages[indice] = StadioRotto(nome)
        try:
            risposta = client.post("/v1/chat/completions", json=chat_payload())
            assert risposta.status_code == 200, f"lo stadio {nome} rotto abbatte la richiesta"
        finally:
            client.gateway.pipeline.stages[indice] = originale


def test_uno_stadio_che_rompe_a_meta_non_lascia_i_suoi_danni(client):
    """Il prompt spedito deve essere quello di prima dello stadio, non quello
    che lo stadio stava scrivendo quando si e' rotto."""
    client.gateway.pipeline.stages.insert(1, StadioVandalo())

    risposta = client.post("/v1/chat/completions", json=chat_payload())
    assert risposta.status_code == 200

    inviato = client.stub.last
    testo = str(inviato.get("messages")) + str(inviato.get("system"))
    assert "mutilato" not in testo
    assert "sostituito" not in testo
    assert "Ciao, come stai?" in testo


def test_il_budget_resta_l_unico_stadio_che_puo_abbattere(client):
    """Il fail-open non deve trasformarsi in "niente ferma piu' niente": lo
    stadio che esiste per impedire una spesa deve continuare a impedirla."""

    class Tetto(BaseStage):
        name = "tetto"

        async def before(self, ctx):
            raise PipelineAbort("tetto giornaliero superato", status_code=429)

    client.gateway.pipeline.stages.insert(0, Tetto())
    risposta = client.post("/v1/chat/completions", json=chat_payload())

    assert risposta.status_code == 429
    assert "tetto giornaliero" in risposta.json()["error"]["message"]
    assert client.stub.requests == [], "abortita, e nessun token speso"


def test_il_guasto_di_uno_stadio_e_visibile_non_silenzioso(client):
    """Degradare in silenzio e' l'altro modo di sbagliare: uno stadio spento
    da un bug continuerebbe a comparire come acceso, e il calo di risparmio
    verrebbe attribuito a chissa' cosa."""
    client.gateway.pipeline.stages.insert(1, StadioRotto("memoria_finta"))
    client.post("/v1/chat/completions", json=chat_payload())

    guasti = client.gateway.pipeline.guasti
    assert "memoria_finta" in guasti
    assert guasti["memoria_finta"]["conteggio"] == 1
    assert "RuntimeError" in guasti["memoria_finta"]["ultimo"]


def test_uno_stadio_che_rompe_sempre_viene_spento(client):
    """Riprovare all'infinito uno stadio che si rompe a ogni richiesta paga il
    costo del salvataggio dei parametri a ogni giro per nessun beneficio."""
    rotto = StadioRotto("insistente")
    client.gateway.pipeline.stages.insert(1, rotto)

    for _ in range(6):
        assert client.post("/v1/chat/completions", json=chat_payload()).status_code == 200

    assert rotto.enabled is False
    assert rotto.chiamato < 6, "ha continuato a essere chiamato dopo essersi spento"
    assert client.gateway.pipeline.guasti["insistente"]["spento"] is True


# --- guasti a monte --------------------------------------------------------


def test_un_529_transitorio_viene_riprovato(client):
    """529 e' "sovraccarico": la richiesta era valida e il secondo tentativo
    passa. Non riprovare qui significa restituire un errore che non c'era."""
    client.stub.guasti = [529]

    risposta = client.post("/v1/chat/completions", json=chat_payload())

    assert risposta.status_code == 200
    assert len(client.stub.requests) == 2, "non ha riprovato"


def test_un_400_non_viene_riprovato(client):
    """Riprovare una richiesta che sara' sempre rifiutata moltiplica la
    latenza dell'errore senza cambiarne l'esito."""
    client.stub.guasti = [400, 400, 400]

    risposta = client.post("/v1/chat/completions", json=chat_payload())

    assert risposta.status_code == 400
    assert len(client.stub.requests) == 1


def test_un_guasto_a_monte_non_scrive_una_riga_di_consumo(client):
    """Una chiamata rifiutata non consuma token: contarla falserebbe il
    denominatore di ogni misura successiva."""
    client.stub.guasti = [400]
    client.post("/v1/chat/completions", json=chat_payload())

    assert client.get("/admin/stats").json()["requests"] == 0


# --- la rottura a meta' stream --------------------------------------------


def _leggi_stream(risposta) -> list[str]:
    return [r for r in risposta.text.splitlines() if r.startswith("data: ")]


def test_uno_stream_troncato_arriva_al_client_come_stream_valido(client):
    """Una volta iniziata la risposta HTTP lo stato non si puo' piu' cambiare:
    l'errore va consegnato come chunk, e lo stream va chiuso con [DONE] o il
    client resta appeso fino al proprio timeout."""
    client.stub.interrompi_stream_dopo = 4

    risposta = client.post("/v1/chat/completions", json=chat_payload(stream=True))

    assert risposta.status_code == 200
    righe = _leggi_stream(risposta)
    assert righe[-1] == "data: [DONE]"
    assert any('"error"' in r for r in righe)


def test_uno_stream_troncato_viene_comunque_messo_in_conto(client):
    """**Il prompt e' gia' stato pagato.**

    Quando lo stream si rompe a meta', Anthropic ha gia' letto tutto il
    prompt - input, letture di cache, scritture - e ha gia' generato i token
    consegnati fin li'. Uscire dal generatore senza passare dalla contabilita'
    rende quella spesa invisibile: `stats` la sottostima, e il tetto di spesa
    non la conta, quindi si puo' superare un budget a furia di stream che
    cadono senza che nessun contatore se ne accorga.
    """
    client.stub.interrompi_stream_dopo = 4
    client.post("/v1/chat/completions", json=chat_payload(stream=True))

    totali = client.get("/admin/stats").json()
    assert totali["requests"] == 1, "lo stream caduto non e' finito nel registro"
    assert totali["input_tokens"] + totali["cache_read_tokens"] > 0
    assert totali["cost_usd"] > 0


def test_uno_stream_troncato_non_finisce_in_cache(client):
    """Servire una risposta tagliata a meta' a ogni richiesta successiva
    trasformerebbe un guasto momentaneo in un guasto permanente."""
    client.gateway.settings.exact_cache.enabled = True
    client.stub.interrompi_stream_dopo = 4
    client.post("/v1/chat/completions", json=chat_payload(stream=True))

    client.stub.interrompi_stream_dopo = None
    seconda = client.post("/v1/chat/completions", json=chat_payload(stream=True))
    righe = _leggi_stream(seconda)
    assert not any('"error"' in r for r in righe)


# --- lo strato che sta sotto ----------------------------------------------


def test_il_database_irraggiungibile_non_abbatte_la_richiesta(client, monkeypatch):
    """Il registro serve a misurare, non a rispondere. Se si rompe si perde
    una misura; perdere anche la risposta sarebbe scambiare il termometro per
    il paziente."""

    async def esplode(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(client.gateway.store, "record_usage", esplode)

    risposta = client.post("/v1/chat/completions", json=chat_payload())
    assert risposta.status_code == 200
    assert risposta.json()["choices"][0]["message"]["content"]


# --- la dichiarazione che rende sicura l'ottimizzazione --------------------


class Spia(BaseStage):
    """Avvolge uno stadio e controlla che rispetti la propria dichiarazione.

    Il salvataggio dei parametri viene fatto **una volta sola**, prima del
    primo stadio che dichiara di riscrivere: e' cio' che tiene il costo della
    protezione a una copia per richiesta invece di otto. Ma quel risparmio si
    regge su una dichiarazione, e una dichiarazione sbagliata non fallirebbe
    rumorosamente: farebbe soltanto sparire la protezione per gli stadi che
    girano prima. Qui la dichiarazione smette di essere una promessa.
    """

    def __init__(self, stadio: BaseStage) -> None:
        self._stadio = stadio
        self.name = stadio.name
        self.riscrive = getattr(stadio, "riscrive", False)
        self.enabled = getattr(stadio, "enabled", True)
        self.bugia: str | None = None

    async def before(self, ctx):
        from ecotokens.pipeline.base import copia_parametri

        prima = None if self.riscrive else copia_parametri(ctx.params)
        await self._stadio.before(ctx)
        if prima is not None and prima != ctx.params:
            self.bugia = f"{self.name} dichiara di non riscrivere, ma ha cambiato i parametri"

    async def after(self, ctx, message):
        await self._stadio.after(ctx, message)


def test_chi_dichiara_di_non_riscrivere_non_riscrive(client):
    spie = [Spia(s) for s in client.gateway.pipeline.stages]
    client.gateway.pipeline.stages[:] = spie

    # Due richieste: la seconda trova una sessione gia' nota, e gli stadi che
    # leggono lo storico prendono una strada diversa dalla prima.
    for _ in range(2):
        assert client.post("/v1/chat/completions", json=chat_payload()).status_code == 200

    bugie = [s.bugia for s in spie if s.bugia]
    assert bugie == [], bugie
    assert any(s.riscrive for s in spie), "nessuno stadio riscrive: il test non prova niente"


def test_gli_stadi_che_riscrivono_sono_dichiarati(client):
    """L'errore opposto - dichiarare di riscrivere senza riscrivere - costa
    solo una copia inutile, e non va confuso con quello pericoloso."""
    riscrivono = {
        s.name for s in client.gateway.pipeline.stages if getattr(s, "riscrive", False)
    }
    assert {"prompt", "context", "router", "cache_planner"} <= riscrivono


def test_chi_si_rompe_perde_solo_il_proprio_lavoro(client):
    """Il salvataggio e' per stadio, non per richiesta.

    La prima versione ne faceva uno solo all'inizio della catena, e un guasto
    nell'ultimo stadio annullava anche la compattazione e la memoria: si
    pagava una richiesta piu' cara per un bug che stava altrove. Costa una
    copia in piu' per stadio che riscrive, e la misura dice che quella copia
    sta sotto il rumore dello strumento.
    """

    class Marcatore(BaseStage):
        name = "marcatore"
        riscrive = True

        async def before(self, ctx):
            ctx.params["messages"][-1]["content"] = "lavoro utile"

    client.gateway.pipeline.stages.insert(1, Marcatore())
    client.gateway.pipeline.stages.insert(2, StadioVandalo())

    assert client.post("/v1/chat/completions", json=chat_payload()).status_code == 200

    inviato = str(client.stub.last["messages"])
    assert "lavoro utile" in inviato, "il guasto ha annullato anche il lavoro di chi lo precede"
    assert "mutilato" not in inviato


# --- la chiusura -----------------------------------------------------------


async def test_la_chiusura_prova_tutti_i_passi_anche_se_uno_fallisce(settings, stub):
    """I tre passi sono indipendenti. In fila senza protezione, una potatura
    fallita lasciava aperti database e client HTTP - cioe' proprio le due cose
    che la chiusura esiste per chiudere."""
    import anthropic
    import httpx2

    from ecotokens.server import Gateway

    stub_app, _ = stub
    gateway = Gateway(settings)
    gateway.client = anthropic.AsyncAnthropic(
        api_key="prova",
        base_url="http://stub",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.ASGITransport(app=stub_app)
        ),
    )
    await gateway.startup()

    async def esplode(*args, **kwargs):
        raise RuntimeError("disco pieno")

    gateway.store.prune_cache = esplode
    await gateway.shutdown()  # non deve sollevare

    assert gateway.client.is_closed()
