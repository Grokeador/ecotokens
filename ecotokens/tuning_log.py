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
    TuningEntry(
        area="misura",
        title="La spesa delle chiamate interne del gateway non era contata",
        finding=(
            "Il riassunto di compattazione e' una chiamata a un modello, quindi costa, ma non "
            "compare in `response.usage` della richiesta dell'utente: nessuna misura la vedeva. "
            "La compattazione risultava gratuita per costruzione, e uno stadio che sembra "
            "gratuito viene acceso anche quando non conviene."
        ),
        effect=(
            "Introdotto `aux_cost_usd` sul contesto: il riassuntore vi addebita la propria "
            "spesa, il ledger la somma al costo della richiesta e il banco di prova la riporta "
            "separata. Sul carico di conversazione lunga vale fra l'1% e il 15% del totale a "
            "seconda della strategia di taglio: abbastanza da ribaltare un confronto."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il simulatore ignorava max_tokens",
        finding=(
            "La lunghezza della risposta dipendeva solo dall'effort, mai dal tetto richiesto. "
            "Qualunque limite imposto dal gateway - per esempio quello sul riassunto - era "
            "invisibile alla misura."
        ),
        effect=(
            "Il simulatore ora smette di generare al tetto, come fa il server. Misurato subito "
            "dopo: sui carichi di prova il riassunto sta ampiamente sotto i 600 token e il "
            "tetto non morde mai. Resta come paracadute, ma non e' un risparmio e non viene "
            "piu' presentato come tale."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il punto di taglio della compattazione inseguiva la conversazione",
        finding=(
            "Il taglio era calcolato come `lunghezza - messaggi_da_tenere`, quindi avanzava di "
            "due messaggi a ogni turno. Il riassunto veniva percio' ricalcolato - e riusciva "
            "diverso - a ogni richiesta, e con esso cambiava l'inizio del prompt. Il codice "
            "memorizzava il riassunto per riusarlo, ma la chiave conteneva il punto di taglio: "
            "una chiave che si muoveva insieme alla conversazione non poteva mai combaciare. "
            "Il docstring prometteva 'calcolato una volta sola e riusato alla lettera' e il "
            "test lo verificava a cronologia ferma, che e' l'unico caso in cui funzionava."
        ),
        effect=(
            "Su una consulenza di quaranta turni la compattazione costava il 40,5% PIU' del non "
            "comprimere affatto: 34 riassunti in 40 turni e quota di prompt servita da cache "
            "giu' dal 95% al 54%. Ora il taglio avanza a scatti di 12 messaggi, cosi' lo stesso "
            "riassunto vale per piu' turni: 9 riassunti, cache all'82%, e la compattazione passa "
            "da -40,5% a +9,9%. Aggiunto un test che fa crescere la conversazione, perche' a "
            "cronologia ferma il difetto non si vedeva."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Accorciare il prompt rende un quarto di quello che sembra",
        finding=(
            "Riscrivere il prompt toglie token veri, ma quasi tutti quei token sarebbero "
            "stati serviti dalla cache a un decimo del prezzo. Misurato sul carico di prompt "
            "verbosi: togliere mille token dal prompt rende circa 0,0014 USD, contro i 0,0050 "
            "che quegli stessi mille token costerebbero a prezzo pieno su Opus 5. La resa e' "
            "circa un quarto, e non dipende molto da dove si taglia: system e messaggi utente "
            "danno 0,00125 e 0,00160 per mille token, perche' in una conversazione a piu' "
            "turni finiscono entrambi nel prefisso servito da cache."
        ),
        effect=(
            "Lo stadio esiste e funziona - sul carico verboso vale l'11% - ma sul corpus "
            "completo pesa lo 0,2%, e viene documentato per quello che e': utile su prompt "
            "scritti male, marginale altrove. Chi cerca il risparmio grosso lo trova nel "
            "prompt caching, non nella prosa."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il risparmio in token delle sostituzioni lessicali non e' misurabile qui",
        finding=(
            "Il simulatore conta i token dalla lunghezza del testo. Sotto quella metrica "
            "qualunque accorciamento risulta un guadagno per costruzione, quindi una tabella "
            "di sinonimi piu' corti si autoconfermerebbe: si misurerebbe l'assunzione, non il "
            "fatto. Il tokenizer di Claude non e' pubblico e l'unica autorita' e' "
            "`messages.count_tokens`."
        ),
        effect=(
            "Le sostituzioni lessicali sono spente di default e, con `only_verified` attivo, "
            "non vengono applicate finche' `ecotokens substitutions --live` non le ha "
            "confrontate con il conteggio vero: l'esito per modello finisce in tabella, e i "
            "candidati bocciati restano inerti. Nella dashboard quel livello e' marcato 'non "
            "validato' invece di essere presentato insieme agli altri."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="La chiave della cache esatta si calcolava sui byte grezzi",
        finding=(
            "Due richieste che differiscono per uno spazio doppio, una riga vuota o una "
            "virgoletta tipografica sono la stessa domanda, e il modello risponderebbe allo "
            "stesso modo. La chiave, calcolata sul testo cosi' com'era arrivato, le mandava su "
            "voci di cache diverse. Il riconoscimento di sessione normalizzava gia' la "
            "spaziatura; la cache no, e nessuno se n'era accorto perche' tutti gli scenari di "
            "prova ripetevano le domande identiche."
        ),
        effect=(
            "Su un carico di domande ripetute con spaziatura variabile - un template "
            "incoerente, un copia e incolla - si passa da 0 hit su 12 a 8 su 12, e il costo "
            "scende del 56%. Sul carico con domande gia' identiche non cambia nulla, che era "
            "la verifica che serviva. E' l'ottimizzazione con la resa piu' alta del gateway: "
            "ogni altra leva sconta il prezzo di un token, un hit di cache lo azzera."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il testo aggiunto dal gateway non era contato da nessuno",
        finding=(
            "Delimitatori attorno al riassunto, blocco della memoria, istruzione per il JSON, "
            "regole date al riassuntore: token che l'utente paga senza averli scritti. Sparsi "
            "per il codice erano invisibili, e ognuno era scritto con il tono del file in cui "
            "capitava. `<riassunto-conversazione-precedente>` costava 22 token per delimitare "
            "cio' che `<storico>` delimita con 6."
        ),
        effect=(
            "Raccolti in `wording.py` con la formulazione precedente accanto, cosi' il "
            "guadagno e' verificabile invece che dichiarato: 254 -> 174 token per occorrenza, "
            "il 31% in meno. Onestamente: sono token per occorrenza, non per richiesta, e "
            "sulla fattura incidono poco. E' stato fatto perche' e' gratis e senza rischio - "
            "quel testo e' nostro - non perche' sposti l'ago."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Abbassare i messaggi tenuti integrali non e' un'ottimizzazione",
        finding=(
            "Il costo scende in modo monotono man mano che si riduce `keep_recent_messages`: "
            "su cinquanta turni, tenerne 4 costa $1,86 e tenerne 24 costa $3,39. Sembra un "
            "parametro da ottimizzare e non lo e': comprimere di piu' costa sempre meno, e' "
            "una tautologia. Il banco non ha nulla da dire sulla qualita' della risposta, che "
            "e' esattamente cio' che si perde tenendo meno cronologia integrale."
        ),
        effect=(
            "Il valore resta 8 per fedelta', non per costo, e il commento nel codice lo dice: "
            "e' un giudizio, non un ottimo misurato. Il caso e' istruttivo perche' e' il "
            "rovescio degli altri: qui la misura c'era ed era corretta, ma rispondeva a una "
            "domanda diversa da quella che sembrava."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="L'effort non veniva mai abbassato sui turni con tool",
        finding=(
            "Il router rifiutava in blocco di abbassare l'effort appena c'erano tool "
            "dichiarati. Contando le valutazioni su tutti gli scenari: lo stadio interveniva "
            "su 12 richieste su 51, e il blocco dominante non era la soglia sulla domanda ma "
            "quel veto, che copriva 23 richieste - il 45% del traffico, incluso il carico di "
            "costruzione che da solo vale il 61% della spesa. La distinzione giusta non e' "
            "'ci sono tool dichiarati' ma 'il modello deve decidere se e quale usarne': con "
            "`tool_choice: none` i tool ci sono e sono inutilizzabili."
        ),
        effect=(
            "Togliere del tutto il veto varrebbe l'11,4% del costo totale, e non e' stato "
            "fatto: il banco modella la lunghezza della risposta in funzione dell'effort, non "
            "la sua qualita', e un effort basso su un turno agentico puo' produrre la chiamata "
            "sbagliata - un tentativo in piu' costa piu' di quanto l'effort abbia risparmiato. "
            "Il default scende a meta' strada (`medium`) e prende la parte sicura: lo stadio "
            "passa dall'1,9% al 3,5% del risparmio, il ciclo agentico dal 48,9% al 53,2%, il "
            "totale dal 70,5% al 72,1%. L'11,4% pieno resta disponibile come scelta "
            "esplicita, con il rischio scritto accanto. Il veto resta invece intatto per il "
            "cambio di modello, dove sbagliare non si paga in token ma in tentativi."
        ),
    ),
]
