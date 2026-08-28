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
    TuningEntry(
        area="gateway",
        title="La potatura del contesto lasciava il confine al valore predefinito del server",
        finding=(
            "Lo stadio valeva 0% nell'ablazione, e la spiegazione accettata era che la soglia "
            "lo tenesse spento di proposito perche' potare distrugge il prompt caching. Vero, "
            "ma non inevitabile: l'edit `clear_tool_uses_20250919` accetta un parametro `keep` "
            "che il gateway non usava affatto. Con `keep` fisso il confine sta sempre a N dal "
            "fondo, quindi scorre di un risultato a ogni turno e l'insieme dei blocchi svuotati "
            "cambia sempre. Il prefisso e' nuovo a ogni richiesta per costruzione."
        ),
        effect=(
            "Ora il gateway sceglie quanti risultati potare dall'inizio, a scatti, e da quello "
            "ricava `keep`: fra uno scatto e l'altro vengono svuotati esattamente gli stessi "
            "blocchi. Sul carico di costruzione la potatura passa da -36,2% a +7,8%, con la "
            "quota di prompt da cache dal 21% all'83%. E' la stessa correzione gia' applicata "
            "al punto di taglio della compattazione, tradotta in un parametro dell'API."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Lo scatto della potatura andava misurato in turni, non in risultati",
        finding=(
            "Con lo scatto contato in risultati i due carichi agentici volevano valori "
            "opposti: il ciclo con sei chiamate per turno preferiva il confine mobile (+10,7%), "
            "quello con una chiamata per turno lo detestava (-36,2%). Non era una differenza di "
            "quanto pesano i tool result - la quota e' identica, 92% contro 93% - ma di "
            "*velocita'*: sei risultati per turno consumano uno scatto sei volte piu' in "
            "fretta, quindi lo stesso numero produce otto turni di stabilita' in un caso e "
            "nemmeno due nell'altro."
        ),
        effect=(
            "Lo scatto e' espresso in turni e convertito in risultati usando il ritmo osservato "
            "della conversazione. Con la stessa impostazione il confine si muove circa una "
            "volta ogni N turni su entrambi i carichi, e entrambi risparmiano. Lo stadio passa "
            "da 0% a 1,2% del risparmio complessivo."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="La soglia di potatura rispondeva alla domanda sbagliata",
        finding=(
            "`trigger_ratio` e' una frazione della finestra del modello, e risponde a 'sono in "
            "pericolo di sforare'. Non risponde a 'conviene potare', che dipende da quanto "
            "materiale vecchio c'e' e non dalla finestra - e le finestre vanno da 200k a un "
            "milione di token, quindi la stessa frazione significa cose molto diverse. "
            "Misurando la soglia sul materiale potabile e' emersa una zona non monotona: a "
            "50.000 token la potatura costa PIU' del non potare affatto, perche' comincia "
            "troppo tardi e sposta il prefisso proprio quando la cache valeva di piu'."
        ),
        effect=(
            "Due condizioni indipendenti: `trigger_ratio` resta la guardia contro l'overflow, "
            "`prune_min_prunable_tokens` decide la convenienza. Misurato il minimo a 20.000 "
            "token di materiale potabile: -4,2% sul corpus completo. Va detto che sotto quella "
            "soglia si scambiano soldi misurati contro fedelta' che il banco non misura - "
            "quel contesto prima restava integrale."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il simulatore ignorava i parametri di clear_tool_uses",
        finding=(
            "L'edit veniva applicato sempre e con un numero fisso di risultati conservati. "
            "Qualunque uso di `keep`, `trigger`, `clear_at_least` o `exclude_tools` era "
            "invisibile alla misura, quindi nessuna strategia di potatura era distinguibile "
            "da un'altra."
        ),
        effect=(
            "Implementati i quattro parametri secondo lo schema ufficiale dell'SDK, non "
            "inventati. Resta un modello dichiarato, ricostruito dalla documentazione e non "
            "osservato: va confermato con `--live` prima di dedurne qualcosa di definitivo. "
            "Senza questa correzione l'intera ottimizzazione della potatura non sarebbe stata "
            "misurabile."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="La potatura pagava scritture di cache che nessuno avrebbe riletto",
        finding=(
            "Il pianificatore e la potatura sono stati misurati per un anno ognuno per conto "
            "suo, e ognuno risultava in guadagno. Contando per la prima volta quante delle "
            "scritture in cache vengono davvero rilette (`ecotokens cachewrites`) e' venuto "
            "fuori che si ostacolano: con il solo pianificatore acceso le scritture orfane a "
            "meta' sessione sono zero, accendendo la potatura a passo 4 diventano 16.999 "
            "token. Ogni volta che il confine avanza, cio' che si era appena pagato 1,25x per "
            "scrivere non e' piu' raggiungibile, perche' il prefisso e' cambiato."
        ),
        effect=(
            "`prune_step_turns` passa da 4 a 7: le scritture orfane calano a 4.509 (-73%) e il "
            "costo scende dell'1,1%. Il vecchio valore era dominato su entrambi gli assi, "
            "quindi non c'e' stato niente da bilanciare. E' la quarta volta che in questo "
            "progetto un confine che insegue la coda si rivela costoso, ma la prima da questo "
            "lato: non 'la cache non trova', bensi' 'si e' pagato di piu' per scrivere "
            "qualcosa che nessuno leggera'."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il conto delle scritture orfane accreditava due volte la stessa rilettura",
        finding=(
            "La prima versione dell'attribuzione prendeva, per ogni scrittura, la lettura piu' "
            "profonda fra tutte le successive della sessione. Su una sequenza in cui il "
            "prefisso riparte da zero e viene riscritto, quella regola dava per ripagata anche "
            "la scrittura precedente, che invece era gia' morta: una sola rilettura ne "
            "giustificava due. L'ha trovata un test scritto sul comportamento atteso, non "
            "sull'implementazione."
        ),
        effect=(
            "La regola si ferma ora alla prima scrittura successiva che riparte da un punto a "
            "monte, perche' da li' in poi la precedente e' irraggiungibile. Lo spreco "
            "misurato sul corpus sale dal 9,0% al 20,0% a passo 4 - il gateway non e' "
            "cambiato, era il conto a essere troppo generoso con se stesso. La scelta di "
            "`prune_step_turns` e' stata rifatta sui numeri corretti."
        ),
    ),
    TuningEntry(
        area="misura",
        title="I breakpoint intermedi non si attivano mai sul corpus",
        finding=(
            "Contando quante volte `_place_intermediate` piazza qualcosa: 43 chiamate, zero "
            "marker. La condizione e' che la coda della conversazione superi i 20 blocchi di "
            "lookback; la coda piu' lunga prodotta dal corpus ne ha 13. Di conseguenza il "
            "gateway usa al massimo 2 breakpoint dei 4 configurati, e `max_breakpoints` sopra "
            "2 non ha alcun effetto osservabile: le tre righe della tabella danno numeri "
            "identici fino all'ultima cifra."
        ),
        effect=(
            "Niente e' stato tolto: il limite dei 20 blocchi e' documentato, e un client "
            "agentico vero con dieci chiamate parallele per turno lo supera. Ma lo stadio "
            "resta **non misurato**, e viene detto invece che lasciato intendere. Servirebbe "
            "uno scenario apposta, che pero' cambierebbe CORPUS_VERSION e azzererebbe i "
            "confronti storici: una decisione da prendere di proposito, non di sfuggita."
        ),
    ),

    TuningEntry(
        area="misura",
        title="Il corpus di misura cresce insieme al codice che misura",
        finding=(
            "Lo scenario `costruzione` legge i quattordici file .py piu' grandi del progetto "
            "**al momento dell'esecuzione**. E' quello che lo rende realistico - e' il carico "
            "vero di una sessione di programmazione assistita - ma significa che ogni commit "
            "che allunga il codice cambia anche il metro. Fra due ablazioni distanti poche "
            "ore, il riferimento 'senza gateway' e' passato da $6,3002 a $6,6338: un +5,3% "
            "che nessuna modifica al gateway aveva prodotto. `CORPUS_VERSION` non se ne "
            "accorge, perche' l'elenco degli scenari non e' cambiato: e' cambiato il loro "
            "contenuto."
        ),
        effect=(
            "Nessun numero riportato finora e' sbagliato - dentro una singola esecuzione il "
            "corpus e' costante, quindi i confronti fra varianti reggono. Sono i confronti "
            "**fra esecuzioni diverse** a essere contaminati, cioe' proprio la sezione dei "
            "progressi della dashboard. La correzione naturale e' registrare un'impronta del "
            "contenuto del corpus accanto a ogni misura e segnalare i confronti che la "
            "attraversano; congelare i sorgenti in un fixture li renderebbe confrontabili ma "
            "smetterebbe di misurare il carico vero. La scelta cambia la versione del corpus, "
            "quindi va fatta di proposito e non di sfuggita."
        ),
    ),

    TuningEntry(
        area="misura",
        title="Il risparmio complessivo non e' un numero del gateway, e' un numero del traffico",
        finding=(
            "Chiesto di portare il risparmio complessivo al 99%, la prima cosa misurata e' "
            "stato il pavimento: cio' che nessuna configurazione puo' togliere. Vale $0,1629 "
            "contro un riferimento di $6,6574, cioe' un massimo teorico del 97,6% - e in quel "
            "conto e' compreso anche lo sconto del 50% della Message Batches API, l'unico "
            "meccanismo del listino che il gateway non usa. Il 99% richiederebbe di stare "
            "sotto $0,0666: due volte e mezzo meno del pavimento. L'obiettivo non era "
            "difficile, era escluso dall'aritmetica, e averlo verificato dopo aver sommato "
            "*ogni* sconto documentato e' cio' che ne fa una dimostrazione invece che una "
            "resa. Misurando invece la stessa cosa al "
            "variare della ripetitivita' del carico, il 99% e' comparso a circa 85 "
            "ripetizioni della stessa richiesta."
        ),
        effect=(
            "Nuovo comando `ecotokens ceiling`, che risponde a un obiettivo dato con un si' o "
            "un no argomentati invece che con un tentativo, e una sezione della dashboard che "
            "affianca al numero di testa il perche' non sia piu' alto. La scala delle leve "
            "tiene una colonna apposta per cio' che si da' in cambio: le ultime due "
            "scambiano denaro contro qualita', che il banco non misura, e senza quella "
            "colonna il 94,7% sembrerebbe un traguardo invece che un prezzo. Nessun default "
            "e' cambiato e il corpus non e' stato toccato: alzare il numero di testa "
            "aggiungendo scenari ripetitivi si sarebbe fatto in dieci minuti, e sarebbe "
            "stata la stessa classe di errore del resto di questo registro, commessa pero' "
            "di proposito."
        ),
    ),

    TuningEntry(
        area="gateway",
        title="Il risparmio del 95% esiste, ma non e' fatto della stessa sostanza del 75%",
        finding=(
            "Portare il risparmio complessivo dal 75,2% al 95% richiede una cosa sola, e "
            "l'ablazione la isola: il cambio di modello vale il 17,5%, piu' di tutti gli "
            "stadi messi insieme tranne il prompt caching. Il resto - effort sempre al "
            "minimo - ne vale 2,6. Nessuna taratura fine ha prodotto niente: sotto la "
            "configurazione aggressiva le soglie gia' scelte sono risultate ottime, "
            "`prune_step_turns = 7` compreso, che regge anche su un modello con un minimo "
            "di cache otto volte piu' alto."
        ),
        effect=(
            "Introdotto il profilo (`prudente` / `aggressivo`), predefinito aggressivo su "
            "richiesta esplicita. La scala dell'ablazione ha due gradini nuovi e separati "
            "invece di uno: fondere 'effort sempre basso' e 'modello economico' avrebbe "
            "nascosto la riga che pesa, e una tabella che nasconde la voce piu' grossa e' "
            "peggio di nessuna tabella. Nel README i due profili stanno in una sezione che "
            "dice cosa li separa: i primi 75 punti sono ottimizzazioni - stessa risposta, "
            "pagata meno - gli ultimi venti sono un'altra risposta a un prezzo diverso. Il "
            "banco misura quanto e' lunga una risposta, non se e' giusta, quindi quel 17,5% "
            "e' interamente misurato e il suo costo interamente no."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il profilo sovrascriveva anche le impostazioni scritte a mano",
        finding=(
            "La prima versione applicava il profilo in `model_post_init` senza guardare "
            "cosa l'utente avesse gia' deciso: chi scriveva `model_downgrade = false` nel "
            "proprio file se lo vedeva riacceso, in silenzio. La documentazione del campo "
            "diceva gia' il contrario - 'il profilo imposta dei default' - quindi il codice "
            "contraddiceva la propria docstring. L'ha trovato un test scritto su quella "
            "frase invece che sull'implementazione."
        ),
        effect=(
            "Il profilo consulta ora `model_fields_set` e non tocca i campi valorizzati "
            "esplicitamente. E' il caso peggiore fra i difetti di configurazione: una "
            "impostazione ignorata non lascia traccia da nessuna parte, e chi la legge "
            "continua a credere che valga."
        ),
    ),

    TuningEntry(
        area="gateway",
        title="Il file di esempio dichiarava un profilo e ne configurava un altro",
        finding=(
            "Introdotto il profilo, il file di esempio e' rimasto con i valori prudenti "
            "scritti a mano sotto [router] mentre in testa dichiarava `profilo = "
            "\"aggressivo\"`. I campi espliciti vincono sul profilo - ed e' giusto che sia "
            "cosi' - quindi chi lo avesse copiato avrebbe ottenuto il 75% credendo di avere "
            "il 95%, senza niente che glielo segnalasse."
        ),
        effect=(
            "I tre campi governati dal profilo sono ora commentati, con scritto accanto che "
            "toglierli dal commento significa decidere a mano. Un test carica il file di "
            "esempio vero e verifica che produca davvero il profilo che dichiara. Una "
            "configurazione che si contraddice e' peggio di una sbagliata: la sbagliata "
            "prima o poi si nota."
        ),
    ),

    TuningEntry(
        area="misura",
        title="Il 68% del prompt caching non era del gateway: era del non avere la cache",
        finding=(
            "Anthropic ha reso disponibile il caching automatico: un solo `cache_control` in "
            "cima alla richiesta, il breakpoint sull'ultimo blocco memorizzabile, che avanza "
            "da solo a ogni turno. Da quel momento \"senza gateway\" ha smesso di voler dire "
            "\"nessuna cache\", ma il banco continuava a confrontarsi con il nulla. "
            "Aggiunto quel gradino all'ablazione, il caching automatico vale da solo il "
            "67,8% e il pianificatore di EcoTokens ne aggiunge **0,7**. Per un anno lo "
            "stadio e' stato documentato come 'la leva di risparmio piu' forte del gateway': "
            "era la leva piu' forte del *prompt caching*, che non e' la stessa cosa, e da "
            "quando basta una riga per averla non e' piu' merito di nessuno."
        ),
        effect=(
            "Il riferimento non e' cambiato - resta utile sapere quanto costa non usare la "
            "cache affatto - ma la scala ha un gradino in piu' fra quello e il "
            "pianificatore, e ogni percentuale del gateway va ora letta a partire dal 67,8%. "
            "Il pianificatore ha guadagnato anche una modalita' `automatico` che delega al "
            "server: se il carico non e' quello giusto, e' la scelta migliore e costa zero "
            "manutenzione."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il pianificatore serve a una cosa sola, e non e' quella che sembrava",
        finding=(
            "Lo 0,7% aggregato nasconde due comportamenti opposti. Su ogni conversazione "
            "singola che cresce - chat, ciclo agentico, costruzione - il pianificatore di "
            "EcoTokens costa lo 0,1-0,2% **in piu'** del caching automatico: piazza due "
            "breakpoint dove ne basta uno, e la seconda scrittura si paga 1,25x senza "
            "aggiungere niente. Su richieste diverse che condividono il prompt di sistema "
            "invece rende il 19,9%, e la ragione e' strutturale: il caching automatico mette "
            "il breakpoint sull'ultimo blocco, cioe' **dopo** la domanda, quindi la voce "
            "creata non serve a nessun'altra domanda; il breakpoint su system+tools crea "
            "invece una voce che tutte le richieste successive rileggono."
        ),
        effect=(
            "Il pianificatore manuale conviene quando piu' richieste condividono un prefisso "
            "e differiscono in coda - assistenti con un system prompt grande, "
            "classificazioni, estrazioni - e non conviene su una conversazione sola che "
            "cresce, dove il server fa lo stesso lavoro meglio. E' documentato cosi' invece "
            "che come 'la leva piu' forte', e `cache_planner.mode` permette di scegliere. "
            "Che il numero aggregato fosse la media di un -0,2% e di un +19,9% e' il motivo "
            "per cui una media, da sola, non e' mai una misura."
        ),
    ),

    TuningEntry(
        area="gateway",
        title="La cache semantica caricava insieme due backend con ruoli diversi",
        finding=(
            "`_load_backend` importava numpy e fastembed nello stesso try: mancando il "
            "secondo si spegneva anche cio' che il primo sa fare da solo. Il modello di "
            "fastembed si scarica dalla rete, e i test di questo progetto non devono "
            "toccare la rete, quindi lo stadio era di fatto non provabile: 112 istruzioni "
            "al 22% di copertura. E' la combinazione peggiore possibile - spedito, "
            "rischioso e non verificato - proprio sull'unico stadio che possa restituire "
            "una risposta **sbagliata**."
        ),
        effect=(
            "I due caricamenti sono separati e lo stadio accetta un embedder qualunque, "
            "purche' abbia `embed(testi)`. La copertura passa dal 22% all'80% con un "
            "embedder deterministico scritto a mano, che permette di provare la soglia di "
            "similarita' al bordo invece che per tentativi. La cucitura serve anche in "
            "produzione: chi ha gia' un servizio di embedding non deve installarne un "
            "secondo."
        ),
    ),
    TuningEntry(
        area="misura",
        title="La copertura media diceva 73% e nascondeva dov'erano i buchi",
        finding=(
            "Misurata per la prima volta, la copertura complessiva era del 73% - un numero "
            "che non allarma. Distribuita, diceva un'altra cosa: `api/errors.py` al 29%, "
            "cioe' il codice che gira **solo quando le cose vanno male**; `stream.py` al "
            "64%, con l'intero percorso 'risposta dalla cache servita in streaming' mai "
            "esercitato - il caso felice della funzione che risparmia di piu'; "
            "`budget.py` al 62%, cioe' la rete di sicurezza. I test seguivano il percorso "
            "felice, che e' il percorso che meno ha bisogno di test."
        ),
        effect=(
            "Trentacinque test nuovi mirati su quei tre moduli: errori al 100%, budget al "
            "100%, streaming all'84%. Nessuno di essi era difficile da scrivere, il che e' "
            "il vero contenuto della voce - non erano mancati per difficolta' ma per "
            "ordine di scrittura. Il totale sale dal 73% al 76%, e quel +3 vale piu' di "
            "quanto sembri: e' tutto concentrato dove il fallimento e' silenzioso."
        ),
    ),
    TuningEntry(
        area="misura",
        title="La dashboard smentiva la propria tabella sugli stadi che cambiano la risposta",
        finding=(
            "Il pannello 'Configurazione in vigore' apriva con una frase scritta a mano: "
            "gli stadi capaci di cambiare il **contenuto** di una risposta - cache "
            "semantica e cambio di modello - 'sono spenti per scelta'. Sotto, la tabella "
            "generata dallo stato reale mostrava `cambio di modello: attivo`, perche' nel "
            "frattempo il profilo predefinito era diventato `aggressivo`. La didascalia era "
            "rimasta ferma al giorno in cui era stata scritta. Con lo stesso difetto, la "
            "voce dell'effort si chiamava 'effort adattivo' anche con "
            "`effort_policy = sempre_basso`, cioe' quando di adattivo non era rimasto "
            "niente e l'effort scendeva anche sulle domande difficili."
        ),
        effect=(
            "Nessun numero cambia: cambia chi li scrive. La frase si deduce ora dallo stato "
            "degli stadi, e il nome della voce dell'effort segue la politica in vigore. Col "
            "profilo aggressivo il pannello dice che il totale include una risposta diversa, "
            "non solo un prezzo diverso; col prudente dice che e' la stessa risposta pagata "
            "meno. Un test lo tiene fermo: nessuno stadio puo' risultare acceso nella "
            "tabella e spento nella didascalia. Vale la pena distinguere il tipo di danno - "
            "un dato sbagliato prima o poi si nota perche' stona con il resto, mentre due "
            "affermazioni contrarie sulla stessa pagina lasciano il lettore senza modo di "
            "scegliere, e la piu' rassicurante delle due vince."
        ),
    ),
]
