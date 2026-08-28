"""Test del modello di caching del simulatore.

Non e' un test accessorio: ogni numero prodotto da ``ecotokens bench`` dipende
da queste regole. Due difetti qui hanno gia' prodotto misure che davano il
gateway per dannoso quando invece risparmiava il 70%, quindi il modello va
vincolato esplicitamente.
"""

from __future__ import annotations

from ecotokens.simulator import LOOKBACK_BLOCKS, StubState, _usage_for

MARKER = {"type": "ephemeral"}


def richiesta(blocchi: list[dict], marcato: int | None = None) -> dict:
    """Costruisce una richiesta con un marker su un blocco a scelta."""
    messaggi = []
    for indice, blocco in enumerate(blocchi):
        contenuto = dict(blocco)
        if marcato is not None and indice == marcato:
            contenuto = {**contenuto, "cache_control": dict(MARKER)}
        messaggi.append({"role": "user", "content": [contenuto]})
    return {"model": "claude-opus-5", "messages": messaggi}


def blocco(testo: str) -> dict:
    """Un blocco abbastanza grosso da poter essere messo in cache davvero.

    La misura non e' arbitraria: Opus 5 non memorizza prefissi sotto i 512
    token, e sotto quella soglia l'API non segnala niente - la richiesta
    riesce, la cache semplicemente non si crea. I blocchi di questi test erano
    da una quindicina di token, quindi provavano il modello di caching su
    richieste che la vera API non avrebbe mai messo in cache.
    """
    return {"type": "text", "text": testo * 2500}


def test_senza_marker_nessuna_cache():
    state = StubState()
    payload = richiesta([blocco("a"), blocco("b")])
    usage = _usage_for(state, payload)
    assert usage["cache_read_input_tokens"] == 0
    assert usage["cache_creation_input_tokens"] == 0


def test_prima_richiesta_scrive_seconda_legge():
    state = StubState()
    payload = richiesta([blocco("a"), blocco("b")], marcato=1)

    prima = _usage_for(state, payload)
    assert prima["cache_creation_input_tokens"] > 0
    assert prima["cache_read_input_tokens"] == 0

    seconda = _usage_for(state, payload)
    assert seconda["cache_read_input_tokens"] > 0
    assert seconda["cache_creation_input_tokens"] == 0


def test_il_marker_non_entra_nell_impronta():
    """La regola che due volte ha falsato le misure.

    ``cache_control`` e' una direttiva, non contenuto. Se entrasse
    nell'impronta, il blocco marcato a un turno non combacerebbe piu' con se
    stesso al turno successivo, quando il marker si e' spostato in avanti: la
    cache verrebbe riscritta ogni volta e mai riletta, e il gateway
    sembrerebbe far aumentare i costi.
    """
    state = StubState()
    conversazione = [blocco("a"), blocco("b")]

    _usage_for(state, richiesta(conversazione, marcato=1))

    # Turno successivo: la conversazione cresce e il marker avanza.
    conversazione = conversazione + [blocco("c"), blocco("d")]
    seconda = _usage_for(state, richiesta(conversazione, marcato=3))

    assert seconda["cache_read_input_tokens"] > 0, (
        "il prefisso del turno precedente deve restare leggibile"
    )


def test_lookback_limitato_a_venti_blocchi():
    """Oltre la finestra di lookback il breakpoint non trova nulla, in silenzio."""
    state = StubState()
    conversazione = [blocco("a"), blocco("b")]
    _usage_for(state, richiesta(conversazione, marcato=1))

    # Un turno che aggiunge molti piu' di venti blocchi senza marker intermedi.
    conversazione = conversazione + [blocco(f"x{i}") for i in range(LOOKBACK_BLOCKS + 5)]
    seconda = _usage_for(state, richiesta(conversazione, marcato=len(conversazione) - 1))

    assert seconda["cache_read_input_tokens"] == 0
    assert seconda["cache_creation_input_tokens"] > 0


def test_marker_intermedio_recupera_il_lookback():
    """Con un marker intermedio la catena non si spezza."""
    state = StubState()
    conversazione = [blocco("a"), blocco("b")]
    _usage_for(state, richiesta(conversazione, marcato=1))

    coda = [blocco(f"x{i}") for i in range(LOOKBACK_BLOCKS + 5)]
    conversazione = conversazione + coda

    # Due marker: uno a meta' della coda, uno in fondo.
    messaggi = []
    intermedio = 1 + (LOOKBACK_BLOCKS // 2)
    for indice, contenuto in enumerate(conversazione):
        blocco_corrente = dict(contenuto)
        if indice in (intermedio, len(conversazione) - 1):
            blocco_corrente = {**blocco_corrente, "cache_control": dict(MARKER)}
        messaggi.append({"role": "user", "content": [blocco_corrente]})

    seconda = _usage_for(state, {"model": "claude-opus-5", "messages": messaggi})
    assert seconda["cache_read_input_tokens"] > 0


def test_un_cambiamento_nel_prefisso_invalida_tutto():
    state = StubState()
    _usage_for(state, richiesta([blocco("a"), blocco("b")], marcato=1))

    # Stesso marker, ma il primo blocco e' cambiato: il prefisso non e' piu'
    # lo stesso e nulla di cio' che segue puo' essere riletto.
    diversa = _usage_for(state, richiesta([blocco("z"), blocco("b")], marcato=1))
    assert diversa["cache_read_input_tokens"] == 0


def test_i_conti_tornano():
    """I tre contatori di input devono sommare al totale del prompt."""
    state = StubState()
    payload = richiesta([blocco("a"), blocco("b")], marcato=1)
    _usage_for(state, payload)
    usage = _usage_for(state, payload)

    totale = (
        usage["input_tokens"]
        + usage["cache_creation_input_tokens"]
        + usage["cache_read_input_tokens"]
    )
    assert totale > 0
    assert usage["input_tokens"] >= 0


# --- la soglia minima, e il caching automatico -----------------------------


def test_sotto_la_soglia_del_modello_la_cache_non_si_crea():
    """E non viene segnalato niente: e' il punto.

    Un marker su un prefisso troppo corto e' silenziosamente inefficace. La
    richiesta riesce, `cache_creation_input_tokens` torna zero, e chi legge
    crede di avere la cache attiva. Il simulatore deve riprodurre il silenzio,
    non solo l'effetto.
    """
    state = StubState()
    corto = {"type": "text", "text": "x" * 40}
    payload = {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [{**corto, "cache_control": dict(MARKER)}]}],
    }
    usage = _usage_for(state, payload)
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


def test_la_soglia_dipende_dal_modello():
    """Non e' monotona col prezzo: Opus 5 vuole 512 token, Haiku 4.5 quattromila.

    Ne segue che passare a un modello piu' economico puo' spegnere la cache in
    silenzio - ed e' esattamente cio' che fa il profilo aggressivo.
    """
    dimensione = [blocco("a")]
    dimensione[0] = {**dimensione[0], "cache_control": dict(MARKER)}
    messaggi = [{"role": "user", "content": dimensione}]

    opus = _usage_for(StubState(), {"model": "claude-opus-5", "messages": messaggi})
    haiku = _usage_for(StubState(), {"model": "claude-haiku-4-5", "messages": messaggi})

    assert opus["cache_creation_input_tokens"] > 0
    assert haiku["cache_creation_input_tokens"] == 0, (
        "lo stesso prefisso supera i 512 token di Opus 5 ma non i 4096 di Haiku 4.5"
    )


def test_il_caching_automatico_mette_un_breakpoint_sull_ultimo_blocco():
    """Un solo `cache_control` in cima, nessun marker sui blocchi.

    E' la modalita' che Anthropic ha reso disponibile, e cambia cosa significa
    "senza gateway": non piu' "nessuna cache", ma "un breakpoint gratis".
    """
    state = StubState()
    payload = {
        "model": "claude-opus-5",
        "cache_control": dict(MARKER),
        "messages": [{"role": "user", "content": [blocco("a")]}],
    }
    primo = _usage_for(state, payload)
    assert primo["cache_creation_input_tokens"] > 0

    # Il breakpoint avanza da solo: al turno dopo il prefisso viene riletto.
    payload["messages"] = payload["messages"] + [
        {"role": "assistant", "content": [blocco("b")]},
        {"role": "user", "content": [blocco("c")]},
    ]
    secondo = _usage_for(state, payload)
    assert secondo["cache_read_input_tokens"] > 0


def test_senza_il_campo_in_cima_il_caching_automatico_non_si_attiva():
    state = StubState()
    payload = {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": [blocco("a")]}],
    }
    usage = _usage_for(state, payload)
    assert usage["cache_creation_input_tokens"] == 0
