"""Registro delle correzioni decise misurando.

Ogni voce e' una cosa che si credeva vera e che la misura ha smentito. Serve a
due scopi: tenere il conto di come il gateway e' migliorato, e ricordare quali
convinzioni erano sbagliate, perche' e' facile riproporle.

La distinzione fra le due aree e' importante e va tenuta separata anche nella
lettura dei numeri:

* ``misura`` - un difetto del banco di prova. Cambia cio' che *credevamo*, non
  cio' che il gateway *fa*. Una correzione qui non migliora il prodotto: rende
  visibile com'era gia'.
* ``gateway`` - un difetto del prodotto. Qui il comportamento cambia davvero.

Le voci nuove si aggiungono in fondo.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TuningEntry:
    area: str
    title: str
    finding: str
    effect: str


TUNING_LOG: list[TuningEntry] = [
    TuningEntry(
        area="misura",
        title="Il marker di cache finiva dentro l'impronta del prefisso",
        finding=(
            "Il simulatore includeva cache_control nel calcolo dell'impronta. Un blocco "
            "marcato a un turno non combaciava piu' con se stesso al turno dopo, quando il "
            "marker si era spostato in avanti, quindi la cache veniva riscritta ogni volta e "
            "mai riletta. Nell'API reale cache_control e' una direttiva, non contenuto."
        ),
        effect=(
            "La misura dava il gateway per dannoso, con costi in aumento del 6,6% e punte del "
            "-21% sui carichi agentici. Corretta l'impronta, lo stesso carico mostra il 72% di "
            "risparmio. Il gateway non e' cambiato: era sbagliato il metro."
        ),
    ),
    TuningEntry(
        area="misura",
        title="L'effort non aveva alcun effetto sui token generati",
        finding=(
            "Il simulatore restituiva una lunghezza di risposta fissa a prescindere "
            "dall'effort richiesto. Lo stadio che abbassa l'effort risultava quindi inutile "
            "per costruzione, non perche' lo fosse."
        ),
        effect=(
            "Introdotto un modello dichiarato dell'effetto dell'effort sui token generati. "
            "Da quel momento il contributo di quello stadio e' diventato misurabile."
        ),
    ),
    TuningEntry(
        area="misura",
        title="La potatura del contesto non veniva mai applicata",
        finding=(
            "Il simulatore ignorava context_management: contava i token dei risultati che il "
            "server avrebbe gia' scartato. Ogni confronto sulla potatura misurava soltanto il "
            "peso in byte del parametro aggiunto alla richiesta."
        ),
        effect=(
            "Implementata la potatura anche nel simulatore. E' cosi' che si e' potuto misurare "
            "che potare e mettere in cache sono in conflitto."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="L'effort adattivo giudicava la difficolta' dal prompt intero",
        finding=(
            "Per decidere se una richiesta fosse semplice si contavano i token di tutto il "
            "prompt, con una soglia di 400. Qualunque prompt di sistema reale la supera, "
            "quindi l'effort non veniva abbassato mai. L'ablazione gli attribuiva un risparmio "
            "esattamente pari a zero."
        ),
        effect=(
            "Ora si misura la domanda, non il contesto: un prompt di sistema da 5000 token non "
            "rende difficile un 'che ore sono'. Lo stadio e' passato da 0% a circa il 2,5% del "
            "risparmio complessivo."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="La potatura del contesto era documentata come un'ottimizzazione di costo",
        finding=(
            "Misurando l'interazione fra potatura e prompt caching e' emerso che si "
            "ostacolano: potare sposta il confine di taglio a ogni turno, quindi cambia il "
            "prefisso. Sul carico di costruzione di questo progetto la quota di prompt servita "
            "da cache crolla dall'89% al 21% e il costo sale del 37%; su un ciclo agentico con "
            "risultati molto grossi invece conviene, di circa il 10%."
        ),
        effect=(
            "La soglia resta alta e la potatura e' stata ridocumentata per quello che e': una "
            "difesa contro l'esaurimento della finestra di contesto, non un modo per "
            "risparmiare. Chi la abbassa ora sa cosa sta scambiando."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il primo turno non scriveva in cache",
        finding=(
            "La regola nasceva da un ragionamento plausibile e sbagliato: il pareggio della "
            "cache e' a due richieste, quindi la scrittura del primo turno sembrava una "
            "perdita. Il ragionamento guardava ai turni di una conversazione e dimenticava "
            "che il pezzo piu' grosso del prefisso - prompt di sistema e definizioni dei "
            "tool - e' condiviso anche fra conversazioni diverse."
        ),
        effect=(
            "Misurato con `ecotokens optimize`: marcare sempre costa il 6% in meno sul mix "
            "standard, e su venti richieste isolate che condividono il prompt di sistema la "
            "differenza arriva al 155%. Il valore predefinito e' stato invertito."
        ),
    ),
]
