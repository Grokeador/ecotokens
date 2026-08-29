# EcoTokens — istruzioni di lavoro

Gateway locale per **Claude** che riduce la spesa in token. Un solo provider
(Anthropic), due porte in ingresso: `/v1/chat/completions` in dialetto OpenAI e
`/v1/messages` in dialetto nativo. "OpenAI-compatibile" descrive la forma della
porta, non la destinazione — è un malinteso facile, evitarlo nei testi.
Codice e commenti in italiano, senza accenti nelle stringhe di codice (`e'`,
non `è`); il testo per l'utente li usa normalmente.

Questo file è caricato in ogni sessione: **tenerlo corto è parte del punto**.
Oltre le ~100 righe, qualcosa va spostato nel README.

## Le due regole

**Non si dichiara un risparmio: si misura.** Quasi metà delle voci del
[registro delle correzioni](ecotokens/tuning_log.py) sono difetti del *metro*,
non del prodotto. Tre volte il gateway è stato dichiarato dannoso o inutile da
uno strumento rotto. Prima di accendere uno stadio, cambiare un default o
scrivere che qualcosa risparmia: `bench`, `ablate`, o una misura mirata. Se non
è misurabile, si dice che non lo è.

**Un guasto interno degrada, non abbatte.** Il gateway sta in mezzo: uno stadio
che si rompe va annullato — parametri riportati a prima, perché un prompt
riscritto a metà è peggio di uno non riscritto — e la catena prosegue. L'unica
eccezione è il tetto di spesa, che esiste per dire di no. Vale anche in
sessione: se uno strumento fallisce si continua senza, non si molla il compito.

## Le cose che abbiamo scoperto misurando, e che valgono anche qui

Non teoria: i vincoli che governano il costo di *questa* sessione.

1. **Il prefisso è tutto, e conta contro chi lo si misura.** Il caching è un
   match di prefisso: un byte diverso all'inizio invalida tutto il resto. In
   sessione: non rileggere file già letti, non rifare la stessa ricerca con
   altre parole, non cambiare modello a metà. Il risparmio del gateway cambia
   di segno secondo il concorrente scelto, e tenerne uno solo è il modo più
   facile di dire una cosa vera che inganna:
   - contro chi **non usa la cache**: 67,8%. Nessuno lo fa: è un fantoccio.
   - contro la **delega automatica** di Anthropic (`cache_control` in cima
     alla richiesta): +19,9% a prefisso condiviso, −0,2% su conversazione
     singola. Il server mette il breakpoint *dopo* la domanda, quindi la voce
     che crea non serve a nessun'altra domanda.
   - contro uno sviluppatore che mette il `cache_control` **sul proprio system
     prompt** — una riga, ed è la pratica documentata: **+52%** su un ciclo
     agentico con tool, **+22,6%** su una chat che cresce, **−0,2%** su molti
     utenti a turno singolo, **+87,2%** su domande ripetute. Il primo è il più
     alto perché lì i risultati dei tool pesano molto più del `system`: chi
     marca solo quello cattura il 3%.

   È il terzo che risponde a «conviene installarlo», ed è quello che il
   registro ora calcola su ogni richiesta.
2. **Ogni turno rispedisce tutto.** Il costo di un ciclo agentico cresce con il
   *numero di turni*, non solo con la loro dimensione. Chiamate indipendenti
   vanno fatte **nello stesso messaggio**: due Bash in parallelo costano un
   round-trip, in sequenza ne costano due, e il secondo rispedisce il primo. È
   la leva più grossa di una sessione.
3. **Leggere mirato.** `Read` con `offset`/`limit`, `Grep` con `-A`/`-B`.
4. **Accorciare il prompt rende un quarto.** Togliere mille token rende
   ~$0,0014 contro $0,0050, perché la cache li aveva già scontati. Non conviene
   contorcersi per essere brevi; conviene non rileggere.
5. **Comprimere e mettere in cache tirano in direzioni opposte.** Riscrivere
   ciò che sta all'inizio del contesto costa più di quanto rende, a meno che
   non sia **deterministico e stabile**.
6. **Due decisioni giuste possono rompersi a vicenda.** Fatti telegrafici (si
   pagano sempre) più ricerca lessicale (gratuita): insieme, zero fatti trovati
   su tre, perché accorciando si tolgono le parole su cui il match si regge.
   Cambiando due cose, misurare la coppia.
7. **L'aritmetica a tavolino convince in fretta di cose false.** Un conto dava
   +21% a spostare i fatti nel prefisso; misurato, −0,4%. Un altro dava la
   protezione della pipeline al 68% del budget di CPU; misurata, sotto il
   rumore. I conti erano giusti, sbagliate le grandezze dentro.

## Il circolo fra sessione e prodotto

Va tenuto aperto nelle due direzioni, ed è perché questo file esiste. Una
pratica di lavoro utile e automatizzabile diventa uno stadio; una misura che
smentisce un'intuizione cambia la regola *anche qui sopra*.

**Contare prima di progettare.** L'effort adattivo sembrava un'euristica da
migliorare: un veto lo spegneva sul 45% del traffico. `ecotokens ritenzione`
fa la domanda gemella — *ciò che serviva è arrivato al prompt?* — e la prima
risposta è stata **zero fatti su tutti** con la potatura accesa.

Protocollo, quando si trova qualcosa:

1. Misurarlo — un confronto A/B, non un'impressione.
2. Se regge: implementarlo nel gateway *e* aggiornare questo file.
3. Aggiungere una voce a `ecotokens/tuning_log.py`, distinguendo se il difetto
   era del **metro** o del **gateway**: correggere il metro non migliora il
   prodotto, rende visibile com'era già.
4. Se la misura non è possibile, dirlo e lasciare la funzionalità **spenta**
   (vedi `prompt.only_verified`).

## Trappole già calpestate — non ricalpestarle

- **Il primo giro non è rappresentativo del secondo.** Scaldare prima di
  misurare; per gli import, un processo nuovo.
- **Gli heredoc di Git Bash mangiano i backslash.** Per file con sequenze di
  escape nelle stringhe usare Write o Edit, non `cat <<'EOF'`. È la trappola
  che scatta più spesso.
- **`git checkout --` scarta, non mette da parte.** Copiare i file altrove
  *prima*: cancella senza chiedere e senza reflog.
- **Una rete di sicurezza è codice nuovo**, cioè dove i bug sono più probabili.
- **Un parametro il cui costo scende sempre non è da ottimizzare**
  (`keep_recent_messages`).
- **Il simulatore è una copia, e una copia più permissiva nasconde.** Conta i
  token dalla lunghezza del testo — dice *a quale tariffa* si paga, non
  *quanti* token serve una parola — e accettava cinque breakpoint dove l'API ne
  vuole quattro, rendendo vuoti i test che doveva sostenere.
- **Aggiungere uno scenario invalida i confronti storici** (`CORPUS_VERSION`).

Il dettaglio di ciascuna sta nel README, sezione «Trappole».

## Comandi

`serve` `stats` `purge` `diagnosi` `assunzioni` `verifica` `consiglia` ·
misure: `bench`
`ablate` `optimize` `compaction` `prompt` `substitutions` `cachekey` `overhead`
`pruning` `ritenzione` `memoria` `ceiling` `cachewrites` `streaming` · pagine:
`/impostazioni` `/quadro` `/` `/admin/dashboard`

Test: `.venv/Scripts/python.exe -m pytest -q` — devono passare tutti, e non
devono mai richiedere rete. Ogni comando viene eseguito, non solo il suo
`--help`; i dodici di misura sono marcati `lento` e si tolgono dal ciclo veloce
con `-m "not lento"`.
