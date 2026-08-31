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
    TuningEntry(
        area="misura",
        title="Dal vivo il declassamento del modello valeva zero, e a volte meno di zero",
        finding=(
            "La contabilita' prezzava la baseline - 'quanto sarebbe costata questa "
            "richiesta senza gateway' - sul modello che il **router aveva scelto**, "
            "non su quello che il client aveva chiesto. Il router pero' riscrive "
            "`ctx.model`, quindi con il declassamento acceso il confronto diventava "
            "'Haiku senza cache contro Haiku con cache': il risparmio del cambio di "
            "modello spariva del tutto. Sul banco lo stesso stadio vale il 17,3%, "
            "perche' li' il confronto e' fra due esecuzioni intere e il modello di "
            "partenza non viene perso. Le due misure dicevano cose diverse dello "
            "stesso stadio, e nessuno le aveva mai messe una accanto all'altra: la "
            "pagina che le ha affiancate e' stata scritta questa settimana."
        ),
        effect=(
            "Peggio dello zero: bastava una scrittura di cache non ancora ripagata "
            "perche' il risparmio risultasse **negativo**, e la console apriva con "
            "'4 richieste sono costate piu' che senza gateway'. E' la terza volta che "
            "uno strumento rotto dichiara dannoso il gateway. Il contesto conserva ora "
            "`requested_model`, valorizzato in `__post_init__` cosi' nessun costruttore "
            "puo' dimenticarsene, e la baseline si prezza su quello: sullo stesso "
            "traffico il risparmio passa da -0,0% a 80,3%. Resta una approssimazione "
            "dichiarata in pagina: i token di prompt sono gli stessi nei due casi e "
            "quella meta' del conto e' esatta, quelli generati no, perche' un modello "
            "diverso avrebbe scritto una risposta di lunghezza diversa."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Contare le note per stadio le contava una per richiesta",
        finding=(
            "La console attribuisce ogni nota allo stadio che l'ha prodotta e ne conta "
            "le occorrenze; gli avvisi sono conteggi di quelle. Ma le note citano "
            "quantita' - 'prompt stimato 2188 token, sotto la soglia' - e ogni "
            "richiesta ne ha una sua, quindi quindici richieste producevano quindici "
            "note distinte da uno. Con il taglio alle prime sei, il primo avviso della "
            "pagina diceva '11 richieste' dove erano 14, e la riga sotto ne mostrava 14: "
            "due numeri contrari nella stessa schermata."
        ),
        effect=(
            "Le note si contano su una forma normalizzata - le cifre sostituite - e "
            "l'elenco non viene piu' troncato, perche' normalizzate le forme distinte "
            "sono poche: una per cosa che lo stadio sa fare. Mostrata resta una nota "
            "vera, la piu' recente di quella forma, perche' 'prompt stimato N token' "
            "non e' una frase che qualcuno voglia leggere. La lezione e' la solita in "
            "veste nuova: un conteggio troncato non e' un conteggio approssimato, e' un "
            "conteggio sbagliato, e sembra giusto."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Un'ottimizzazione contata da un anno e mai applicata",
        finding=(
            "La tavola in `wording` raccoglie il testo che il gateway aggiunge di suo "
            "e ne dichiara la forma corta accanto a quella precedente; `ecotokens "
            "overhead` ne stampa il risparmio. Ma la tavola e' un elenco: non obbliga "
            "nessuno a usarla. Due punti la ignoravano. `memory.py` importava "
            "`MEMORY_OPEN` e poi scriveva a mano `<memoria-rilevante>`; `context.py` "
            "faceva lo stesso con la forma lunga del riassunto. Ventiquattro token per "
            "richiesta che il cruscotto contava come gia' risparmiati e che nessuna "
            "richiesta ha mai risparmiato."
        ),
        effect=(
            "E' la variante peggiore della famiglia, per una ragione asimmetrica: "
            "un'ottimizzazione **mancante** prima o poi la si cerca, una **contata e "
            "mancante** no, perche' il cruscotto dice che c'e'. Corretti i due punti e "
            "aggiunti test che guardano il codice invece della tavola: nessuna forma "
            "lunga puo' restare scritta a mano, e nessuna voce della tavola puo' "
            "restare senza qualcuno che la emetta - il secondo copre il difetto "
            "simmetrico, che sarebbe contare un costo mai pagato."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Potare perdeva il 100% dei fatti, e nessuno strumento lo vedeva",
        finding=(
            "Quattro funzioni - memoria, cache semantica, declassamento di modello, "
            "effort minimo - erano spente o non misurate per lo stesso motivo: il banco "
            "vede il loro costo e non il loro beneficio. Non erano quattro problemi, "
            "era uno solo. La domanda intera (la risposta e' ancora giusta?) non e' "
            "misurabile senza un modello che ne giudica un altro, cioe' un metro con "
            "opinioni; ne contiene pero' una piu' piccola e deterministica - "
            "l'informazione necessaria e' arrivata fino al prompt? Se non c'e', nessun "
            "modello puo' rispondere, e la verifica e' la ricerca di una stringa. Nuovo "
            "comando `ecotokens ritenzione`: pianta un dato a un turno, lo chiede venti "
            "turni dopo, guarda il prompt in partenza."
        ),
        effect=(
            "Il primo risultato: con potatura e riassunto accesi sopravvive lo **zero "
            "per cento** dei fatti piantati, su ogni scenario; con la memoria accesa, "
            "il cento. Il banco non poteva dirlo in nessun modo, e cambia il senso "
            "della potatura - resta una difesa contro l'overflow, non un'ottimizzazione "
            "da accendere a cuor leggero. Due difetti di metodo trovati costruendolo. "
            "Le domande finali fatte tutte a partire dalla stessa cronologia sono "
            "biforcazioni, non una conversazione: il gateway le riconosce come sessioni "
            "diverse, e la memoria risultava persa quando era solo cambiata sessione. E "
            "i token di due varianti potate non sono confrontabili, perche' possono "
            "trovarsi in punti diversi del ciclo di compattazione: si e' vista la "
            "memoria risultare piu' economica della sola potatura per aver fatto "
            "scattare un riassunto un turno prima. La tabella riporta ora i riassunti "
            "nuovi accanto ai token, cosi' l'anomalia si spiega invece di essere creduta."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Accorciare i fatti ha rotto il modo di ritrovarli",
        finding=(
            "I fatti della memoria si rispediscono a ogni richiesta successiva, quindi "
            "vanno scritti telegrafici: 'Porta: 8443' costa 4 token, 'La porta di "
            "ascolto deve restare la 8443' ne costa 25. Cambiate le regole date "
            "all'estrattore, il risparmio vale sotto qualunque tokenizer, perche' non "
            "e' una sostituzione lessicale - e' dire di meno. Ma il recupero dei fatti "
            "e' **lessicale**: cerca le parole della domanda dentro i fatti, e "
            "accorciandoli si sono tolte le parole su cui il match si reggeva. "
            "Misurato: su domande che usano sinonimi - 'su quale interfaccia devo "
            "mettermi in ascolto?' contro 'Porta: 8443' - il recupero per pertinenza "
            "trova **zero fatti su tre**. Con i fatti in prosa ne trovava tre su tre."
        ),
        effect=(
            "Due decisioni giuste che, prese insieme, si rompevano a vicenda, e nessuna "
            "delle misure esistenti poteva vederlo: una guarda i token, l'altra la "
            "ritenzione a parita' di parole. Serviva uno scenario in cui domanda e "
            "fatto non condividono niente. Nuova modalita' di recupero stabile, ora "
            "predefinita: tutti i fatti della sessione, in ordine d'inserimento fisso, "
            "dentro il prefisso memorizzabile invece che in coda. Immune per "
            "costruzione. L'ipotesi diceva che sarebbe stata anche piu' economica, con "
            "un'aritmetica che dava +21% a venti turni: **la misura dice il contrario**, "
            "fra -0,4% e -2,2%, perche' il blocco e' piccolo e la scrittura in cache si "
            "paga 1,25x. Il default cambia lo stesso, ma per l'altro asse - il 2% e' il "
            "prezzo di un recupero che funziona - e l'aritmetica fatta a tavolino resta "
            "il modo piu' rapido di convincersi di una cosa falsa."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il trasporto verso il database valeva 65 volte il lavoro",
        finding=(
            "Ogni richiesta tocca il database otto volte, e ogni operazione passava "
            "da `asyncio.to_thread`. Misurato: una `SELECT 1` costa **6,9 us** dentro "
            "SQLite e **448** attraverso il wrapper. Il salto fra thread esiste per non "
            "bloccare il loop, ma su un database locale la query e' piu' corta dello "
            "scheduling che si voleva evitare: erano 3,5 ms di solo trasporto su 15,8 "
            "totali per richiesta."
        ),
        effect=(
            "Il percorso caldo gira ora sul loop; restano su un thread solo le sette "
            "letture delle pagine di osservazione, che leggono migliaia di righe e "
            "fermerebbero tutto mentre la console si aggiorna. Da **63 a 96 richieste "
            "al secondo**, cioe' il 52% in piu' di traffico dallo stesso codice. Il "
            "numero e' quello del solo gateway, con l'upstream istantaneo: in "
            "produzione l'attesa dell'API lo nasconde, finche' il carico non cresce "
            "abbastanza da farlo emergere. Nessuno l'aveva mai misurato, e il README "
            "punta ora esplicitamente al caso multiutente."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Due volte in un'ora ho confrontato una serie fredda con una calda",
        finding=(
            "Misurando la concorrenza, dodici richieste in parallelo risultavano piu' "
            "lente di dodici in fila: la serie parallela girava per prima e pagava "
            "l'avvio. Scaldando entrambe, i due tempi sono identici - il gateway "
            "serializza, che e' un fatto diverso e piu' utile. Poco dopo, "
            "`cache_write_report` sembrava impiegare 831 ms su ventimila eventi: erano "
            "gli import pigri dentro il metodo, pagati alla prima chiamata. Dalla "
            "seconda erano 58."
        ),
        effect=(
            "Nessuna delle due era una misura sbagliata del gateway: erano due misure "
            "giuste di qualcos'altro. Il rimedio non e' un accorgimento ma un'abitudine, "
            "ed e' entrata nei test - ogni prova di velocita' scalda prima, e quelle "
            "sull'import girano in un processo nuovo, perche' nello stesso interprete il "
            "secondo import e' gratis ed e' esattamente cosi' che una regressione del "
            "genere resta invisibile. Vale anche per il costo di una sessione di lavoro: "
            "il primo giro di qualunque cosa non e' rappresentativo del secondo."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Le pagine che osservano erano il carico piu' pesante",
        finding=(
            "`stage_activity` e `cache_write_report` leggevano ventimila righe - in "
            "pratica tutto il registro - e la console le chiama entrambe ogni cinque "
            "secondi, tenendo il lock del database mentre lo fa. Il registro, dal canto "
            "suo, non veniva mai cancellato: `purge` toccava solo le cache, quindi "
            "cresceva senza limite e le pagine rallentavano con lui. Lo strumento di "
            "osservazione stava diventando la cosa da osservare."
        ),
        effect=(
            "Finestra di duemila richieste per le pagine - la loro domanda e' *cosa sta "
            "succedendo adesso*, non *da sempre* - e dichiarata in pagina, perche' un "
            "conteggio su un sottoinsieme presentato come totale sarebbe il solito "
            "numero plausibile e sbagliato. `cache_write_report` ordinava inoltre per "
            "sessione prima di applicare il limite, quindi doveva ordinare l'intera "
            "tabella: adesso taglia per chiave primaria e ordina dopo. "
            "Per la crescita, due tabelle invece di una politica di cancellazione: il "
            "dettaglio recente e un riepilogo giornaliero. Cancellare e basta avrebbe "
            "fatto **calare i totali storici** a ogni pulizia - il gateway avrebbe "
            "dimenticato di aver risparmiato, cioe' il difetto del metro introdotto di "
            "proposito. `stats` legge da entrambe, e un test verifica che compattare non "
            "sposti i totali di un centesimo."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Lo streaming non era mai stato misurato, ed e' meta' del traffico",
        finding=(
            "Zero richieste su cinquantuno del corpus avevano `stream: true`. Non per "
            "distrazione: il percorso in streaming vive nella rotta HTTP e non in "
            "`Gateway.complete`, che e' la strada che il banco percorre - quindi era "
            "irraggiungibile per costruzione. Il risparmio pubblicato descriveva la "
            "meta' del traffico reale, visto che la maggior parte delle interfacce di "
            "chat trasmette."
        ),
        effect=(
            "Nuovo `ecotokens streaming`, che passa dall'app vera e serve lo stesso "
            "carico nei due modi. I due percorsi coincidono: 63.335 contro 63.367 token "
            "di prompt, letture da cache identiche, **0,12%** di scarto sul costo. La "
            "misura resta fuori dal corpus di proposito - aggiungere uno scenario "
            "cambierebbe il denominatore di tutte le percentuali storiche, e la domanda "
            "e' un'altra: non quanto vale uno stadio, ma se il risultato cambia quando "
            "la risposta arriva a pezzi. Un esito rassicurante non toglie che fosse "
            "ignoto."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il riconoscimento di sessione andava contato, non ottimizzato",
        finding=(
            "Sembrava il costo principale per richiesta - spegnendolo il gateway andava "
            "il 30% piu' veloce - e il piano era renderlo piu' economico, a partire "
            "dalla cronologia che veniva cancellata e riscritta per intero a ogni turno. "
            "La regola del progetto dice pero' di contare prima: su quattro carichi lo "
            "stadio interviene 7 volte su 8, 6 su 7, 8 su 12, 15 su 16. Non e' un caso "
            "come l'effort adattivo, che veniva raffinato mentre un veto lo spegneva."
        ),
        effect=(
            "Spegnerlo costa: sul carico di costruzione il **13,7% in piu'**, perche' "
            "senza sessione non c'e' riassunto da riusare. E la riscrittura della "
            "cronologia impiega 1,8 ms a due righe e 2,7 a ottanta: non cresce con la "
            "conversazione, quindi il costo sono i due round-trip e non le righe - "
            "l'ottimizzazione che si stava per fare non era dove stava la spesa. Il "
            "vero costo era il trasporto verso il database, comune a tutti gli stadi. "
            "Contare prima ha evitato di ottimizzare la cosa sbagliata **e** di spegnere "
            "una funzione che si ripaga."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il pannello faceva vincere il profilo su una scelta esplicita",
        finding=(
            "Il pannello di controllo applica il profilo riscrivendo i campi che "
            "governa - declassamento, politica dell'effort - a meno che l'utente non "
            "li abbia decisi lui nello stesso salvataggio. Il codice riconosceva "
            "quelli **risultati diversi** dal valore corrente, non quelli **inviati**: "
            "chi passava dal profilo prudente all'aggressivo spegnendo nello stesso "
            "momento il declassamento non otteneva niente, perche' `false` coincideva "
            "col valore che c'era gia' e quindi non compariva fra i cambiamenti."
        ),
        effect=(
            "\"Non e' cambiato\" non vuol dire \"non l'ha chiesto\", e trattarli allo "
            "stesso modo faceva ignorare una decisione dell'utente in silenzio - sul "
            "campo che decide se le risposte cambiano. Adesso si guardano le chiavi "
            "inviate. L'ha trovato un test scritto sul comportamento voluto, non sul "
            "codice: e' la seconda volta in questo progetto che un difetto di questa "
            "forma - una condizione plausibile e non equivalente a quella giusta - "
            "viene fuori solo scrivendo cosa ci si aspetta prima di guardare come e' "
            "fatto."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Un bug di uno stadio faceva fallire una richiesta che sarebbe passata",
        finding=(
            "`Pipeline.before` chiamava gli stadi senza alcuna protezione. Qualunque "
            "eccezione dentro memoria, compattazione, router o pianificatore di cache "
            "risaliva fino alla rotta e usciva come 500. Un ottimizzatore che si rompe "
            "trasformava cosi' una richiesta valida in un errore: senza il gateway "
            "sarebbe passata."
        ),
        effect=(
            "Introdotta la regola che governa adesso tutti gli stadi: **un guasto "
            "interno degrada, non abbatte**. Lo stadio rotto viene annullato - con i "
            "parametri riportati a com'erano prima, perche' proseguire con un prompt "
            "riscritto a meta' e' peggio che non riscriverlo - e la catena prosegue. "
            "`PipelineAbort` resta l'unica eccezione, perche' il tetto di spesa deve "
            "poter dire di no. Dopo tre guasti consecutivi lo stadio si spegne, e il "
            "motivo compare nella console al posto di quello dichiarato in "
            "configurazione: uno stadio spento da un bug e uno spento per scelta "
            "sembravano identici."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Una risposta tagliata a meta' veniva consegnata come completa",
        finding=(
            "In streaming, `stop_reason` arriva in `message_delta`. Se lo stream si "
            "chiude prima - un proxy che taglia, una connessione che cade - quel "
            "campo resta assente, e la traduzione lo passava a `finish_reason()`, il "
            "cui valore predefinito e' `\"stop\"`. Il client riceveva mezza risposta "
            "con l'etichetta di risposta finita, indistinguibile da quella giusta."
        ),
        effect=(
            "E' il difetto peggiore trovato finora, perche' non produce nessun errore "
            "da nessuna parte: chi legge la risposta non ha modo di sapere che manca "
            "qualcosa. Ora uno stream chiuso senza `stop_reason` esce con "
            "`finish_reason` nullo e un blocco di errore esplicito; nessuno dei "
            "quattro valori previsti da OpenAI descrive \"la connessione e' caduta\", "
            "e sceglierne uno sarebbe stato sostituire una bugia comoda a "
            "un'informazione mancante. La risposta tagliata non entra piu' in cache, "
            "o un guasto momentaneo sarebbe stato servito per sempre."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Uno stream caduto per errore non veniva messo in conto",
        finding=(
            "Quando lo streaming sollevava, il generatore emetteva un chunk di errore "
            "e usciva **senza passare dalla contabilita'**. Ma a quel punto Anthropic "
            "ha gia' letto tutto il prompt - input, letture e scritture di cache - e "
            "generato i token consegnati fin li'."
        ),
        effect=(
            "La spesa era invisibile: `stats` la sottostimava e il tetto giornaliero "
            "non la contava, quindi si poteva sforare un budget a furia di stream che "
            "cadono senza che nessun contatore se ne accorgesse. Adesso i consumi "
            "raccolti dal traduttore finiscono nel registro anche quando non arriva "
            "nessun messaggio finale."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Gli errori di validazione uscivano nel formato sbagliato",
        finding=(
            "Il progetto aveva gia' la regola - un client OpenAI che non trova "
            "`error.message` fallisce nel proprio parser, e la causa vera sparisce - "
            "ma applicata ai soli errori dell'API a monte. Gli errori generati dal "
            "gateway stesso uscivano come `422` con il campo `detail` di FastAPI. In "
            "piu' un corpo senza messaggi passava del tutto: pydantic non valida i "
            "valori predefiniti, quindi `messages` con `default_factory=list` "
            "accettava una conversazione vuota e la spediva all'API."
        ),
        effect=(
            "Una regola applicata a meta' e' una regola che protegge nel caso raro e "
            "non in quello frequente: un client sbaglia molto piu' spesso la propria "
            "richiesta di quanto l'API a monte vada in errore. Ora la busta e' la "
            "stessa per tutti e due i casi, con `400` invece di `422`, ed e' il codice "
            "su cui i client decidono di non riprovare."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il conto a tavolino diceva che proteggere la pipeline era troppo caro",
        finding=(
            "Salvare i parametri prima di ogni stadio costa una copia: 0,89 ms su una "
            "conversazione da cinquanta turni. Moltiplicato per otto stadi faceva "
            "7 ms, contro i 10,4 ms di CPU per richiesta ricavati dalle 96 richieste "
            "al secondo misurate in precedenza - il 68% del budget. Sulla base di quel "
            "conto la protezione era stata fatta con **un solo** salvataggio per "
            "richiesta, accettando che un guasto annullasse anche il lavoro degli "
            "stadi precedenti."
        ),
        effect=(
            "Misurato A/B, alternando le serie: la differenza fra protetto e non "
            "protetto sta **sotto il rumore** a 0, 10 e 40 turni. Il conto era giusto "
            "e le grandezze sbagliate - rapportava una copia che cresce con la "
            "conversazione al tempo di CPU di una richiesta corta, cioe' proprio "
            "quella in cui la copia non costa niente. Passato al salvataggio per "
            "stadio, che e' la versione giusta: chi si rompe perde solo il proprio "
            "lavoro. E' la settima volta che l'aritmetica a tavolino convince in "
            "fretta di qualcosa di falso."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="La rete di sicurezza apriva la via di guasto che doveva chiudere",
        finding=(
            "La copia dei parametri e' ricorsiva, e i parametri arrivano da fuori. Un "
            "client che manda un contenuto annidato cinquecento volte esauriva lo "
            "stack: `RecursionError` a 500 livelli, sollevato **fuori** dal `try` che "
            "avrebbe dovuto proteggere lo stadio. Il conteggio dei guasti aveva un "
            "difetto gemello: `before` e `after` condividevano lo stesso contatore, e "
            "uno stadio rotto in `before` ha quasi sempre un `after` che non fa niente "
            "e quindi riesce - la serie si azzerava a ogni richiesta e lo stadio rotto "
            "non veniva mai spento."
        ),
        effect=(
            "Un limite di profondita' dichiarato trasforma un errore dell'interprete "
            "in una decisione del gateway, e il salvataggio e' entrato dentro il "
            "`try`: se non si riesce a salvare, si salta lo stadio invece di far "
            "fallire la richiesta - non si ottimizza cio' che non si saprebbe "
            "annullare. Entrambi i difetti li ha trovati un test scritto sul "
            "comportamento voluto, non la rilettura del codice, ed entrambi stavano "
            "**nella correzione**: scrivere una rete di sicurezza e' scrivere codice, "
            "e il codice nuovo e' esattamente dove i bug sono piu' probabili."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il simulatore era piu' permissivo dell'API che simulava",
        finding=(
            "Accettava cinque breakpoint di cache dove l'API ne consente quattro, e "
            "accettava `temperature`, `top_p` e gli altri parametri che i modelli "
            "Claude attuali rifiutano con un 400. Trovato eseguendo `ecotokens "
            "verifica --anche-simulato`, cioe' il giro che il comando stesso "
            "dichiara incapace di dire alcunche' sull'API vera."
        ),
        effect=(
            "Un simulatore piu' permissivo dell'originale non semplifica: nasconde. "
            "Rendeva **vuoti** i test che coprono la sanificazione dei parametri - "
            "il mestiere di `translate/to_anthropic.py`, il file piu' delicato del "
            "progetto - perche' passavano anche se il gateway avesse smesso di "
            "rimuoverli; e avrebbe lasciato passare un pianificatore che emette "
            "cinque breakpoint, con i test verdi proprio sul caso che dovevano "
            "cogliere. La lezione sta nel modo in cui e' venuto fuori: il confronto "
            "circolare non dice niente sull'originale, ma dice molto sulla copia."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="La chiusura si fermava al primo intoppo",
        finding=(
            "`shutdown` eseguiva potatura della cache, chiusura del database e "
            "chiusura del client HTTP in fila senza protezione. Una potatura fallita "
            "- database in sola lettura, disco pieno - lasciava aperti gli altri due, "
            "cioe' proprio le risorse che la chiusura esiste per rilasciare."
        ),
        effect=(
            "I tre passi sono indipendenti e adesso vengono tentati tutti. E' lo "
            "stesso principio del fail-open applicato all'uscita: un guasto durante "
            "la pulizia non deve trasformarsi in una perdita di risorse."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il numero in cima si misurava contro un fantoccio",
        finding=(
            "`baseline_cost_usd` prezza la stessa richiesta con tutti i token di "
            "prompt a tariffa piena, cioe' un client che non usa affatto il prompt "
            "caching. La console lo etichettava \"Senza gateway: stesso traffico a "
            "prezzo pieno\" e ne ricavava il risparmio. Ma chiunque integri l'API "
            "oggi mette un `cache_control` sul proprio system prompt - una riga, ed "
            "e' la pratica documentata - e ottiene lo sconto sul prefisso stabile "
            "senza installare niente."
        ),
        effect=(
            "Il gateway si prendeva il merito della meta' piu' grossa del numero. "
            "Aggiunta una seconda baseline, e la console mostra ora **Merito del "
            "gateway** in cima e il risparmio totale sotto, con un avviso quando il "
            "primo scende sotto il 2%. Misurato: +21,1% su una conversazione che "
            "cresce, -4,6% su molti utenti a turno singolo, +87,1% su domande che "
            "si ripetono. I numeri gia' pubblicati - +19,9% e -0,2% - non erano "
            "sbagliati: misuravano un **concorrente diverso**, la delega automatica "
            "di Anthropic, che mette il breakpoint dopo la domanda. Tenere un solo "
            "concorrente e' il modo piu' facile di dire una cosa vera che inganna."
        ),
    ),
    TuningEntry(
        area="misura",
        title="La baseline nuova deduceva lo stato del concorrente dalla nostra politica",
        finding=(
            "Per sapere se il concorrente avrebbe avuto il prefisso gia' in cache, "
            "la prima versione guardava i **nostri** `cache_read_tokens`. Spegnendo "
            "il pianificatore quel numero va a zero, il concorrente risultava freddo "
            "su ogni richiesta e gli si addebitava una scrittura a 1,25x: il merito "
            "del gateway saltava da -5,8% a +8,0%."
        ),
        effect=(
            "Bastava smettere di ottimizzare per sembrare piu' bravi di 13,8 punti. "
            "Circolare, e nella direzione comoda - la stessa forma della trappola "
            "caldo/freddo gia' calpestata due volte in questo progetto, alla terza "
            "occorrenza. Adesso la domanda va posta al traffico: se lo stesso "
            "`tools` + `system` e' passato di qui negli ultimi cinque minuti, era "
            "caldo per chiunque. E' una proprieta' di cio' che arriva, non di cio' "
            "che decidiamo."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il breakpoint sulla coda scriveva in cache cio' che nessuno rileggeva",
        finding=(
            "Il pianificatore marcava l'ultimo blocco dell'ultimo messaggio a ogni "
            "richiesta, primo turno compreso. Su traffico a turno singolo - molti "
            "utenti diversi, stesso system prompt - quel marker scrive una coda "
            "unica di quella richiesta, che nessuno rileggera' mai: 1,25x invece di "
            "1x, per niente."
        ),
        effect=(
            "Il pareggio non e' una soglia da scegliere: scrivere costa 0,25x in "
            "piu', rileggere fa risparmiare 0,9x, quindi marcare conviene se la "
            "probabilita' che qualcuno rilegga supera 0,25/0,9 = 27,8%. Il gateway "
            "osserva quella frazione sulle proprie sessioni invece di indovinarla, e "
            "finche' non ne ha almeno venti marca - cioe' resta com'era. Misurato su "
            "traffico a turno singolo: +1,6 punti, e le scritture in cache da 25.046 "
            "token a 3.967. Sulle conversazioni lunghe non cambia niente, perche' li' "
            "la scommessa la vince sempre. Su traffico misto perde un decimo di "
            "punto: la frazione e' una sola per tutta l'installazione, e distinguerla "
            "per prefisso e' il passo successivo - non misurato, quindi non fatto."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Le due meta' della sottrazione erano contate con righelli diversi",
        finding=(
            "Il prefisso del concorrente veniva dallo stimatore locale, che conta 3,6 "
            "caratteri per token; `usage` viene dall'API, che ha il proprio "
            "tokenizzatore - nel simulatore, 4 caratteri. I due numeri finivano nella "
            "stessa sottrazione, e l'11% di scarto fra i righelli finiva tutto nella "
            "differenza."
        ),
        effect=(
            "Bastava a far risultare il gateway **dannoso** - -4,6% - su traffico a "
            "turno singolo, cioe' su un carico comune. Adesso la conversione si "
            "ricava dal rapporto fra la stima e il conteggio reale dello stesso "
            "prompt: nessuna costante nuova, e si aggiorna da sola se lo stimatore "
            "cambia. Vale 1,2 punti."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Si stimava una dimensione che era gia' stata misurata",
        finding=(
            "Dove il breakpoint e' andato sul system, `cache_read_tokens` **e'** il "
            "prefisso stabile contato dall'API: la stessa grandezza che si stava "
            "stimando, misurata da chi poi la fattura. Il conto usava comunque la "
            "stima, che sopravvalutava il prefisso del concorrente e quindi il suo "
            "sconto."
        ),
        effect=(
            "Altri 1,7 punti. Non e' il ragionamento circolare corretto poco prima, "
            "ed e' una distinzione che vale la pena tenere: quello riguardava il "
            "**quando** - se il prefisso fosse caldo - e dedurlo dalla nostra "
            "politica ci premiava per aver smesso di ottimizzare. Questo riguarda il "
            "**quanto**, ed e' una misura dello stesso oggetto. Si prende il minimo "
            "fra stima e osservazione, perche' su una conversazione lunga il nostro "
            "breakpoint copre anche i turni, e accreditarli al concorrente sarebbe "
            "regalargli il lavoro del gateway."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="La regola sul primo turno aspettava venti sessioni per entrare",
        finding=(
            "Il tasso di continuazione veniva restituito solo sopra le venti "
            "sessioni, e come frazione secca. Su un carico di ventiquattro richieste "
            "la regola toccava le ultime quattro: meta' del suo effetto restava sul "
            "tavolo."
        ),
        effect=(
            "La frazione secca su poche sessioni vale zero o uno e decide sul rumore, "
            "ma la decisione giusta minimizza il **costo atteso**, quindi serve una "
            "stima della frazione e non un intervallo di confidenza. Con la media a "
            "posteriori di Jeffreys - `(proseguite + 0,5) / (totali + 1)` - si decide "
            "da cinque sessioni: con zero continuazioni su cinque da' l'8%, con due "
            "su cinque il 42%. Insieme alle due correzioni del metro qui sopra, il "
            "merito del gateway su traffico a turno singolo passa da **-4,6% a "
            "-0,2%**, e sulle conversazioni che proseguono non cambia niente. Zero e' "
            "la risposta giusta: li' il gateway fa esattamente cio' che farebbe un "
            "client accorto, e arrivarci ha richiesto di correggere il metro tre "
            "volte - ognuna delle quali dava un numero plausibile."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il carico su cui il gateway vale di piu' non era mai stato misurato cosi'",
        finding=(
            "Le tre forme di traffico confrontate con il concorrente accorto erano "
            "chat, turno singolo e domande ripetute. Mancava il ciclo agentico - "
            "molti turni, tool che restituiscono blocchi grossi - che e' il carico "
            "piu' vicino a come lavora una sessione di sviluppo, cioe' al modo in "
            "cui questo progetto stesso e' stato costruito."
        ),
        effect=(
            "E' il caso in cui il gateway vale di piu': **+52,0%** su venti turni, "
            "+55,7% con otto chiamate per turno, contro il 3,4% che prende chi marca "
            "solo il proprio system prompt. La ragione e' strutturale e vale piu' del "
            "numero: in un ciclo agentico i risultati dei tool pesano molto piu' del "
            "`system`, quindi il prefisso che conta e' **la conversazione**, e "
            "marcarla bene richiede di sapere dov'e' cresciuta. Non averlo misurato "
            "prima significa che per mesi la documentazione ha citato numeri fra il "
            "-0,2% e il +22% mentre il caso migliore stava a +52 e nessuno lo "
            "guardava: un metro puo' sbagliare anche solo scegliendo cosa misurare."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Un nome di modello sconosciuto veniva prezzato come Opus 5, in silenzio",
        finding=(
            "`resolve_model` ripiega sul modello di default quando non riconosce "
            "un nome, ed e' la scelta giusta per **servire** la richiesta. Ma il "
            "ripiego arrivava anche a `pricing`: `llama-3.3-70b`, "
            "`qwen2.5-coder:32b` o un `claude-opuss-5` sbagliato di battitura "
            "venivano prezzati a 5/25 USD per Mtok e finivano nel merito del "
            "gateway. Peggio: il nome originale veniva scartato in traduzione, "
            "quindi nemmeno il registro poteva accorgersene - `requested_model` "
            "conteneva gia' il ripiego."
        ),
        effect=(
            "Le richieste su modelli fuori catalogo escono dal confronto "
            "(`baseline_ingenua_usd` a zero) e la nota dice il nome e la tariffa "
            "usata; la spesa resta registrata e il tetto continua a contarla, "
            "perche' un guasto degrada e non abbatte. Il difetto e' della stessa "
            "famiglia degli altri sette del metro trovati in questo progetto: non "
            "produce un errore, produce **un numero plausibile**. Vale anche come "
            "risposta a 'e con un LLM locale?' - senza questo, puntare il gateway "
            "su un modello locale avrebbe prodotto dollari risparmiati a tariffe "
            "Opus su token che non costano niente."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Il profilo predefinito spegneva la funzione principale del gateway",
        finding=(
            "Il default spedito era `aggressivo`, che declassa a Haiku 4.5. La "
            "soglia minima di cache di Haiku e' 4096 token, contro i 512 di Opus "
            "5. Misurato su una chat di dieci turni con un system prompt di "
            "~1.000 token: costo $0,02763 contro $0,08570 senza declassamento - "
            "un terzo - ma **token riletti dalla cache: zero**. Con un system di "
            "~5.000 token, 4.236 contro 30.196. Il README lo diceva gia' ('su un "
            "prompt di 100 o 300 parole il profilo aggressivo perde del tutto la "
            "cache'): era documentato e spedito lo stesso."
        ),
        effect=(
            "Predefinito portato a `prudente`. Il risparmio dell'aggressivo e' "
            "reale e resta disponibile con una riga, ma e' di natura diversa - "
            "un'altra risposta a un prezzo diverso, non la stessa a meno - e un "
            "default non dovrebbe scegliere per l'utente quale delle due sta "
            "comprando. Il difetto del **metro** che lo accompagnava e' pero' il "
            "piu' istruttivo: `bench._spegni_tutto` chiama "
            "`applica_profilo_prudente`, quindi tutti i numeri pubblicati dal "
            "progetto erano misurati senza declassamento mentre il profilo "
            "spedito ce l'aveva acceso. La pagina dell'utente e il README "
            "dicevano cifre non confrontabili, e quella dell'utente era piu' "
            "grossa - la direzione che rende un confronto meno sospetto. Ora "
            "`costo_modello_richiesto_usd` separa le due meta' ovunque."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il numero piu' alto del progetto poggiava su un'assunzione mai enumerata",
        finding=(
            "Il +52% sul ciclo agentico si regge su una cosa sola: che marcare la "
            "**conversazione** produca riletture che crescono turno dopo turno. "
            "Quella proprieta' non era fra le undici assunzioni dichiarate. Era "
            "data per vera senza essere elencata, il che e' peggio che darla per "
            "vera dichiarandolo: il registro delle assunzioni esiste proprio per "
            "impedire che una premessa scompaia dalla vista, e la premessa piu' "
            "importante gli era sfuggita."
        ),
        effect=(
            "Aggiunta come dodicesima voce, e con essa il controllo "
            "`verifica._ciclo_agentico` che la mette alla prova in tre chiamate. "
            "Scritto la prima volta sbagliato, e il simulatore l'ha smentito con "
            "`riletture: 0, 0, 0`: la storia veniva tenuta come stringa e "
            "convertita in blocchi solo per l'ultimo messaggio, quindi il "
            "prefisso cambiava forma a ogni turno. Corretto, osserva "
            "`0, 3613, 10858`. Vale come promemoria che un controllo nuovo va "
            "guardato mentre fallisce prima di fidarsi di quando passa - e come "
            "prova che il simulatore, per una volta, era piu' severo del "
            "controllo che doveva sostenere."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Un campo nuovo chiamato `client` avrebbe sostituito il client dell'SDK",
        finding=(
            "Aggiungendo l'attribuzione per client, il campo del contesto e' "
            "stato chiamato `client`. In `RequestContext` esisteva gia' un campo "
            "`client`: il client Anthropic. Un dataclass accetta la "
            "ridefinizione **in silenzio**, e la richiesta sarebbe partita verso "
            "una stringa vuota."
        ),
        effect=(
            "Rinominato `nome_client`. Si e' visto solo perche' la ridefinizione "
            "ha spostato il campo dopo un argomento senza default e Python ha "
            "protestato per l'ordine: senza quella coincidenza sarebbe passato, e "
            "il guasto sarebbe apparso a runtime lontano dalla causa. Vale come "
            "promemoria che in una dataclass grande i nomi sono uno spazio "
            "condiviso, e che il rumore di un errore all'import e' un regalo."
        ),
    ),
    TuningEntry(
        area="misura",
        title="La sensibilita' della deduplica diceva +42,8% dove il risparmio era zero",
        finding=(
            "La deduplicazione dei `tool_result` misurava +61,0% su un ciclo "
            "agentico. Per capire quanto dipendesse dalla ripetizione, la stessa "
            "misura e' stata rifatta variando il numero di file distinti. Il "
            "controllo - sessanta file letti una volta ciascuno, cioe' **nessuna "
            "ripetizione** - dava +42,8%, che e' impossibile. Il generatore "
            "sceglieva il file con `(turno + k) % n`: su venti turni e tre "
            "chiamate quella formula produce solo ventidue indici distinti, "
            "qualunque sia `n`. Il carico si ripeteva da solo."
        ),
        effect=(
            "Corretto in `(turno * chiamate + k) % n`, la scala diventa "
            "coerente: 12 riletture per file **+61,0%**, 3 riletture +43,8%, "
            "nessuna ripetizione **+0,0%** con zero sostituzioni. E' il terzo "
            "numero a rendere credibili i primi due, ed e' quello che mancava. "
            "Un test di controllo che non puo' dare zero non e' un controllo - "
            "e il difetto stava nel generatore del carico, cioe' nel posto dove "
            "in questo progetto si nasconde quasi meta' dei difetti."
        ),
    ),
    TuningEntry(
        area="misura",
        title="`ritenzione` non puo' misurare la deduplicazione: non ha un solo tool_result",
        finding=(
            "Il piano prevedeva di sottoporre la deduplicazione a `ecotokens "
            "ritenzione` prima di accenderla. Gli scenari di ritenzione sono "
            "pero' conversazioni normali: `grep -c tool_result retention.py` "
            "restituisce **zero**. Una variante nuova li' avrebbe misurato "
            "l'inazione - il rischio che il codice di quel modulo segnala da se' "
            "a proposito delle soglie."
        ),
        effect=(
            "La domanda e' stata posta altrove e in forma piu' stretta: la prima "
            "copia resta intatta, quindi il fatto e' ancora nel prompt "
            "(`tests/test_dedup.py`). Resta non verificato che il modello sappia "
            "**usare** un riferimento all'indietro invece del testo, ed e' una "
            "cosa che nessun conteggio di token puo' dire. Lo stadio esce quindi "
            "spento, con l'assunzione dichiarata: la regola del progetto quando "
            "la misura non e' possibile, non un ripiego."
        ),
    ),
    TuningEntry(
        area="strumento",
        title="`diagnosi` diceva OK su una credenziale lunga un carattere",
        finding=(
            "Alla prima chiave vera del progetto, l'incolla dentro `Read-Host "
            "-AsSecureString` non e' passato - in molte console `Ctrl+V` li' non "
            "funziona - e nella variabile e' finito **un solo carattere**. "
            "`ecotokens diagnosi` ha risposto `OK Credenziali Anthropic da "
            "ANTHROPIC_API_KEY`, perche' il controllo chiedeva se la variabile "
            "esistesse, non se contenesse qualcosa di possibile."
        ),
        effect=(
            "Aggiunto `_forma_sospetta`: bordi con spazi, virgolette di `setx` "
            "finite dentro il valore, lunghezza sotto venti caratteri. Il limite "
            "sta basso di proposito - distingue «non e' arrivato niente» da «non "
            "conosco questo formato», e convalidare per davvero e' lavoro del "
            "server. Due cose valgono oltre la correzione. La prima: il difetto "
            "stava nel **comando che esiste per rendere rumorosi i guasti "
            "silenziosi**, e ne aggiungeva uno - sarebbe riemerso come 401 alla "
            "prima richiesta vera, cioe' esattamente lo scenario che il modulo "
            "dichiara di prevenire. La seconda: un test usava `sk-ant-finta`, "
            "dodici caratteri, e il controllo nuovo lo ha bocciato subito. Un "
            "campione finto piu' corto del vero e' un metro piu' permissivo del "
            "reale, ed e' la stessa forma di difetto del simulatore che "
            "accettava cinque breakpoint."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Con una chiave legata a un'identita' il gateway prendeva 400 su tutto",
        finding=(
            "Primo contatto con l'API vera, dopo mesi di misure sul simulatore. "
            "La chiave creata dalla propria utenza su `platform.claude.com` e' "
            "**identity-linked** e l'API pretende l'header "
            "`anthropic-workspace-id`: senza, risponde 400 a qualunque "
            "richiesta. Il gateway non lo mandava. Non un caso di nicchia - e' "
            "cio' che si ottiene creando una chiave nel modo piu' ovvio."
        ),
        effect=(
            "`intestazioni_upstream()` in `config.py`, letta da `[upstream] "
            "workspace_id` o da `ANTHROPIC_WORKSPACE_ID`, applicata dove si "
            "costruisce un client vero. Vale la pena notare **quanto tardi** e' "
            "arrivata questa scoperta: nessun test poteva trovarla, perche' il "
            "simulatore non chiede l'header che non conosce. Il simulatore "
            "risponde alla domanda per cui e' stato scritto e tace su tutte le "
            "altre - ed e' la ragione per cui `verifica --live` esiste e si "
            "rifiuta di girare contro di lui."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Due assunzioni dichiarate confermate da un 400 che parlava d'altro",
        finding=(
            "Nella stessa esecuzione, `verifica --live` ha risposto `OK` a «I "
            "parametri rimossi danno 400» e a «Quattro breakpoint al massimo». "
            "Nessuna delle due era stata messa alla prova: entrambi i controlli "
            "concludevano su `except BadRequestError`, e il 400 ricevuto era "
            "quello sul workspace. Lo strumento che esiste per dire quali "
            "assunzioni reggono ne ha certificate due che non aveva sfiorato."
        ),
        effect=(
            "`_quattrocento_estraneo` confronta il messaggio con cio' che il "
            "controllo ha chiesto; se non combacia l'esito e' INDETERMINATO. "
            "Rifatta la misura, il verdetto e' passato da **2 su 6 combaciano** "
            "a **0 su 6, 6 indeterminate** - cioe' dalla risposta sbagliata a "
            "«non lo so», che e' quella vera. E' il difetto piu' caro che questo "
            "progetto possa avere: un controllo che fallisce si corregge, uno "
            "che passa per la ragione sbagliata si crede. Vale anche in "
            "generale, e non solo qui: **asserire su un codice di errore invece "
            "che sulla sua causa e' un test che passa da solo.**"
        ),
    ),
    TuningEntry(
        area="misura",
        title="L'effort non sembrava cambiare niente: erano due risposte tagliate al tetto",
        finding=(
            "Prima verifica dal vivo riuscita: `low 1.00x, high 1.00x, max "
            "1.00x`, e il controllo ha concluso che abbassare l'effort non "
            "riduce i token generati - cioe' che il primo livello del router "
            "non risparmia. Guardando `stop_reason`: `low` 3940 token e "
            "`end_turn`, `high` **4096** e `max_tokens`, `max` **4096** e "
            "`max_tokens`. Le due risposte alte erano identiche perche' erano "
            "state **tagliate allo stesso tetto**, non perche' l'effort non "
            "faccia niente. Il rapporto 1,00x misurava `max_tokens=4096`."
        ),
        effect=(
            "Tetto alzato a 16.000 e, soprattutto, esito INDETERMINATO se anche "
            "una sola risposta si ferma su `max_tokens`: alzare il limite non "
            "basta, perche' puo' saturare comunque. Nota che il verso che il "
            "controllo cercava era in parte visibile - `low` **ha** generato "
            "meno, e ha finito da solo - e la conclusione l'ha ignorato. Una "
            "misura satura non e' una misura: confrontare due valori entrambi "
            "al tetto misura il tetto."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Ho quasi cancellato il +52% per una condizione che cambiava sotto le mani",
        finding=(
            "Nella stessa esecuzione, `_ciclo_agentico` ha dato `riletture: 0, "
            "0, 0` - la smentita dell'assunzione su cui poggia il numero piu' "
            "alto del progetto, con la nota che dice di togliere quel numero "
            "dal README prima di ogni altra cosa. Sono seguite otto sonde: "
            "dimensione del corpo, ritardo di propagazione, breakpoint stabile "
            "contro breakpoint mobile, alternanza dei ruoli, ripetizione "
            "identica prima dell'estensione. Una di esse ha mostrato che il "
            "prefisso esteso **rilegge** benissimo (2829 riletti, 13 scritti). "
            "L'ultima ha mostrato che nemmeno la richiesta identica ripetuta "
            "rileggeva piu' - il caso che aveva funzionato dieci minuti prima."
        ),
        effect=(
            "La variabile non era la forma della richiesta: era il tempo. Stavo "
            "attribuendo a otto forme diverse una differenza che cambiava da "
            "sola, ed e' la forma di errore piu' costosa possibile qui, perche' "
            "la conclusione sarebbe stata cancellare una cifra pubblicata. "
            "Aggiunto un **testimone**: prima di misurare la tenuta del "
            "prefisso, il controllo manda due volte la stessa richiesta; se "
            "nemmeno quella rilegge, l'esito e' INDETERMINATO e non DIVERGE. "
            "Separata anche la condizione di successo in due: che il prefisso "
            "**regga** e che la rilettura **cresca**. Osservato una volta 2809 "
            "e poi ancora 2809 - regge senza crescere, che e' una tenuta piu' "
            "debole di quella assunta e un'informazione diversa da una "
            "smentita, mentre la vecchia condizione unica le confondeva."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il testimone d'apertura non copriva la misura che doveva sorvegliare",
        finding=(
            "Rifatta la verifica col testimone appena aggiunto: il testimone "
            "**e' passato**, i tre turni hanno dato zero, e la divergenza "
            "sembrava finalmente credibile. Un minuto dopo, in una sonda "
            "separata, lo stesso testimone falliva. La rilettura su questo "
            "account va e viene su una scala di **minuti** - piu' corta della "
            "misura che il testimone doveva convalidare."
        ),
        effect=(
            "Aggiunto un testimone **di chiusura**: se una delle due prove "
            "cade, la finestra non era buona e l'esito e' INDETERMINATO. La "
            "lezione e' piu' generale del caso: un controllo di validita' "
            "eseguito *prima* di una misura assume che le condizioni reggano "
            "per tutta la durata della misura, e quando la grandezza "
            "disturbante varia piu' in fretta della misura stessa quel "
            "controllo non copre niente. Vale per ogni misura di questo "
            "progetto che duri piu' di una chiamata."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="L'effort rendeva meno della meta' di quanto il progetto assumeva",
        finding=(
            "Riparato il controllo, la misura e' stata rifatta come andava "
            "fatta: cinque livelli per **cinque compiti di natura diversa** - "
            "fattuale corto, spiegazione aperta, ragionamento, codice, "
            "estrazione - lanciati insieme livello per livello, tetto a 32.000 "
            "token, nessuna risposta troncata. Mediane osservate contro "
            "dichiarate: low **0,75** contro 0,40, medium **0,94** contro 0,70, "
            "xhigh **1,48** contro 1,60, max **2,22** contro 2,60. Due cose che "
            "un compito solo non avrebbe potuto dire: `medium` non e' una leva "
            "di risparmio (su un compito e' esattamente 1,00), e **il verso non "
            "regge sempre** - su una domanda fattuale corta `xhigh` e `max` "
            "generano *meno* di `high`, perche' non c'e' ragionamento dove "
            "spendere."
        ),
        effect=(
            "Moltiplicatori sostituiti con le mediane misurate. Rimisurata "
            "l'ablazione a parita' di tutto il resto: effort adattivo da "
            "**2,0% a 0,6%**, effort sempre basso da **1,8% a 0,9%**, e il "
            "valore che il gateway aggiunge sopra il caching automatico da "
            "**21,9% a 17,0%** - su una chat da 35,8% a 14,8%, su un ciclo "
            "agentico da 8,2% a 1,4%. **Una sola assunzione sbagliata valeva un "
            "quarto del merito dichiarato**, ed era sbagliata nel verso "
            "comodo. Trovata solo perche' il controllo che la sorvegliava era "
            "stato riparato lo stesso giorno: prima diceva che l'effort non "
            "serviva a niente, e diceva il falso pure quello."
        ),
    ),
    TuningEntry(
        area="misura",
        title="I numeri piu' citati del progetto non li ricalcola nessun comando",
        finding=(
            "Propagando la correzione dell'effort si cercava il comando che "
            "produce la tabella di testa del README - +52% agentico, +87,2% "
            "ripetitivo, +22,6% chat, -0,2% turno singolo, contro uno "
            "sviluppatore che marca il proprio system prompt. **Non esiste.** "
            "Nessuno dei ventidue comandi la riproduce: viene da uno script "
            "estemporaneo, e i valori vivono in due copie scritte a mano, nel "
            "README e in `consiglia.MERITO`."
        ),
        effect=(
            "Segnati in entrambi i posti come da rifare, invece di aggiornarli "
            "a occhio: non avendo il comando, un numero nuovo sarebbe stato "
            "inventato. E' la stessa lezione dell'ablazione, presa dall'altro "
            "lato: rieseguire `ablate` oggi dava numeri diversi dal README "
            "**gia' prima** della correzione, cioe' la tabella era invecchiata "
            "in silenzio. Una misura che nessun comando ricalcola non invecchia "
            "male: invecchia **invisibile**. Scrivere quel comando e' il lavoro "
            "che vale di piu' fra quelli rimasti."
        ),
    ),
    TuningEntry(
        area="misura",
        title="`ecotokens merito`, e il +22,6% sulla chat che era +1,1%",
        finding=(
            "Scritto il comando mancante: cinque carichi, il profilo che **non "
            "cambia la risposta**, e le due baseline portate via dalla pipeline "
            "vera invece di riscrivere la formula - una seconda copia della "
            "stessa aritmetica e' il modo in cui due numeri divergono senza che "
            "nessun test se ne accorga. Primo ricalcolo contro i valori "
            "pubblicati: ciclo agentico +52,3% contro +52,0% (regge), otto "
            "chiamate per turno +49,8% contro +55,7%, domande ripetute +75,6% "
            "contro +87,2%, turno singolo -0,1% contro -0,2%, e **la chat "
            "+1,1% contro +22,6%**."
        ),
        effect=(
            "La riga di testa - quella che descrive un assistente di codice - "
            "regge, ed e' la piu' importante. La chat no, e la colonna centrale "
            "dice perche' senza bisogno di ipotesi: con un system prompt grosso "
            "e turni brevi, chi lo marca da se' cattura gia' il **57,7%**, e "
            "non resta quasi niente da aggiungere. Il gateway serve dove il "
            "prefisso che vale non e' il system prompt - che e' esattamente la "
            "tesi del progetto, ma adesso e' misurata invece che raccontata. "
            "Aggiunto anche un test che confronta `consiglia.MERITO` con la "
            "tabella del README: restano due copie a mano, e senza un test che "
            "le leghi sarebbero libere di divergere di nuovo."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Tre spiegazioni sbagliate di seguito, e la quarta era la mia sonda",
        finding=(
            "Cercando di misurare `merito --live` il testimone diceva zero, poi "
            "4608, poi di nuovo zero nel giro di un minuto. Sono seguite due "
            "spiegazioni, **entrambe sbagliate**, ed erano sbagliate allo stesso "
            "modo: costruite su due campioni per parte.\n\n"
            "La prima: «dipende da dove sta il marcatore» - `system` non "
            "rileggeva e `messages` si'. La seconda: «e' una corsa fra la "
            "scrittura e la rilettura», da 2/3 senza pausa contro 3/3 con "
            "cinque secondi. Portati a sei campioni per parte, la pausa da' "
            "**3/6 contro 3/6**: nessuna differenza. Sommando le diciotto "
            "sonde della giornata, la rilettura di un prefisso appena scritto "
            "riesce **11 volte su 18**."
        ),
        effect=(
            "Seguirono altre due spiegazioni, e anche quelle cadute: una "
            "strozzatura sulle scritture ravvicinate (la raffica fece **3/8** e "
            "il ritmo lento **1/8**, cioe' il contrario) e la dimensione del "
            "prefisso (4/8 contro 4/8). Quarantadue sonde, quattro ipotesi, "
            "nessuna che spiegasse niente.\n\n"
            "Il difetto era nella sonda, e stava in bella vista dall'inizio: "
            "**ogni prova usava un prefisso nuovo di zecca, scritto una volta e "
            "riletto una volta.** E' il caso meno favorevole che esista, e non "
            "e' come si comporta nessun traffico vero. Riprovato con un solo "
            "prefisso riusato - come lo riusa qualunque conversazione - la "
            "rilettura riesce **7 volte su 7**, cioe' e' deterministica. La "
            "moneta al 57% non descriveva il caching: descriveva come lo stavo "
            "interrogando.\n\n"
            "Il testimone aveva lo stesso difetto - ci metteva davanti un uuid "
            "per essere sicuro di misurare una scrittura nuova - e quindi "
            "misurava il caso peggiore per decidere se si poteva misurare "
            "quello normale, bloccando tre misure buone di fila. E' l'errore "
            "piu' sottile della giornata perche' **assomiglia al rigore**: la "
            "scelta che sembrava piu' severa era quella non rappresentativa. "
            "Ora il testo del testimone e' fisso."
        ),
    ),
    TuningEntry(
        area="strumento",
        title="La guardia che si rifiuta di spendere ha gia' pagato il proprio costo",
        finding=(
            "`merito --live` sui cinque carichi fa sessanta richieste con prompt "
            "agentici che crescono fino a decine di migliaia di token: qualche "
            "dollaro. In una finestra in cui la cache non rilegge, quelle "
            "sessanta richieste concluderebbero che il gateway non serve a "
            "niente - descrivendo il momento invece del gateway."
        ),
        effect=(
            "Due chiamate di testimone prima di cominciare, e se non rilegge il "
            "comando **non parte** e lo dice. Al primo giro dal vivo ha fermato "
            "la spesa un minuto dopo che una sonda a mano leggeva 2821: ha gia' "
            "guadagnato piu' di quanto costa, e ha reso indipendente da chi "
            "lancia il comando una prudenza che fino a un'ora prima dipendeva "
            "da chi si ricordava di averla."
        ),
    ),
    TuningEntry(
        area="misura",
        title="Il numero di testa, misurato sull'API vera: +76,0% dove il simulatore diceva +52,3%",
        finding=(
            "`ecotokens merito --live` sul carico agentico, 21 richieste "
            "vere su claude-opus-5. Contro uno sviluppatore che marca il "
            "proprio system prompt: **+76,0%**, dove la stessa misura sul "
            "simulatore da' +52,3%. La colonna centrale - lo sconto che "
            "Anthropic regala a chiunque - combacia invece quasi esattamente: "
            "2,9% dal vivo contro 2,1% simulato."
        ),
        effect=(
            "E' la prima volta che l'affermazione centrale del progetto ha "
            "sotto una misura non simulata, e il simulatore **sottostimava**. "
            "La spiegazione piu' probabile e' una sua limitazione gia' "
            "dichiarata: conta i token in proporzione alla lunghezza del testo "
            "invece di usare il tokenizer vero. Il verso dell'errore e' quello "
            "giusto - per mesi il numero pubblicato e' stato piu' basso del "
            "reale - ma resta un errore del 45% in relativo, e vale la pena "
            "ricordarlo prima di fidarsi di qualunque altra cifra simulata. "
            "Un carico, un modello, una esecuzione: le altre quattro righe "
            "costano qualche dollaro l'una e restano simulate."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="Un `system` scritto come stringa non veniva mai messo in cache",
        finding=(
            "Scrivendo la libreria, il primo test che chiedeva «lo stadio che "
            "vale il +76% fa qualcosa?» ha risposto **no**. Il pianificatore "
            "attacca il `cache_control` solo a un blocco, e controlla "
            "`isinstance(system, list)`: l'API pero' accetta `system` anche "
            "come **stringa**, che e' la forma che scrive quasi chiunque. In "
            "quel caso non marcava niente e non lo diceva."
        ),
        effect=(
            "Il difetto non era della libreria: era della porta nativa "
            "`/v1/messages`, e c'era da sempre. Un client che manda una "
            "stringa vedeva il gateway funzionare, il prefisso non andare mai "
            "in cache, e nessun errore da nessuna parte - la famiglia di "
            "guasti che `diagnosi` esiste per stanare, questa volta invisibile "
            "anche a lui. `make_native_context` converte ora la stringa in un "
            "blocco: il modello legge la stessa cosa, il prefisso diventa "
            "marcabile, e le due porte mandano la stessa forma (due forme "
            "diverse sono due voci di cache invece di una).\n\n"
            "La lezione non e' sul `system`. **Scrivere una seconda faccia "
            "dello stesso motore ha trovato in un pomeriggio un difetto che "
            "657 test non vedevano**, perche' i test guardavano tutti dalla "
            "stessa parte: passavano dal dialetto OpenAI, dove la traduzione "
            "converte il `system` in blocchi da sola e copriva il buco."
        ),
    ),
    TuningEntry(
        area="gateway",
        title="EcoTokens come libreria: la chiave non si muove piu'",
        finding=(
            "Le misure dicono che il valore e' concentrato in due stadi su "
            "nove, e che il piu' grosso - il pianificatore, +76,0% dal vivo su "
            "un ciclo agentico - **non ha bisogno di niente di condiviso**. "
            "Chi guadagna di piu' e' pero' chi scrive il proprio codice, cioe' "
            "esattamente chi una riga la aggiunge senza pensarci e un processo "
            "in piu' non lo vuole: si chiedeva lo sforzo maggiore a chi aveva "
            "il guadagno maggiore."
        ),
        effect=(
            "`ecotokens.Economico` avvolge un `anthropic.AsyncAnthropic` e "
            "restituisce gli stessi oggetti `Message`. L'ostacolo che toglie "
            "non e' la comodita': e' che **la chiave non passa piu' da un "
            "programma di terzi**, che e' l'obiezione che in un'azienda ferma "
            "l'adozione prima di ogni discussione tecnica.\n\n"
            "Due stadi valgono meno in questa forma, e sta scritto nel modulo "
            "invece che scoperto dopo: cache esatta e contabilita' vivono in "
            "memoria e muoiono col processo, a meno di passare `memoria=`. Il "
            "gateway resta per i tre casi che una libreria non copre: "
            "applicazioni non tue, tetto di spesa comune, cache condivisa fra "
            "processi. Lo streaming per ora **dice di no** invece di passare "
            "dritto fingendo di risparmiare."
        ),
    ),

]
