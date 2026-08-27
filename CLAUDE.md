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
   diverso all'inizio invalida tutto ciò che segue. Vale l'88% del risparmio
   misurato. In pratica, in sessione: non rileggere file già letti, non
   rifare la stessa ricerca con parole diverse, non cambiare modello a metà.
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

## Il circolo fra sessione e prodotto

Le due direzioni vanno tenute entrambe aperte, ed è il motivo per cui questo
file esiste.

**Sessione → EcoTokens.** Quando una pratica di lavoro qui si rivela utile e si
può automatizzare, diventa uno stadio o un default del gateway. Esempi già
percorsi: la normalizzazione del testo prima della chiave di cache è nata
notando che due richieste identiche a meno di uno spazio non si riconoscevano.

**Contare prima di progettare.** Prima di raffinare uno stadio, misurare quante
volte interviene davvero. L'effort adattivo sembrava un'euristica da migliorare;
contando le valutazioni si è visto che il problema non era la qualità della
regola ma un veto in blocco che la spegneva sul 45% del traffico. La domanda
vale per ogni stadio: *quante volte ha fatto qualcosa?*

**EcoTokens → sessione.** Quando una misura del banco smentisce un'intuizione,
la regola cambia *anche qui sopra*, in questo file. Il registro delle
correzioni è la fonte: se una voce nuova cambia il modo di lavorare, va
riportata nell'elenco qui sopra.

Protocollo, quando si trova qualcosa:

1. Misurarlo — un confronto A/B, non un'impressione.
2. Se regge: implementarlo nel gateway *e* aggiornare questo file.
3. Aggiungere una voce a `ecotokens/tuning_log.py`, distinguendo se il difetto
   era del **metro** o del **gateway**. La distinzione conta: correggere il
   metro non migliora il prodotto, rende visibile com'era già.
4. Se la misura non è possibile con gli strumenti disponibili, dirlo e
   lasciare la funzionalità **spenta** (vedi `prompt.only_verified`).

## Trappole già calpestate — non ricalpestarle

- **Un parametro il cui costo scende sempre non è da ottimizzare.**
  `keep_recent_messages` costa meno più lo si abbassa, ma ciò che si perde — la
  qualità della risposta — il banco non lo misura. È un giudizio, non un ottimo.
- **Il simulatore conta i token dalla lunghezza del testo.** Va bene per
  chiedersi *a quale tariffa* un token viene fatturato, non *quanti* token
  serva una parola. Qualunque misura di accorciamento lessicale fatta lì si
  autoconferma.
- **Gli heredoc di Git Bash mangiano i backslash.** Per file con `\n` dentro le
  stringhe usare Write o Edit, non `cat <<'EOF'`.
- **Aggiungere uno scenario invalida i confronti storici.** Il corpus è
  versionato (`CORPUS_VERSION` in `bench.py`): cambiarlo azzera la sezione dei
  progressi nella dashboard. Farlo di rado e di proposito.

## Comandi

`serve` `stats` `purge` · misure: `bench` `ablate` `optimize` `compaction`
`prompt` `substitutions` `cachekey` `overhead` `pruning` `dashboard`

Test: `.venv/Scripts/python.exe -m pytest -q` — devono passare tutti, e non
devono mai richiedere rete.
