"""La tavola delle formulazioni deve descrivere cio' che il gateway emette davvero.

`wording.CATALOG` dichiara, per ogni testo che il gateway aggiunge di suo, la
forma attuale e quella precedente, e `ecotokens overhead` ne stampa il
risparmio. Ma la tavola e' un elenco: non obbliga nessuno a usarla.

Due punti la ignoravano. La memoria importava `MEMORY_OPEN` e poi scriveva a
mano `<memoria-rilevante>`; la compattazione faceva lo stesso con
`<riassunto-conversazione-precedente>`. Ventiquattro token per richiesta che
`overhead` contava come gia' risparmiati e che nessuna richiesta risparmiava.

E' il difetto peggiore della famiglia: un'ottimizzazione **contata e mai
applicata** non ha nessuno che la vada a cercare, perche' il cruscotto dice che
c'e'. Da qui questi test, che guardano il codice invece della tavola.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ecotokens.wording as wording
from ecotokens.wording import CATALOG

SORGENTI = sorted(
    percorso
    for percorso in Path("ecotokens").rglob("*.py")
    # La tavola stessa contiene per forza entrambe le forme, ed e' il suo
    # mestiere. Il registro delle correzioni le cita raccontando la storia.
    if percorso.name not in {"wording.py", "tuning_log.py"}
)


@pytest.mark.parametrize("voce", CATALOG, ids=lambda v: v.key)
def test_nessuna_forma_lunga_e_rimasta_scritta_a_mano(voce):
    """Se la forma precedente compare ancora nel codice, non e' stata sostituita."""
    if not voce.legacy:
        return
    colpevoli = [
        percorso.as_posix()
        for percorso in SORGENTI
        if voce.legacy in percorso.read_text(encoding="utf-8")
    ]
    assert not colpevoli, (
        f"{voce.key}: la forma lunga {voce.legacy!r} e' ancora emessa da "
        f"{', '.join(colpevoli)}, mentre overhead conta come risparmiati "
        f"{voce.saved} token per occorrenza"
    )


@pytest.mark.parametrize("voce", CATALOG, ids=lambda v: v.key)
def test_ogni_voce_della_tavola_e_usata_da_qualcuno(voce):
    """Una voce che nessuno emette gonfia il conto dell'overhead con testo assente.

    E' il difetto simmetrico al precedente: li' si contava un risparmio non
    ottenuto, qui si conterebbe un costo non pagato. In entrambi i casi il
    numero e' plausibile e sbagliato.

    Si cerca il **simbolo**, non il testo: che il codice importi `MEMORY_OPEN`
    invece di scrivere `<note>` e' esattamente cio' che si vuole, ed e' il
    motivo per cui il test precedente puo' esistere.
    """
    # Alcune voci della tavola sono un **esempio reso** di una costante che e'
    # un modello: `TOOL_CALL` vale "[>{name}]" e la tavola ne mostra
    # "[>read_file]". Il confronto tiene conto di entrambi i casi.
    def corrisponde(valore: str) -> bool:
        if valore == voce.text:
            return True
        pezzi = [p for p in re.split(r"\{[^}]*\}", valore) if p]
        return bool(pezzi) and all(pezzo in voce.text for pezzo in pezzi)

    nomi = [nome for nome, valore in vars(wording).items()
            if isinstance(valore, str) and nome.isupper() and corrisponde(valore)]
    assert nomi, f"{voce.key}: nessuna costante di wording produce {voce.text!r}"

    usata = any(
        any(nome in percorso.read_text(encoding="utf-8") for nome in nomi)
        for percorso in SORGENTI
    )
    assert usata, (
        f"{voce.key}: la tavola conta {voce.text!r} ma nessun file usa "
        f"{' o '.join(nomi)}"
    )


def test_la_memoria_usa_i_delimitatori_corti():
    """Il caso concreto da cui sono nati questi test, fissato per nome."""
    sorgente = Path("ecotokens/pipeline/memory.py").read_text(encoding="utf-8")
    assert "wrap(MEMORY_OPEN, MEMORY_CLOSE" in sorgente
    assert "memoria-rilevante" not in sorgente


def test_il_riassunto_usa_i_delimitatori_corti():
    sorgente = Path("ecotokens/pipeline/context.py").read_text(encoding="utf-8")
    assert "wrap(SUMMARY_OPEN, SUMMARY_CLOSE" in sorgente
    assert "riassunto-conversazione-precedente" not in sorgente
