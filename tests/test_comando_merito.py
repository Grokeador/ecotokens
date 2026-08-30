"""Test di `ecotokens merito`, il comando.

Accanto a `test_merito.py`, che prova l'aritmetica delle due baseline: quello
difende la formula, questo difende il fatto che qualcuno la **esegua**.


Il comando esiste per una ragione che vale la pena tenere scritta: fino al 30
agosto 2026 i quattro numeri piu' citati del progetto - +52% agentico, +87,2%
ripetitivo, +22,6% chat, -0,2% turno singolo - **non erano ricalcolabili da
nessun comando**. Venivano da uno script buttato via e vivevano in due copie
scritte a mano, nel README e in `consiglia.MERITO`.

Il difetto non era che fossero sbagliati. Era che non potevano accorgersene: il
giorno in cui i moltiplicatori dell'effort sono stati misurati invece che
assunti, `ablate` e' cambiato del 25% e quei quattro sono rimasti fermi.
Rieseguendoli, il +22,6% sulla chat si e' rivelato **+1,1%**.

Quindi la proprieta' che questi test difendono non e' un valore: e' che la
tabella continui a essere *prodotta* invece che citata, e che le sue tre colonne
restino distinte - perche' confonderle e' il modo piu' facile di dire una cosa
vera che inganna.
"""

from __future__ import annotations

import pytest

from ecotokens.merito import Riga, calcola, carichi, scenario_turno_singolo


# --- l'aritmetica delle tre colonne ---------------------------------------


def test_la_colonna_che_decide_si_rapporta_al_concorrente_vero():
    """Il denominatore e' la baseline **ingenua**, non quella piena.

    La domanda e' «quanto risparmio in piu' rispetto a dove sarei senza
    gateway»: rapportarla a chi non usa affatto la cache diluirebbe il numero
    fino a farlo sembrare piccolo quando e' grande."""
    riga = Riga("prova", 10, piena_usd=100.0, ingenua_usd=50.0, nostro_usd=25.0)
    assert riga.di_ecotokens == pytest.approx(0.50)
    # E non 0,25, che sarebbe (50-25)/100.
    assert riga.di_ecotokens != pytest.approx(0.25)


def test_le_tre_colonne_si_ricompongono():
    """Quello che Anthropic regala piu' quello che aggiunge il gateway deve
    fare il totale: se non torna, una delle tre sta contando due volte."""
    riga = Riga("prova", 10, piena_usd=100.0, ingenua_usd=40.0, nostro_usd=10.0)
    assert riga.totale == pytest.approx(0.90)
    assert riga.di_anthropic == pytest.approx(0.60)
    # 60% lo regala Anthropic; del 40 che resta, il gateway ne toglie i tre
    # quarti. 0,60 + 0,40 x 0,75 = 0,90.
    assert riga.di_anthropic + (1 - riga.di_anthropic) * riga.di_ecotokens == pytest.approx(
        riga.totale
    )


def test_un_merito_negativo_resta_negativo():
    """La riga scomoda e' la piu' importante di tutte: su molti utenti a turno
    singolo il gateway non conviene, e una tabella che non lo mostra e' una
    brochure."""
    riga = Riga("prova", 10, piena_usd=100.0, ingenua_usd=40.0, nostro_usd=42.0)
    assert riga.di_ecotokens < 0


@pytest.mark.parametrize("campo", ["piena_usd", "ingenua_usd"])
def test_un_denominatore_a_zero_non_esplode(campo):
    valori = {"piena_usd": 10.0, "ingenua_usd": 10.0, "nostro_usd": 1.0}
    valori[campo] = 0.0
    riga = Riga("prova", 0, **valori)
    assert riga.totale == 0.0 or riga.di_ecotokens == 0.0


# --- i carichi -------------------------------------------------------------


def test_ci_sono_tutti_e_cinque_i_regimi():
    assert len(carichi()) == 5
    etichette = " ".join(nome for nome, _ in carichi())
    assert "agentico" in etichette
    assert "turno singolo" in etichette


def test_il_turno_singolo_non_condivide_conversazione():
    """E' il caso in cui chi marca il proprio system prompt ha gia' tutto: il
    prefisso condiviso **e'** il system prompt, e non cresce niente."""
    scenario = scenario_turno_singolo(utenti=5)
    assert len(scenario.requests) == 5
    domande = {r["messages"][1]["content"] for r in scenario.requests}
    assert len(domande) == 5, "le domande devono essere diverse fra loro"
    sistemi = {r["messages"][0]["content"] for r in scenario.requests}
    assert len(sistemi) == 1, "il system prompt deve essere identico"


def test_i_carichi_non_toccano_il_corpus_condiviso():
    """Aggiungere uno scenario a `all_scenarios` invaliderebbe i confronti
    storici del banco: e' una trappola gia' calpestata, e sta nel README."""
    from ecotokens.workloads import all_scenarios

    nomi = {s.name for s in all_scenarios()}
    assert "turno-singolo" not in nomi


# --- la misura vera, in piccolo -------------------------------------------


async def test_la_tabella_si_produce_e_le_righe_tornano():
    """Non verifica un valore - quello cambia quando cambia il gateway, ed e'
    il punto. Verifica che il comando la **produca**: e' l'unica differenza fra
    un numero misurato e una citazione."""
    rapporto = await calcola()
    assert len(rapporto.righe) == 5
    assert rapporto.simulato is True
    for riga in rapporto.righe:
        assert riga.richieste > 0, riga.etichetta
        assert riga.piena_usd > 0, riga.etichetta
        # Nessuno paga piu' del fantoccio: se succede, il gateway sta
        # peggiorando le cose e va saputo, non nascosto in una media.
        assert riga.nostro_usd <= riga.piena_usd, riga.etichetta


async def test_il_turno_singolo_e_il_caso_in_cui_non_conviene():
    """La riga che tiene onesta tutta la tabella. Se un giorno diventasse
    lusinghiera, o e' migliorato il gateway o si e' rotto il metro - e la
    seconda, qui, e' l'ipotesi piu' probabile."""
    rapporto = await calcola()
    riga = next(r for r in rapporto.righe if "turno singolo" in r.etichetta)
    assert riga.di_ecotokens < 0.05, (
        "su molti utenti a turno singolo chi marca il proprio system prompt "
        f"cattura gia' quasi tutto: osservato {riga.di_ecotokens:+.1%}"
    )


# --- le due copie non possono divergere -----------------------------------


def test_i_valori_citati_altrove_combaciano_col_README():
    """La lezione, resa un test.

    `consiglia.MERITO` e la tabella del README sono due copie a mano dello
    stesso risultato, e restano tali: il comando le produce, non le scrive. Ma
    due copie senza un test che le confronti sono esattamente il meccanismo per
    cui questi numeri sono rimasti fermi per mesi mentre il resto si muoveva.
    """
    import re
    from pathlib import Path

    from ecotokens.consiglia import MERITO

    readme = Path(__file__).resolve().parents[1] / "README.md"
    testo = readme.read_text(encoding="utf-8")

    atteso = {
        "agentico": "ciclo agentico, 20 turni con tool",
        "ripetitivo": "domande che si ripetono",
        "chat": "una conversazione che cresce, 8 turni",
        "turno_singolo": "molti utenti, stesso system, turno singolo",
    }
    for chiave, etichetta in atteso.items():
        riga = next(
            (r for r in testo.splitlines() if etichetta in r and r.startswith("|")),
            None,
        )
        assert riga is not None, f"riga mancante nel README: {etichetta}"
        percentuali = re.findall(r"[+−-]\d+,\d+%", riga)
        assert percentuali, riga
        valore = MERITO[chiave].replace("-", "−")
        assert valore in [p.replace("-", "−") for p in percentuali], (
            f"{chiave}: `consiglia.MERITO` dice {MERITO[chiave]}, il README no. "
            "Rieseguire `ecotokens merito` e allineare entrambi."
        )


async def test_dal_vivo_non_si_spende_se_la_cache_non_rilegge(monkeypatch):
    """La guardia che vale i due centesimi che costa.

    La rilettura su un account vero va e viene nell'arco di minuti. In una
    finestra morta questi carichi spenderebbero qualche dollaro per concludere
    che il gateway non serve, e la conclusione descriverebbe il momento invece
    del gateway. Il primo giro dal vivo e' stato fermato cosi', a mano: questo
    test e' la stessa prudenza, ma che non dipende da chi lancia il comando.
    """
    import ecotokens.verifica as modulo_verifica

    async def cache_ferma(client, modello, **kwargs):
        return 0

    monkeypatch.setattr(modulo_verifica, "testimone_di_cache", cache_ferma)
    monkeypatch.setattr(
        "anthropic.AsyncAnthropic", lambda **kwargs: object(), raising=True
    )

    rapporto = await calcola(live=True)
    assert rapporto.eseguita is False
    assert rapporto.testimone == 0
    assert rapporto.righe == [], "non deve aver eseguito nessun carico"


async def test_il_testimone_non_si_arrende_al_primo_zero(monkeypatch):
    """Uno solo sarebbe piu' severo della misura che protegge.

    La rilettura di un prefisso nuovo riesce circa tre volte su cinque: un
    testimone a colpo singolo rifiuterebbe di misurare due volte su cinque
    senza che ci sia niente che non va. E' lo stesso difetto - un controllo
    tarato male che blocca il lavoro buono - che questo progetto rimprovera
    agli strumenti troppo permissivi, preso dal lato opposto.
    """
    import ecotokens.verifica as modulo_verifica
    from ecotokens.merito import TENTATIVI_TESTIMONE

    # Quattro zeri e poi una rilettura: il testimone deve arrivarci.
    esiti = iter([0, 0, 0, 0, 2800])
    chiamate = []

    async def a_singhiozzo(client, modello, **kwargs):
        valore = next(esiti)
        chiamate.append(valore)
        return valore

    async def niente_api(scenario, settings, variante, *, live, **kwargs):
        from ecotokens.bench import Measurement

        return Measurement(
            scenario=scenario.name,
            variant=variante,
            requests=1,
            cost_usd=1.0,
            baseline_piena_usd=3.0,
            baseline_ingenua_usd=2.0,
        )

    monkeypatch.setattr(modulo_verifica, "testimone_di_cache", a_singhiozzo)
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda **kwargs: object())
    monkeypatch.setattr("ecotokens.merito._run_scenario", niente_api)

    rapporto = await calcola(live=True, solo="turno singolo")
    assert len(chiamate) == TENTATIVI_TESTIMONE == 5
    assert rapporto.testimone == 2800
    assert rapporto.eseguita is True
