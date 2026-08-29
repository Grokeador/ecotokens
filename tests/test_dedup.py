"""Deduplicare i tool_result: quanto rende, e cosa toglie dal prompt.

In un ciclo agentico lo stesso file viene riletto in turni diversi e rispedito
intero ogni volta. Sostituire le copie successive con un riferimento alla prima
vale molto - misurato: +61,0% con dodici riletture per file, +43,8% con tre,
**+0,0% senza ripetizione**, che e' il controllo che rende credibili gli altri
due.

Ma cambia il contenuto del prompt, e la domanda che decide non e' quanto costa
meno: e' se cio' che serviva e' ancora li'. `ecotokens ritenzione` non sa
rispondere - i suoi scenari sono conversazioni senza un solo `tool_result`, e
una variante nuova misurerebbe l'inazione - quindi la risposta si costruisce
qui: **la prima copia resta intatta**, e il fatto continua a stare nel prompt.

Resta non verificato che il modello sappia *usare* un riferimento all'indietro
invece del testo. E' un'assunzione dichiarata, non una misura, ed e' per questo
che lo stadio esce spento.
"""

from __future__ import annotations

from ecotokens.config import Settings
from ecotokens.pipeline.context import ContextStage, _MINIMO_PER_DEDUP

MARCATORE = "IL-SEGNO-CHE-SERVE"
CORPO = f"riga di codice con {MARCATORE}\n" * 40  # ben oltre la soglia


class _Ctx:
    """Il minimo che serve a `_dedup_tool_results`: parametri e note."""

    def __init__(self, messages):
        self.params = {"messages": messages}
        self.notes: list[str] = []

    def note(self, testo: str) -> None:
        self.notes.append(testo)


def _stadio(**modifiche):
    config = Settings()
    config.context.dedup_tool_results = True
    for chiave, valore in modifiche.items():
        setattr(config.context, chiave, valore)
    return ContextStage(config)


def _messaggi(testi: list[str]) -> list[dict]:
    """Una conversazione dove ogni turno porta un risultato di tool."""
    messaggi: list[dict] = []
    for indice, testo in enumerate(testi):
        messaggi.append({"role": "user", "content": f"passo {indice}"})
        messaggi.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": f"toolu_{indice}",
                     "name": "leggi", "input": {}}
                ],
            }
        )
        messaggi.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"toolu_{indice}",
                     "content": [{"type": "text", "text": testo}]}
                ],
            }
        )
    messaggi.append({"role": "user", "content": "domanda finale"})
    return messaggi


def _testi_dei_risultati(messaggi) -> list[str]:
    esiti = []
    for messaggio in messaggi:
        contenuto = messaggio.get("content")
        if not isinstance(contenuto, list):
            continue
        for blocco in contenuto:
            if isinstance(blocco, dict) and blocco.get("type") == "tool_result":
                esiti.append(
                    "".join(
                        p.get("text", "")
                        for p in blocco["content"]
                        if isinstance(p, dict)
                    )
                )
    return esiti


# --- cosa resta nel prompt -------------------------------------------------


def test_la_prima_copia_resta_intatta():
    """La domanda che `ritenzione` porrebbe se sapesse porla: il fatto c'e'
    ancora? Si', nella prima occorrenza - ed e' l'unica ragione per cui questo
    stadio non e' una potatura travestita."""
    ctx = _Ctx(_messaggi([CORPO, CORPO, CORPO]))
    _stadio()._dedup_tool_results(ctx)

    testi = _testi_dei_risultati(ctx.params["messages"])
    assert MARCATORE in testi[0], "la prima copia e' stata toccata"
    assert sum(1 for t in testi if MARCATORE in t) == 1
    assert all("identico a quello di" in t for t in testi[1:])


def test_si_tiene_il_primo_e_non_l_ultimo():
    """Il primo sta piu' a monte, cioe' dentro il prefisso gia' in cache:
    sostituirlo invaliderebbe tutto cio' che segue, e la deduplicazione
    costerebbe piu' di quanto rende."""
    ctx = _Ctx(_messaggi([CORPO, CORPO]))
    _stadio()._dedup_tool_results(ctx)

    testi = _testi_dei_risultati(ctx.params["messages"])
    assert testi[0] == CORPO
    assert testi[1] != CORPO


def test_i_risultati_diversi_non_vengono_toccati():
    ctx = _Ctx(_messaggi([CORPO, CORPO.replace("codice", "altro"), CORPO]))
    _stadio()._dedup_tool_results(ctx)

    testi = _testi_dei_risultati(ctx.params["messages"])
    assert testi[0] == CORPO
    assert "altro" in testi[1]
    assert "identico a quello di" in testi[2]


def test_la_coppia_tool_use_tool_result_resta_appaiata():
    """Togliere il blocco invece di svuotarlo farebbe fallire la richiesta con
    un 400: e' il modo piu' probabile in cui questo stadio puo' rompere tutto."""
    ctx = _Ctx(_messaggi([CORPO, CORPO]))
    _stadio()._dedup_tool_results(ctx)

    chiamate, risultati = set(), set()
    for messaggio in ctx.params["messages"]:
        for blocco in messaggio.get("content") or []:
            if not isinstance(blocco, dict):
                continue
            if blocco.get("type") == "tool_use":
                chiamate.add(blocco["id"])
            elif blocco.get("type") == "tool_result":
                risultati.add(blocco["tool_use_id"])
    assert risultati == chiamate


def test_l_ultimo_messaggio_non_viene_toccato():
    """E' la domanda a cui si sta rispondendo: li' il guadagno e' minimo e il
    rischio massimo."""
    messaggi = _messaggi([CORPO, CORPO])
    ultimo_prima = dict(messaggi[-1])
    ctx = _Ctx(messaggi)
    _stadio()._dedup_tool_results(ctx)
    assert ctx.params["messages"][-1] == ultimo_prima


# --- quando non deve fare niente ------------------------------------------


def test_i_risultati_corti_si_lasciano_stare():
    """Il testo del riferimento occupa ~90 caratteri: sostituire qualcosa di
    piu' corto **aggiungerebbe** token."""
    corto = "ok"
    assert len(corto) < _MINIMO_PER_DEDUP
    ctx = _Ctx(_messaggi([corto, corto, corto]))
    _stadio()._dedup_tool_results(ctx)
    assert _testi_dei_risultati(ctx.params["messages"]) == [corto, corto, corto]


def test_senza_ripetizioni_non_cambia_niente():
    """Il controllo che rende credibile il +61%: dove non c'e' niente da
    deduplicare, non deve succedere niente."""
    testi = [CORPO.replace("codice", f"variante-{i}") for i in range(4)]
    ctx = _Ctx(_messaggi(testi))
    _stadio()._dedup_tool_results(ctx)
    assert _testi_dei_risultati(ctx.params["messages"]) == testi
    assert ctx.notes == []


def test_la_nota_dice_quanto_e_stato_tolto():
    ctx = _Ctx(_messaggi([CORPO, CORPO, CORPO]))
    _stadio()._dedup_tool_results(ctx)
    assert any("deduplicazione: 2 risultati" in nota for nota in ctx.notes), ctx.notes


def test_esce_spento():
    """Cambia il contenuto del prompt e una delle sue premesse non e'
    verificata: la regola del progetto e' lasciarlo spento e dirlo."""
    assert Settings().context.dedup_tool_results is False
