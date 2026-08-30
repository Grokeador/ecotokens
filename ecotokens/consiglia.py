"""Che forma ha *il tuo* traffico, e cosa conviene accendere per quella forma.

Il progetto ha ventidue comandi che misurano e nessuno che consigli. La
differenza non e' di comodita': tutte le percentuali pubblicate sono medie su
un corpus di scenari, e la stessa configurazione che rende **+52,3%** su un
ciclo agentico rende **-0,1%** su molti utenti a turno singolo. Un numero medio fra
quei due non descrive nessuno.

Questo modulo non misura niente di nuovo: legge il traffico gia' registrato,
riconosce quale dei quattro regimi misurati gli somiglia, e mette accanto a
ogni consiglio **il numero misurato per quel regime**, non una media.

La regola che lo governa: dove il campione e' troppo piccolo per decidere, si
dice - non si consiglia. Un consiglio dato su tre richieste non e' un consiglio
prudente, e' un consiglio inventato con la faccia di uno misurato.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Sotto questa soglia non si classifica. Non e' una scelta di stile: la quota
# di sessioni a turno singolo e il tasso di continuazione sono rapporti, e su
# poche richieste un rapporto oscilla fra 0 e 1 senza dire niente.
CAMPIONE_MINIMO = 20

# Il merito misurato del gateway per ciascun regime, contro uno sviluppatore
# che marca il proprio system prompt. Sono i numeri del README, e stanno qui
# per essere citati accanto al consiglio invece che ricordati a memoria.
#
# Restano scritti a mano, ma non sono piu' inventabili: **li ricalcola
# `ecotokens merito`**, e questa costante e' una citazione di quel comando. Fino
# al 30 agosto 2026 non lo era, e si e' visto: erano fermi da mesi mentre il
# resto delle misure si muoveva, perche' nessun comando li produceva. Il primo
# ricalcolo ha spostato la chat da +22,6% a **+1,1%** - chi marca il proprio
# system prompt, in una conversazione con turni brevi, cattura gia' quasi tutto.
#
# Chi li aggiorna: eseguire `ecotokens merito` e copiare la colonna di destra.
# Un test confronta questi valori con quelli del README, cosi' le due copie non
# possono divergere in silenzio.
MERITO = {
    "agentico": "+52,3%",
    "ripetitivo": "+75,6%",
    "chat": "+1,1%",
    "turno_singolo": "-0,1%",
}

DESCRIZIONE = {
    "agentico": "ciclo agentico: molti turni, risultati di tool voluminosi",
    "ripetitivo": "domande che si ripetono",
    "chat": "conversazioni che crescono turno dopo turno",
    "turno_singolo": "molte richieste brevi, poco condivise fra loro",
}


@dataclass
class Consiglio:
    titolo: str
    verdetto: str
    perche: str
    azione: str = ""


@dataclass
class Rapporto:
    regime: str | None = None
    campione: int = 0
    segnali: dict[str, Any] = field(default_factory=dict)
    consigli: list[Consiglio] = field(default_factory=list)

    @property
    def sufficiente(self) -> bool:
        return self.regime is not None


def classifica(profilo: dict[str, Any]) -> str:
    """Il regime che somiglia di piu' al traffico osservato.

    L'ordine dei controlli e' la parte che conta, e non e' arbitrario: si va
    dal segnale piu' specifico al piu' generico. La potatura che scatta implica
    almeno 20.000 token di `tool_result`, che nessun altro regime produce; la
    quota servita da cache implica domande ripetute alla lettera. I turni per
    sessione separano gli ultimi due, e sono il segnale piu' debole perche' una
    sessione lunga puo' nascere da un client che rimanda tutta la cronologia
    senza che sia davvero una conversazione.
    """
    if profilo.get("quota_potatura", 0.0) >= 0.20:
        return "agentico"
    if profilo.get("quota_da_cache", 0.0) >= 0.20:
        return "ripetitivo"
    if profilo.get("turni_medi", 0.0) >= 3.0:
        return "chat"
    return "turno_singolo"


def _consigli_del_regime(regime: str, settings: Any) -> list[Consiglio]:
    merito = MERITO[regime]
    consigli: list[Consiglio] = []

    if regime == "agentico":
        consigli.append(
            Consiglio(
                titolo="Il pianificatore e' la leva, tienilo acceso",
                verdetto=f"merito misurato su questo regime: {merito}",
                perche=(
                    "In un ciclo agentico i risultati dei tool pesano molto piu' "
                    "del prompt di sistema: chi marca solo il proprio `system` "
                    "cattura il 3% e lascia il resto. E' il caso migliore del "
                    "gateway."
                ),
                azione=(
                    "" if settings.cache_planner.enabled
                    else "ACCENDILO: `[cache_planner] enabled = true`"
                ),
            )
        )
        consigli.append(
            Consiglio(
                titolo="Non declassare il modello",
                verdetto="il declassamento azzera il vantaggio qui sopra",
                perche=(
                    "Haiku 4.5 richiede 4096 token di prefisso per usare la "
                    "cache contro i 512 di Opus 5: su questo traffico "
                    "spegnerebbe in silenzio proprio cio' che rende di piu'."
                ),
                azione=(
                    "SPEGNILO: `profilo = \"prudente\"` oppure "
                    "`[router] model_downgrade = false`"
                    if settings.router.model_downgrade else ""
                ),
            )
        )
        consigli.append(
            Consiglio(
                titolo="La potatura del contesto qui rende",
                verdetto="+7,8% misurato sul carico agentico lento",
                perche=(
                    "E' l'unico regime in cui la potatura e' un'ottimizzazione "
                    "di costo e non solo una difesa contro l'overflow."
                ),
                azione=(
                    "" if settings.context.enabled
                    else "ACCENDILA: `[context] enabled = true`"
                ),
            )
        )

    elif regime == "ripetitivo":
        consigli.append(
            Consiglio(
                titolo="La cache esatta e' quasi tutto il valore",
                verdetto=f"merito misurato su questo regime: {merito}",
                perche=(
                    "Una richiesta identica servita dalla cache costa zero, ed "
                    "e' merito interamente del gateway: nessun client senza di "
                    "esso avrebbe evitato la chiamata."
                ),
                azione=(
                    "" if settings.exact_cache.enabled
                    else "ACCENDILA: `[exact_cache] enabled = true`"
                ),
            )
        )
        consigli.append(
            Consiglio(
                titolo="Controlla la durata e la capienza della cache",
                verdetto="una voce scaduta o sfrattata e' una chiamata pagata due volte",
                perche=(
                    "Su questo traffico il parametro che conta di piu' non e' "
                    "quale stadio e' acceso, ma per quanto tempo la risposta "
                    "resta disponibile."
                ),
                azione="verifica `[exact_cache] ttl_seconds` e `max_entries`",
            )
        )

    elif regime == "chat":
        consigli.append(
            Consiglio(
                titolo="Il pianificatore conviene, ma meno che altrove",
                verdetto=f"merito misurato su questo regime: {merito}",
                perche=(
                    "Su una conversazione sola che cresce, il caching "
                    "automatico di Anthropic fa gia' buona parte del lavoro. Il "
                    "gateway aggiunge mettendo in cache la **conversazione**, "
                    "non solo il prompt di sistema."
                ),
                azione=(
                    "" if settings.cache_planner.enabled
                    else "ACCENDILO: `[cache_planner] enabled = true`"
                ),
            )
        )

    else:  # turno_singolo
        consigli.append(
            Consiglio(
                titolo="Su questo traffico il gateway non fa risparmiare",
                verdetto=f"merito misurato su questo regime: {merito} — cioe' pari",
                perche=(
                    "Molte richieste brevi che non condividono granche' non "
                    "danno al pianificatore niente da riusare. Il gateway resta "
                    "utile per il registro della spesa, il tetto e la cache "
                    "esatta, ma non aspettarti una riduzione di costo."
                ),
                azione=(
                    "se lo tieni, tienilo per il tetto di spesa: "
                    "`[budget] enabled = true`"
                    if not settings.budget.enabled else ""
                ),
            )
        )

    return consigli


def _consigli_trasversali(profilo: dict[str, Any], settings: Any) -> list[Consiglio]:
    """Valgono in ogni regime, e nascono da segnali che il registro gia' ha."""
    consigli: list[Consiglio] = []

    tasso = profilo.get("tasso_continuazione")
    if tasso is not None and tasso < 0.278:
        consigli.append(
            Consiglio(
                titolo="Le tue conversazioni finiscono presto",
                verdetto=f"solo il {tasso:.0%} prosegue oltre il primo turno",
                perche=(
                    "Il pareggio per marcare la coda e' al 27,8%, ed e' il "
                    "rapporto fra i moltiplicatori dell'API (scrittura 1,25x, "
                    "lettura 0,1x), non una soglia scelta. Sotto quella, "
                    "marcare la coda costa piu' di quanto rende."
                ),
                azione=(
                    "il gateway se ne accorge da solo se "
                    "`[cache_planner] adatta_primo_turno = true`"
                ),
            )
        )

    if settings.profilo == "aggressivo":
        consigli.append(
            Consiglio(
                titolo="Sei sul profilo aggressivo: leggi il merito con cautela",
                verdetto="parte del risparmio non e' la stessa risposta a meno prezzo",
                perche=(
                    "Il declassamento del modello risponde con un modello "
                    "diverso da quello chiesto. `stats` lo separa sotto la voce "
                    "\"di cui da sostituzione del modello\": guarda quella riga "
                    "prima di confrontare il tuo numero con quelli del README, "
                    "che sono misurati senza declassamento."
                ),
            )
        )

    if profilo.get("quota_potatura", 0.0) >= 0.20 and not settings.context.enabled:
        consigli.append(
            Consiglio(
                titolo="Hai materiale da potare e la potatura e' spenta",
                verdetto="i risultati di tool superano i 20.000 token potabili",
                perche="E' la condizione in cui potare smette di essere solo una difesa.",
                azione="`[context] enabled = true`",
            )
        )

    return consigli


def analizza(profilo: dict[str, Any], settings: Any) -> Rapporto:
    """Il rapporto completo. Se il campione e' scarso, `regime` resta None."""
    campione = int(profilo.get("richieste") or 0)
    rapporto = Rapporto(campione=campione, segnali=dict(profilo))
    if campione < CAMPIONE_MINIMO:
        return rapporto

    rapporto.regime = classifica(profilo)
    rapporto.consigli = _consigli_del_regime(rapporto.regime, settings)
    rapporto.consigli += _consigli_trasversali(profilo, settings)
    return rapporto
