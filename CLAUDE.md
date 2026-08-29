# EcoTokens — istruzioni di lavoro

Gateway locale per **Claude** che riduce la spesa in token. Un solo provider
(Anthropic), due porte in ingresso: `/v1/chat/completions` in dialetto OpenAI e
`/v1/messages` in dialetto nativo. "OpenAI-compatibile" descrive la forma della
porta, non la destinazione — è un malinteso facile, evitarlo nei testi.
Codice e commenti in italiano, senza accenti nelle stringhe di codice (`e'`,
non `è`); il testo per l'utente li usa normalmente.

Questo file è caricato in ogni sessione: **tenerlo corto è parte del punto**.
Se cresce oltre le ~100 righe, qualcosa va spostato nel README.

## La regola che governa tutto il progetto

**Non si dichiara un risparmio: si misura.** Quasi metà delle voci del
[registro delle correzioni](ecotokens/tuning_log.py) sono difetti del *metro*,
non del prodotto — misure che davano risposte plausibili e sbagliate. Tre volte
il gateway è stato dichiarato dannoso o inutile da uno strumento rotto.
L'ultima volta è stato un conto che accreditava due volte la stessa rilettura,
e a scoprirlo è stato un test scritto sul comportamento atteso.

Prima di accendere uno stadio, di cambiare un default o di scrivere in un
README che qualcosa risparmia: `ecotokens bench`, `ablate`, o una misura
mirata. Se non è misurabile, si dice che non lo è.

## Le cose che abbiamo scoperto misurando, e che valgono anche qui

Queste non sono teoria: sono i vincoli che governano il costo di *questa*
sessione, non solo del gateway.

1. **Il prefisso è tutto.** Il prompt caching è un match di prefisso: un byte
   diverso all'inizio invalida tutto ciò che segue, e vale il 67,8% del
   risparmio misurato. In pratica, in sessione: non rileggere file già letti,
   non rifare la stessa ricerca con parole diverse, non cambiare modello a
   metà. **Ma quel 67,8% non è del gateway**: Anthropic lo dà a chiunque con
   un `cache_control` in cima alla richiesta. Il pianificatore di EcoTokens ne
   aggiunge 0,7 — e quello 0,7 è la media fra un −0,2% sulle conversazioni
   singole e un +19,9% quando più richieste diverse condividono un prefisso.
   La lezione di metodo conta più del numero: *un riferimento invecchia*.
   Finché non lo si aggiorna, si misura quanto costava non usare una funzione
   che nel frattempo è diventata gratis.
2. **Ogni turno rispedisce tutto.** Il costo di un ciclo agentico cresce con il
   *numero di turni*, non solo con la loro dimensione. Chiamate indipendenti
   vanno fatte **nello stesso messaggio**: due Bash in parallelo costano un
   round-trip, in sequenza ne costano due, e il secondo rispedisce anche il
   primo. È la leva più grossa di una sessione di lavoro.
3. **Leggere mirato.** `Read` con `offset`/`limit`, `Grep` con `-A`/`-B`, invece
   di versare un file intero nel contesto. Un file da 800 righe letto per
   controllare una funzione costa quanto quella funzione per duecento.
4. **Accorciare il prompt rende un quarto.** Misurato: togliere mille token
   rende ~$0,0014 contro $0,0050 di prezzo pieno, perché la cache li aveva già
   scontati. Non vale la pena contorcersi per essere brevi; vale la pena non
   rileggere.
5. **Comprimere e mettere in cache tirano in direzioni opposte.** Qualunque
   riscrittura di ciò che sta all'inizio del contesto costa più di quanto fa
   risparmiare, a meno che non sia **deterministica e stabile**.
6. **Due decisioni giuste possono rompersi a vicenda.** Fatti di memoria
   telegrafici (si pagano sempre) più ricerca lessicale (gratuita): insieme,
   zero fatti trovati su tre, perché accorciandoli si tolgono le parole su cui
   il match si regge. Cambiando due cose, misurare la coppia.
7. **L'aritmetica a tavolino convince in fretta di cose false.** Un conto su
   carta dava +21% a spostare i fatti nel prefisso; misurato, −0,4%. Il conto
   era giusto, sbagliate le grandezze dentro. Una stima non sostituisce
   l'esecuzione — vale anche qui.

## Il circolo fra sessione e prodotto

Le due direzioni vanno tenute entrambe aperte, ed è il motivo per cui questo
file esiste.

**Sessione → EcoTokens.** Una pratica di lavoro che qui si rivela utile e si può
automatizzare diventa uno stadio o un default (la normalizzazione prima della
chiave di cache è nata così).

**Contare prima di progettare.** Prima di raffinare uno stadio, misurare quante
volte interviene: l'effort adattivo sembrava un'euristica da migliorare, e
invece un veto lo spegneva sul 45% del traffico. `ecotokens ritenzione` fa la
domanda gemella — *ciò che serviva è arrivato al prompt?* — e la prima risposta
è stata **zero fatti su tutti** con la potatura accesa.

**EcoTokens → sessione.** Quando una misura smentisce un'intuizione, la regola
cambia *anche qui sopra*. Il registro delle correzioni è la fonte.

Protocollo, quando si trova qualcosa:

1. Misurarlo — un confronto A/B, non un'impressione.
2. Se regge: implementarlo nel gateway *e* aggiornare questo file.
3. Aggiungere una voce a `ecotokens/tuning_log.py`, distinguendo se il difetto
   era del **metro** o del **gateway**. La distinzione conta: correggere il
   metro non migliora il prodotto, rende visibile com'era già.
4. Se la misura non è possibile con gli strumenti disponibili, dirlo e
   lasciare la funzionalità **spenta** (vedi `prompt.only_verified`).

## Trappole già calpestate — non ricalpestarle

- **Il primo giro non è rappresentativo del secondo.** Due volte in un'ora ho
  confrontato una serie fredda con una calda e concluso il contrario del vero.
  Scaldare prima di misurare; per gli import, un processo nuovo.
- **Gli heredoc di Git Bash mangiano i backslash.** Per file con `
` dentro le
  stringhe usare Write o Edit, non `cat <<'EOF'`. È la trappola che scatta più
  spesso, e costa un errore di sintassi ogni volta.
- **`git checkout --` scarta, non mette da parte.** Per isolare un commit,
  copiare i file altrove *prima*: un `checkout` su file modificati e non messi
  in stage cancella il lavoro senza chiedere e senza reflog.
- **Un parametro il cui costo scende sempre non è da ottimizzare**
  (`keep_recent_messages`).
- **Il simulatore conta i token dalla lunghezza del testo**: va bene per sapere
  *a quale tariffa* si paga, non *quanti* token serva una parola.
- **Aggiungere uno scenario invalida i confronti storici** (`CORPUS_VERSION`).

Il dettaglio di ciascuna sta nel README, sezione «Trappole».

## Comandi

`serve` `stats` `purge` · misure: `bench` `ablate` `optimize` `compaction`
`prompt` `substitutions` `cachekey` `overhead` `pruning` `ritenzione` `memoria`
`ceiling` `cachewrites` `streaming` · pagine: `/impostazioni` `/quadro` `/`
`/admin/dashboard`

Test: `.venv/Scripts/python.exe -m pytest -q` — devono passare tutti, e non
devono mai richiedere rete.
