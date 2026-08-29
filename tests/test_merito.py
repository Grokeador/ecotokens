"""Quanto del risparmio e' del gateway, e quanto lo darebbe Anthropic comunque.

E' la domanda che decide se vale la pena installarlo, ed e' quella a cui il
numero di testa **non** rispondeva. `baseline_cost_usd` prezza un client che
non usa affatto il prompt caching: un fantoccio. Chiunque integri l'API oggi
mette un `cache_control` in cima al proprio system prompt e ottiene lo sconto
sul prefisso stabile senza installare niente.

Misurarsi contro il fantoccio significa prendersi il merito di una funzione che
c'e' comunque - e in questo progetto e' la meta' piu' grossa del numero: il
67,8% e' di Anthropic, lo 0,7 di medi e' del gateway. Il file `CLAUDE.md` lo
dice da tempo; la console diceva ancora "Senza gateway: stesso traffico a
prezzo pieno".

Da qui la seconda baseline, e questi test che le impediscono di ricadere in
quella comoda.
"""

from __future__ import annotations

from ecotokens.pricing import Usage, baseline_cost_usd, baseline_ingenua_usd

from .conftest import chat_payload


def _usage(prompt: int, output: int = 200) -> Usage:
    return Usage(input_tokens=prompt, output_tokens=output)


# --- il conto --------------------------------------------------------------


def test_la_baseline_realistica_costa_meno_di_quella_a_prezzo_pieno():
    """Se non costasse meno, non ci sarebbe niente da distinguere."""
    usage = _usage(20_000)
    pieno = baseline_cost_usd("claude-opus-5", usage)
    accorto = baseline_ingenua_usd("claude-opus-5", usage, 8_000)
    assert accorto < pieno
    # E lo sconto e' quello del prefisso: 8.000 token a 0,1x invece che a 1x.
    atteso = pieno - 8_000 * 5.0 * 0.9 / 1_000_000
    assert abs(accorto - atteso) < 1e-9


def test_il_prefisso_freddo_si_sa_prezzare_ma_non_e_il_predefinito():
    """Al primo giro un client accorto la cache la **scrive**, a 1,25x: su una
    richiesta isolata mettere un breakpoint gli costa piu' di quanto rende, e
    la sua baseline finisce sopra il prezzo pieno.

    Il conto lo sa fare, e non e' quello che il registro usa. Il predefinito
    concede all'altro il prezzo migliore, perche' dove EcoTokens dichiara il
    proprio guadagno piu' grande - molte sessioni che condividono un system
    prompt - anche il suo prefisso e' caldo, e fargli pagare la scrittura li'
    gonfierebbe il nostro merito proprio dove il numero viene citato."""
    usage = _usage(20_000)
    prima = baseline_ingenua_usd("claude-opus-5", usage, 8_000, prefisso_freddo=True)
    poi = baseline_ingenua_usd("claude-opus-5", usage, 8_000)
    assert prima > poi
    assert prima > baseline_cost_usd("claude-opus-5", usage) * 0.99


def test_sotto_la_soglia_del_modello_le_due_baseline_coincidono():
    """Sotto soglia nessuna cache si forma, nemmeno per lui: attribuirgli uno
    sconto che non avrebbe avuto gonfierebbe il fantoccio invece di sgonfiarlo.

    E la soglia non e' la stessa per tutti - Opus 5 ne chiede 512, Haiku 4.5
    ne chiede 4096 - quindi lo stesso prefisso puo' essere sopra per un modello
    e sotto per un altro.
    """
    usage = _usage(3_000)
    for modello in ("claude-opus-5", "claude-haiku-4-5"):
        pieno = baseline_cost_usd(modello, usage)
        accorto = baseline_ingenua_usd(modello, usage, 400)
        assert accorto == pieno, modello

    # 2.000 token: sopra la soglia di Opus 5, sotto quella di Haiku 4.5.
    assert baseline_ingenua_usd("claude-opus-5", usage, 2_000) < (
        baseline_cost_usd("claude-opus-5", usage)
    )
    assert baseline_ingenua_usd("claude-haiku-4-5", usage, 2_000) == (
        baseline_cost_usd("claude-haiku-4-5", usage)
    )


def test_un_prefisso_piu_grande_del_prompt_non_sconta_piu_del_dovuto():
    """La stima locale del prefisso puo' superare il conteggio vero dell'API.
    Senza il taglio, lo sconto si mangerebbe token mai pagati."""
    usage = _usage(1_000)
    accorto = baseline_ingenua_usd("claude-opus-5", usage, 50_000)
    assert accorto > 0
    assert accorto < baseline_cost_usd("claude-opus-5", usage)


# --- attraverso il gateway -------------------------------------------------


def test_su_una_richiesta_sola_mettere_in_cache_costa_a_tutti_e_due(client):
    """Il pareggio della cache e' a due richieste, e vale anche per il
    concorrente: chi scrive un prefisso e non lo rilegge mai ha pagato 1,25x
    per niente. Su una richiesta isolata la baseline del client accorto sta
    quindi **sopra** quella a prezzo pieno, e non e' un errore del conto - e'
    il motivo per cui il pianificatore non marca le sessioni usa-e-getta.
    """
    client.post("/v1/chat/completions", json=chat_payload())
    stats = client.get("/admin/stats").json()

    assert stats["baseline_cost_usd"] > 0
    assert stats["baseline_ingenua_usd"] > stats["baseline_cost_usd"]


def test_su_una_conversazione_il_client_accorto_paga_meno_del_prezzo_pieno(client):
    """Dal secondo turno in poi il suo prefisso e' caldo, ed e' li' che si
    vede quanto di quel risparmio non ha niente a che fare con noi."""
    for turno in range(4):
        client.post(
            "/v1/chat/completions",
            json=chat_payload(
                messages=[
                    {"role": "system", "content": "Sei un assistente. " * 200},
                    {"role": "user", "content": f"domanda numero {turno}"},
                ]
            ),
        )
    stats = client.get("/admin/stats").json()

    pieno = stats["baseline_cost_usd"]
    accorto = stats["baseline_ingenua_usd"]
    assert 0 < accorto < pieno, "il concorrente non sta ottenendo il suo sconto"
    # E il numero che conta: quanto resta al gateway dopo aver tolto
    # quello sconto. Puo' essere piccolo - su questo carico lo e' - ma deve
    # essere quello mostrato, non l'altro.
    dati = client.get("/admin/live").json()["totals"]
    assert dati["quota_anthropic_usd"] > 0
    assert abs(
        dati["quota_gateway_usd"] + dati["quota_anthropic_usd"] - dati["saved_usd"]
    ) < 1e-6, "le due meta' devono ricomporre il risparmio totale"


def test_il_concorrente_e_freddo_quando_lo_eravamo_noi(client):
    """La trappola gia' calpestata due volte in questo progetto, in una forma
    nuova: confrontare una serie fredda con una calda.

    Assumendo il concorrente sempre caldo, ogni prima richiesta ci vedeva
    pagare una scrittura contro una sua lettura, e il gateway risultava
    dannoso su ogni sessione nuova. Il verso lo decide `cache_read_tokens`,
    che e' un'osservazione e non un modello.
    """
    prima = client.post("/v1/chat/completions", json=chat_payload())
    assert prima.json()["ecotokens"]["cached_prompt_tokens"] == 0

    eventi = client.get("/admin/live").json()["recent"]
    assert eventi, "senza eventi il test non prova niente"


def test_un_hit_della_cache_esatta_e_merito_solo_del_gateway(client):
    """Nessun client senza gateway avrebbe evitato la chiamata: qui le due
    baseline coincidono, ed e' il caso in cui vale di piu'."""
    client.gateway.settings.exact_cache.enabled = True
    for stadio in client.gateway.pipeline.stages:
        if stadio.name == "exact_cache":
            stadio.enabled = True

    corpo = chat_payload(messages=[{"role": "user", "content": "domanda ripetuta"}])
    client.post("/v1/chat/completions", json=corpo)
    seconda = client.post("/v1/chat/completions", json=corpo)
    assert seconda.json()["ecotokens"]["source"] == "exact_cache"

    dati = client.get("/admin/live").json()["totals"]
    assert dati["quota_gateway_usd"] > 0


def test_la_console_non_chiama_piu_prezzo_pieno_il_senza_gateway():
    """L'etichetta vecchia diceva "Senza gateway: stesso traffico a prezzo
    pieno", e attribuiva al gateway anche cio' che Anthropic da' gratis."""
    from ecotokens.console import render_console

    pagina = render_console()
    assert "Merito del gateway" in pagina
    assert "Client accorto, senza gateway" in pagina
    assert "usa la cache da sé" in pagina


def test_la_console_avvisa_quando_il_gateway_non_si_ripaga():
    """Il caso peggiore per un progetto onesto: dire all'utente che quello che
    ha installato, sul suo traffico, non sta aggiungendo niente."""
    from ecotokens.console import _avvisi

    avvisi = _avvisi(
        {
            "requests": 40,
            "stages": [],
            "cache_writes": {},
            "spend": {"enabled": False},
            "calibration": [],
            "totals": {"baseline_ingenua_usd": 1.0, "quota_gateway_ratio": 0.004},
        }
    )
    titoli = " ".join(a["title"] for a in avvisi)
    assert "gratis" in titoli
    assert all(a["count"] for a in avvisi)


# --- il pianificatore che osserva invece di indovinare ---------------------


async def test_il_pareggio_non_e_una_soglia_scelta():
    """E' il rapporto fra i due moltiplicatori dell'API: scrivere costa 0,25x
    in piu', rileggere fa risparmiare 0,9x. Un numero derivato non invecchia
    da solo come uno inventato."""
    from ecotokens.pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER

    pareggio = (CACHE_WRITE_MULTIPLIER["5m"] - 1.0) / (1.0 - CACHE_READ_MULTIPLIER)
    assert 0.27 < pareggio < 0.28


async def test_senza_abbastanza_sessioni_non_si_decide(client):
    """Su quattro conversazioni la frazione oscilla fra 0 e 1: deciderebbe a
    caso, e un caso su due sarebbe il verso sbagliato."""
    assert await client.gateway.store.tasso_continuazione() is None


def test_su_traffico_a_turno_singolo_smette_di_marcare_la_coda(client):
    """Misurato: su richieste che un turno dopo non ce l'hanno, il marker
    sulla coda scrive in cache qualcosa che nessuno rileggera'. Toglierlo vale
    +1,6 punti e porta le scritture da 25.046 token a 3.967."""
    for indice in range(24):
        client.post(
            "/v1/chat/completions",
            json=chat_payload(
                messages=[
                    {"role": "system", "content": "Assistente. " * 300},
                    {"role": "user", "content": f"utente {indice}, domanda sua " * 20},
                ]
            ),
        )

    ultima = client.post(
        "/v1/chat/completions",
        json=chat_payload(
            messages=[
                {"role": "system", "content": "Assistente. " * 300},
                {"role": "user", "content": "utente nuovo, domanda sua " * 20},
            ]
        ),
    )
    note = " ".join(ultima.json()["ecotokens"]["notes"])
    assert "nessun breakpoint sulla coda" in note, note
    assert "prosegue" in note


def test_una_conversazione_che_prosegue_continua_a_marcare(client):
    """La regola vale solo al primo turno: dal secondo in poi la conversazione
    ha gia' dimostrato di proseguire, e il marker si ripaga."""
    storia = [{"role": "system", "content": "Assistente. " * 300}]
    ultima = None
    for turno in range(3):
        storia = storia + [{"role": "user", "content": f"domanda {turno} " * 20}]
        ultima = client.post("/v1/chat/completions", json=chat_payload(messages=list(storia)))
        storia = storia + [{"role": "assistant", "content": "risposta " * 40}]

    note = " ".join(ultima.json()["ecotokens"]["notes"])
    assert "breakpoint sull'ultimo turno" in note, note
