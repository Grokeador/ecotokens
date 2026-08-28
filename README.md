# EcoTokens

Gateway locale che si mette tra le tue applicazioni e l'API Anthropic, e riduce
la spesa in token.

**Parla solo con Claude.** Non c'è nessun provider OpenAI nel progetto: tutto
quello che fa — prompt caching a match di prefisso, `output_config.effort`,
`context_management` — esiste solo sull'API Anthropic.

Accetta però **due dialetti in ingresso**, perché il valore sta nel non dover
riscrivere le applicazioni:

| Porta | Per chi |
|---|---|
| `POST /v1/chat/completions` | applicazioni che parlano il protocollo OpenAI: si cambia `base_url` e basta |
| `POST /v1/messages` | client che parlano già il dialetto nativo di Claude |
| `POST /v1/messages/count_tokens` | preventivare il costo di una richiesta senza generarla |

```
la tua app  ──►  EcoTokens  ──►  API Anthropic  ──►  Claude
             (due porte)      (l'unico provider)
```

"Compatibile OpenAI" descrive la forma della porta, non la destinazione.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="non-serve")
client.chat.completions.create(
    model="claude-opus-5",
    messages=[{"role": "user", "content": "Ciao"}],
)
```

Licenza MIT, tutto self-hosted, nessun servizio a pagamento oltre all'API
Anthropic che useresti comunque.

## Cosa fa davvero

Il gateway non si limita a inoltrare le richieste: le riscrive prima di
mandarle. Le percentuali qui sotto non sono stime: vengono dal banco di
misura incluso nel progetto, che esegue lo stesso carico con e senza gli
stadi di ottimizzazione (vedi [Misurare, invece di credere](#misurare-invece-di-credere)).

| Tecnica | Risparmio | Rischio |
|---|---|---|
| **Prompt caching automatico** | fino al 90% sui token di prefisso riletti | nessuno |
| **Effort adattivo** | 3,5% del risparmio; fino all'11,4% accettando un rischio sui turni con tool | nessuno al default |
| **Potatura del contesto** | 1,2% del risparmio; **+7,8%** sul carico agentico lento | perde i risultati di tool vecchi |
| **Compattazione con riassunto** | −10% se il taglio avanza a scatti; **+40%** di costo se insegue la conversazione | perdita di dettaglio |
| **Riscrittura del prompt** | −11% su prompt scritti in modo prolisso, 0,2% sul corpus completo | cambia il testo, non il senso |
| **Cache esatta** | richieste identiche servite a costo zero; **−56%** quando differiscono solo per spaziatura | nessuno |
| **Cache semantica** *(spenta)* | richieste simili servite a costo zero | può restituire risposte sbagliate |
| **Declassamento di modello** *(spento)* | modello meno costoso sulle richieste semplici | azzera la cache, vedi sotto |

Le due tecniche che possono cambiare il *contenuto* di una risposta sono
disattivate di default. Accenderle è una scelta, non un default.

## Tre vincoli che spiegano il progetto

Sono la ragione per cui il gateway è fatto così e non in modo più ingenuo.

**Il prompt caching è un match di prefisso.** L'ordine di render è
`tools` → `system` → `messages`, e un solo byte diverso nel prefisso invalida
tutto ciò che segue. Per questo il gateway ordina i tool per nome, congela il
`system`, e inietta memoria e istruzioni dinamiche *in coda*, mai in testa.

**Una scrittura in cache costa più di una lettura.** 1.25× con TTL 5 minuti,
2× con TTL un'ora, contro 0.1× per una rilettura. Il pareggio è a 2 richieste
(o 3 con il TTL lungo): per questo il primo turno di una conversazione non
scrive mai in cache, e il TTL lungo si adotta solo per sessioni con pause vere.

**Le cache sono legate al modello.** Cambiare modello a metà conversazione
azzera il prompt caching accumulato, e su una conversazione lunga la cache
persa costa più del modello economico. Per questo il declassamento è spento di
default e, quando è acceso, il modello si sceglie una volta per sessione e non
cambia più. Il risparmio sicuro si ottiene abbassando l'`effort`, che non
tocca il prefisso.

## Installazione

Serve Python 3.11 o superiore.

```bash
python -m venv .venv && .venv/Scripts/activate && pip install -e .
```

Su Linux o macOS l'attivazione è `source .venv/bin/activate`.

Poi servono le credenziali Anthropic. Il gateway non le chiede: le risolve
l'SDK ufficiale, nell'ordine `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, un
profilo creato con `ant auth login`.

```bash
set ANTHROPIC_API_KEY=sk-ant-...
```

## Avvio

```bash
ecotokens serve
```

Il gateway ascolta su `http://127.0.0.1:8000/v1`. Per configurarlo:

```bash
copy ecotokens.example.toml ecotokens.toml
```

Ogni valore si può sovrascrivere da ambiente con
`ECOTOKENS_<SEZIONE>__<CAMPO>`, per esempio `ECOTOKENS_SERVER__PORT=9000`.

## Vedere quanto si risparmia

```bash
ecotokens stats
```

Mostra token consumati, quota di prompt servita dalla cache, costo effettivo,
costo che la stessa attività avrebbe avuto senza ottimizzazioni, e la
differenza tra i due.

Un dettaglio che vale la pena conoscere: nell'API Anthropic `input_tokens` è
solo il residuo **non** servito dalla cache. La dimensione reale del prompt è
la somma dei tre contatori di input. EcoTokens riporta sempre il totale, e nel
campo `usage.prompt_tokens_details.cached_tokens` di ogni risposta trovi
quanti di quei token sono arrivati dalla cache.

Le stesse informazioni via HTTP:

```bash
curl http://localhost:8000/admin/stats
```

Ogni risposta porta anche un campo `ecotokens` con l'origine (`api`,
`exact_cache`, `semantic_cache`), il costo, il risparmio e le decisioni prese
dalla pipeline. I client OpenAI lo ignorano senza problemi.

## Misurare, invece di credere

Il gateway include un banco di misura. Esegue lo **stesso identico carico** due
volte, sulla stessa strada percorsa dalle richieste vere, cambiando una cosa
sola: gli stadi di ottimizzazione accesi o spenti.

```bash
ecotokens bench
```

Su quattro carichi diversi, uno dei quali ricostruito dai file veri di questo
repository:

| Carico | Senza gateway | Con gateway | Risparmio | Prompt da cache |
|---|---:|---:|---:|---:|
| chat, 8 turni con system prompt grande | $0,4359 | $0,0257 | **94%** | 86% |
| ciclo agentico, 6 turni da 6 tool | $0,6249 | $0,0521 | **92%** | 72% |
| domande frequenti ripetute | $0,3779 | $0,0180 | **95%** | — |
| prompt verbosi, 8 turni | $0,3338 | $0,0290 | **91%** | 54% |
| costruzione di EcoTokens | $5,2262 | $0,2048 | **96%** | 82% |
| **totale** | **$6,9987** | **$0,3294** | **95%** | 79% |

Sono i numeri del profilo **aggressivo**, quello predefinito. Con il profilo
`prudente` — che non tocca mai il contenuto di una risposta — il totale è
**75,2%**. La differenza e cosa la produce stanno nella sezione [I due
profili](#i-due-profili-e-cosa-distingue-davvero-unottimizzazione).

Lo scenario `costruzione` non è inventato: legge i sorgenti veri del progetto e
ricostruisce il traffico che un agente di codice produce scrivendolo.

> **Il metro cresce con ciò che misura.** Quei sorgenti vengono letti *al momento
> dell'esecuzione*, quindi ogni commit che allunga il codice cambia anche il
> carico. Dentro una singola esecuzione il corpus è costante e i confronti fra
> varianti reggono — sono quelli **fra esecuzioni diverse** a essere contaminati.
> Misurato: fra due ablazioni distanti poche ore il riferimento è passato da
> $6,3002 a $6,6338, un +5,3% che nessuna modifica al gateway aveva prodotto.
> `CORPUS_VERSION` non se ne accorge, perché l'elenco degli scenari non cambia:
> cambia il loro contenuto.

## I due profili, e cosa distingue davvero un'ottimizzazione

Il gateway esce configurato sul profilo **aggressivo**, che risparmia il 95,3%.
Prima di lasciarlo così vale la pena sapere cosa lo separa dall'altro, perché
non è una differenza di grado.

| | `prudente` | `aggressivo` *(predefinito)* |
|---|---|---|
| Risparmio misurato | 75,2% | **95,3%** |
| Modello | quello chiesto dal client | il più economico, fissato a inizio sessione |
| Effort | abbassato dove il router giudica sicuro | sempre al minimo |
| Contenuto delle risposte | mai toccato | **cambia** |

I venti punti di differenza vengono quasi tutti da una riga sola
dell'ablazione: il cambio di modello vale il **17,5%**, più di tutti gli altri
stadi messi insieme tranne il prompt caching.

Ed è qui che serve una distinzione che il resto di questo README dà per
scontata. I primi 75 punti sono **ottimizzazioni**: la risposta che ricevi è la
stessa che avresti ricevuto senza il gateway, solo pagata meno. Gli ultimi venti
non lo sono. Sono un'altra risposta a un prezzo diverso.

Il banco misura quanto è *lunga* una risposta, non se è *giusta*. Quindi quel
17,5% è interamente misurato e il suo costo interamente no — e nessuna misura
futura potrà cambiarlo, perché la qualità non è una grandezza che questo
strumento sappia leggere.

```toml
profilo = "prudente"   # per tornare a non toccare mai il contenuto
```

Il profilo imposta dei **default**: qualunque campo scritto a mano nel file di
configurazione vince su di esso. Chi scrive `model_downgrade = false` sotto
`[router]` ottiene quello, profilo aggressivo o no.

#### Cosa il profilo aggressivo continua a non fare

Tre garanzie sopravvivono a entrambi i profili, e sono coperte da test:

- **Un `reasoning_effort` chiesto esplicitamente dal client non viene mai
  toccato.** Decidere al posto di chi non ha deciso è un'ottimizzazione;
  sovrascrivere chi ha deciso è ignorare un'istruzione.
- **Il modello si sceglie una volta per sessione e non cambia più.** Cambiarlo
  a metà butterebbe via tutta la cache accumulata, che è legata al modello.
- **Il declassamento lascia traccia.** Haiku 4.5 richiede 4096 token di
  prefisso contro i 512 di Opus 5: fra le due soglie la cache non si forma e
  l'API non lo segnala. Le note della richiesta lo dicono.

Su quest'ultimo punto una misura vale più di un avvertimento. Con un system
prompt di 100 o 300 parole il profilo aggressivo perde **del tutto** la cache —
e resta comunque più economico, perché il modello costa cinque volte meno su
input *e* output e la compensa. Il rischio c'è ma non si è materializzato sui
carichi provati.

#### La porta nativa

I client che parlano già il dialetto di Claude non hanno bisogno di nessuna
traduzione: passando dalla porta OpenAI si farebbero tradurre due volte per
tornare al punto di partenza.

```bash
curl http://localhost:8000/v1/messages -H "Content-Type: application/json"   -d '{"model":"claude-opus-5","messages":[{"role":"user","content":"ciao"}],"max_tokens":1024}'
```

La risposta esce in formato Anthropic — `type`, `content`, `stop_reason` — non
in formato OpenAI. Lo streaming ritrasmette gli eventi dell'API così come
arrivano (`message_start`, `content_block_delta`, `message_stop`).

Qui il gateway fa *meno* lavoro, non di più: la pipeline lavora già in formato
Anthropic, quindi non c'è traduzione né all'andata né al ritorno. Restano tre
cose, le stesse dell'altra porta: risolvere l'alias del modello, togliere i
parametri di campionamento che i modelli attuali rifiutano con un 400, e
applicare i valori predefiniti dove il client tace.

**Le due porte condividono la cache.** La chiave si calcola sui parametri
Anthropic, dopo la traduzione, e riduce il contenuto a una forma canonica: una
stringa e un blocco di testo singolo sono la stessa cosa per l'API, quindi la
stessa domanda posta nei due dialetti costa una volta sola. Anche la risposta
viene salvata in formato Anthropic, e tradotta in uscita solo per chi ha chiesto
in dialetto OpenAI.

Il corpo non viene validato campo per campo di proposito: riprodurre lo schema
Anthropic qui dentro significherebbe mantenerne una copia che invecchia, e
rifiutare parametri che l'API magari accetta già.

#### Preventivare senza generare

```bash
curl http://localhost:8000/v1/messages/count_tokens -H "Content-Type: application/json"   -d '{"model":"claude-opus-5","messages":[{"role":"user","content":"ciao"}]}'
```

Un client nativo che vuole sapere quanto costerà una richiesta chiama questo
endpoint. Conta la richiesta **come è arrivata**, dopo la sola sanificazione —
non dopo gli stadi che riscrivono il prompt, perché quelli hanno effetti
collaterali che un preventivo non deve produrre: creerebbero sessioni,
scriverebbero riassunti, chiamerebbero modelli. Il numero è quindi il costo del
prompt *che hai scritto*; quello che il gateway manda davvero è di solito più
corto, ed è il punto.

La risposta porta anche la stima locale accanto al conteggio vero:

```json
{"input_tokens": 1234,
 "ecotokens": {"estimated_input_tokens": 1301, "estimate_error_ratio": 0.0543}}
```

Non serve al chiamante, serve al progetto. Tutto quello che c'è in questo README
poggia su uno stimatore euristico mai tarato contro il tokenizer reale, e ogni
chiamata a questo endpoint è un punto di taratura gratuito.

Gli scarti finiscono in tabella e si leggono da `ecotokens stats`,
`/admin/stats` e dalla sezione **Quanto vale il metro** della dashboard,
aggregati per modello:

| Modello | Campioni | Scarto medio | Intervallo |
|---|---:|---:|---:|
| claude-opus-5 | 41 | +4,2% | +1,1% … +7,8% |

Lo scarto medio da solo ingannerebbe. Una stima che sbaglia del +5% *sempre* è
utilizzabile — si corregge. Una che oscilla fra −30% e +40% ha media zero e non
lo è, e la media la farebbe sembrare perfetta: per questo accanto c'è
l'intervallo.

*(La riga qui sopra è un esempio di forma, non una misura: senza credenziali il
campione è vuoto.)*

*Non provato:* in teoria un client nativo configurabile via `ANTHROPIC_BASE_URL`
potrebbe passare dal gateway. Non l'ho verificato con nessuno in particolare.

### Calibrare contro l'API vera

Tutti i numeri di questo README vengono dal simulatore. Per sapere quanto è
largo lo scarto con la realtà senza spendere una fortuna, conviene partire da un
solo scenario:

```bash
ecotokens bench --live --scenario chat
```

Il comando dice quante richieste sta per fare e chiede conferma prima di
spendere. La misura finisce in un corpus separato, così un sottoinsieme non
viene confrontato con la serie completa — cambierebbe il denominatore di tutte
le percentuali. Servono credenziali: senza, il comando si ferma prima di
spendere e spiega come impostarle.

I numeri vengono dal simulatore incluso nel pacchetto. La meccanica della cache
è fedele — match di prefisso, finestra di lookback di venti blocchi, marker
fuori dall'impronta — ma i conteggi di token sono proporzionali alla dimensione
del testo, non prodotti dal tokenizer vero. `ecotokens bench --live` rifà la
stessa misura contro l'API reale, e consuma token veri.

### Quanto vale ogni stadio

```bash
ecotokens ablate
```

Gli stadi si accendono uno alla volta: la differenza fra un gradino e il
precedente è il contributo di quello stadio.

| Stadio | Contributo |
|---|---:|
| prompt caching | **66,6%** |
| effort adattivo | 3,4% |
| cache esatta | 2,2% |
| potatura del contesto | 1,2% |
| riscrittura del prompt | 0,2% |

### Potare senza distruggere la cache

Potare i vecchi risultati dei tool toglie token dal prompt. Per molto tempo qui
è costato più di quanto rendesse, e lo stadio valeva **0%**: la spiegazione
accettata era che potare e mettere in cache siano incompatibili.

Non lo sono. L'edit `clear_tool_uses_20250919` accetta un parametro `keep` che
il gateway non usava affatto, lasciandolo al valore predefinito del server. Con
`keep` fisso il confine sta sempre a N risultati dal fondo, quindi **scorre di
un risultato a ogni turno**: l'insieme dei blocchi svuotati è diverso a ogni
richiesta, il prefisso è nuovo per costruzione, e la cache non trova mai niente.

Ora il gateway sceglie quanti risultati potare *dall'inizio*, a scatti, e da
quello ricava `keep`. Fra uno scatto e l'altro vengono svuotati esattamente gli
stessi blocchi.

| Carico di costruzione | Costo | Da cache |
|---|---:|---:|
| nessuna potatura | $1,0936 | 90% |
| confine mobile (com'era) | $1,4898 | 21% |
| **a scatti** | **$1,0087** | **83%** |

Da −36,2% a **+7,8%**.

#### Lo scatto si misura in turni, non in risultati

Contato in risultati, i due carichi agentici volevano valori opposti. Non per
quanto pesano i tool result — la quota è identica, 92% contro 93% — ma per la
*velocità*: sei chiamate per turno consumano uno scatto sei volte più in fretta
di una. Lo stesso numero dava otto turni di stabilità su un carico e nemmeno due
sull'altro, cioè il confine tornava a inseguire.

Espresso in turni e convertito col ritmo osservato della conversazione, il
confine si muove circa una volta ogni N turni su entrambi, ed entrambi
risparmiano.

#### Due soglie, perché sono due domande

`trigger_ratio` è una frazione della finestra del modello e risponde a *sono in
pericolo di sforare*. Non risponde a *conviene potare*, che dipende da quanto
materiale vecchio c'è — e le finestre vanno da 200k a un milione di token,
quindi la stessa frazione significa cose molto diverse.

Misurando la soglia sul materiale potabile è emersa una zona non monotona:

| Materiale minimo | Costo totale |
|---|---:|
| mai (potatura spenta) | $1,6858 |
| 50.000 token | **$1,7534** |
| 30.000 token | $1,6209 |
| **20.000 token** | **$1,6146** |
| 10.000 token | $1,6151 |

A 50.000 potare costa **più che non potare affatto**: comincia troppo tardi e
sposta il prefisso proprio quando la cache valeva di più.

Va detto chiaramente: sotto quella soglia si scambiano soldi misurati contro
fedeltà che il banco non misura. Quel contesto prima restava integrale, ora i
risultati di tool vecchi diventano un segnaposto. È `prune_min_prunable_tokens`,
e alzarlo lo disattiva.

### Le scritture in cache che nessuno rilegge

L'ablazione dice che il prompt caching vale il **67%** del risparmio e che gli
altri quattro stadi messi insieme valgono il **7%**. Da un certo punto in poi
limare gli stadi piccoli significa contendersi un settimo di quello che vale il
primo, e l'unica domanda che sposta qualcosa è se dentro quel 67% ci sia dello
sprecato.

Una scrittura in cache costa **1,25×** (cinque minuti) o **2×** (un'ora); una
rilettura costa **0,1×**. Riletta anche una sola volta è già in guadagno. Mai
riletta è una perdita netta pari al 25% del suo prezzo pieno: si è pagato di più
per non avere niente in cambio. Il pianificatore piazza fino a quattro
breakpoint per richiesta, e per molto tempo nessuno ha contato quanti rendano.

```bash
ecotokens cachewrites
```

| Tetto | Costo | Scritti | Ripagati | Orfani in mezzo | Orfani di coda |
|---|---:|---:|---:|---:|---:|
| pianificatore spento | $4,2992 | 0 | 0 | 0 | 0 |
| 1 breakpoint | $3,8593 | 15.668 | 12.436 | 0 | 3.232 |
| **2 breakpoint** | **$1,6686** | 142.077 | 129.221 | 4.509 | 8.347 |
| 3 breakpoint | $1,6686 | 142.077 | 129.221 | 4.509 | 8.347 |
| 4 breakpoint | $1,6686 | 142.077 | 129.221 | 4.509 | 8.347 |

Le due colonne vanno lette insieme, e in quest'ordine: prima il costo, poi lo
spreco. Lo spreco da solo si minimizza spegnendo il pianificatore, che è la riga
più cara della tabella.

#### Come si attribuisce una rilettura a una scrittura

L'API non dice *quale* voce di cache ha letto. La ricostruzione poggia sul fatto
che la cache è un match di prefisso, quindi le letture crescono da sinistra: se
una richiesta successiva della stessa sessione legge più a fondo, la differenza
può venire solo da ciò che si era scritto prima. Si guarda avanti dalla
scrittura e ci si ferma alla prima di due cose: una lettura che la supera —
ripagata — oppure una nuova scrittura che riparte da un punto a monte, e allora
la precedente non è più raggiungibile da nessuno.

La prima versione di questa regola prendeva semplicemente la lettura più
profonda fra tutte le successive, e accreditava due volte la stessa rilettura:
lo spreco misurato risultava del 9,0% invece del 20,0%. L'ha trovata un test, ed
è la decima voce del registro classificata come difetto del **metro**.

#### Perché gli orfani "di coda" restano separati

L'ultima scrittura di una sessione non ha, per definizione, una richiesta dopo
di sé. Non è un difetto del pianificatore: è il prezzo di non sapere in anticipo
che la conversazione finiva lì. Può inoltre essere riletta da un'altra sessione
che condivide il prefisso di sistema, cosa che questo conto — che guarda una
sessione alla volta — non vedrebbe. Sommarla agli orfani "in mezzo" darebbe un
numero più grosso e meno utile: si agirebbe su una quota che in parte non
dipende da nessuna decisione del gateway.

#### Cosa ha trovato

**Il pianificatore, da solo, non lascia orfana nessuna scrittura a metà
sessione.** Tutte le scritture orfane vengono dalla potatura: ogni volta che il
confine avanza il prefisso cambia, e ciò che si era appena pagato 1,25× per
scrivere diventa irraggiungibile.

| `prune_step_turns` | Costo | Orfani in mezzo |
|---|---:|---:|
| solo pianificatore, niente potatura | $2,1258 | **0** |
| 4 (il vecchio default) | $1,6879 | 16.999 |
| 5 | $1,6646 | 12.823 |
| **7** | **$1,6686** | **4.509** |
| 8 | $1,6831 | 4.434 |

Il default passa da 4 a **7**: −73% di scritture orfane e −1,1% di costo. Il
vecchio valore era dominato su entrambi gli assi, quindi non c'è stato niente da
bilanciare.

Fra 5 e 8 il costo oscilla dell'1% senza andamento — 5 basso, 6 alto, 7 basso, 8
alto. Sono effetti discreti del confine, non una tendenza, e leggerli come tale
sarebbe adattarsi al corpus; dentro quel tratto si sceglie perciò il valore che
lascia meno orfani.

#### Una cosa che il corpus non misura

Le righe a 2, 3 e 4 breakpoint danno numeri identici fino all'ultima cifra,
perché **il gateway non arriva mai a usarne più di due**. I breakpoint intermedi
si attivano solo quando la coda della conversazione supera i 20 blocchi di
lookback: contati, sono 43 chiamate e zero marker piazzati, e la coda più lunga
che il corpus produce ne ha 13.

Non è stato tolto niente — il limite dei 20 blocchi è documentato, e un client
agentico con dieci chiamate parallele per turno lo supera. Ma quello stadio
resta **non misurato**, e vale la pena dirlo invece di lasciarlo intendere.
Servirebbe uno scenario apposta, che però cambierebbe `CORPUS_VERSION` e
azzererebbe i confronti storici.

#### Sul traffico vero

Lo stesso conto gira sui dati del ledger, non solo sul simulatore: `ecotokens
stats` e la dashboard lo mostrano appena c'è traffico registrato. È per questo
che `usage_events` porta ora una colonna `cache_ttl` — senza sapere con quale
TTL una scrittura è stata pagata, il costo di una scrittura a un'ora verrebbe
calcolato come se fosse a cinque minuti, cioè un quarto del vero.

### Fin dove si può arrivare, e cosa lo ferma

Il 74,9% in testa invita a una domanda sola: perché non di più? La risposta è
aritmetica, e conviene darla insieme al numero.

```bash
ecotokens ceiling --goal 99
```

Le leve non sono tutte della stessa natura. Le prime non costano niente che non
sia già misurato; le ultime scambiano denaro contro **qualità**, e la qualità
questo banco non la misura — sa quanto è lunga una risposta, non se è giusta.

| Leva | Costo | Risparmio | In cambio di |
|---|---:|---:|---|
| profilo `prudente` | $1,6716 | **75,2%** | niente che non sia già misurato |
| + effort minimo sui tool | $1,5725 | 76,4% | la correttezza delle chiamate agli strumenti |
| + effort sempre basso | $1,4370 | 77,8% | la profondità di ragionamento su tutto |
| + modello economico *(= profilo `aggressivo`)* | $0,3294 | **95,3%** | non è più la stessa risposta |
| + modalità batch | $0,1647 | **97,6%** | l'immediatezza: esito entro 24 ore, non dietro un'interfaccia |

#### Il pavimento

Sotto una certa cifra non si scende, perché il modello deve pur rispondere.

| Voce | Costo | Perché resta |
|---|---:|---|
| output generato | $0,0438 | nessuna cache lo sconta: non esisteva prima della richiesta |
| input mai visto | $0,0852 | contenuto nuovo, va trasmesso almeno una volta |
| riletture | $0,0339 | già scontate a 0,1×, ma non gratuite |
| **totale** | **$0,1629** | massimo teorico: **97,6%** |

Il conto è deliberatamente generoso, e conviene dire quanto: valuta l'input nuovo
a prezzo pieno invece che a 1,25×, lo fa al modello più economico del listino, e
applica anche lo sconto del 50% della **Message Batches API** — l'unico meccanismo
di sconto che il gateway non usa, perché rende le richieste asincrone. È un limite
che nessuna configurazione può battere, non una stima realistica.

**Ne segue che il 99% su questo corpus è impossibile.** Richiederebbe di stare
sotto $0,0666; il pavimento è $0,1629, due volte e mezzo tanto. Non è un obiettivo
difficile: è un obiettivo che l'aritmetica esclude, e averlo verificato dopo aver
sommato *ogni* sconto documentato è ciò che rende la conclusione una dimostrazione
invece che una resa.

#### Il risparmio dipende dal traffico, non dal gateway

Detto questo, il 99% esiste. Non è una proprietà del gateway ma del **carico**:
su richieste tutte diverse l'unica leva è il prefisso condiviso, su richieste che
si ripetono entra la cache esatta — e quella non sconta il prezzo di un token, lo
azzera.

| Carico | Richieste | Senza | Con | Risparmio |
|---|---:|---:|---:|---:|
| 12 domande ×1 | 4 | $0,1260 | $0,0509 | 59,6% |
| 6 domande ×2 | 8 | $0,2519 | $0,0509 | 79,8% |
| 4 domande ×3 | 12 | $0,3779 | $0,0509 | 86,5% |
| 3 domande ×5 | 15 | $0,4723 | $0,0428 | 90,9% |
| 2 domande ×10 | 20 | $0,6297 | $0,0347 | 94,5% |
| 1 domanda ×20 | 20 | $0,6296 | $0,0266 | 95,8% |
| 1 domanda ×50 | 50 | $1,5740 | $0,0266 | **98,3%** |

Su richieste tutte uguali il 99% arriva a circa **85 ripetizioni**, il 99,9% a
circa 850. La prima richiesta si paga sempre, quindi la curva sale verso il 100%
senza toccarlo.

Se il tuo traffico assomiglia alle ultime righe — FAQ, classificazioni, estrazioni
ripetute su testi simili — il numero che vedrai sarà molto più alto di quello del
corpus standard. Se assomiglia a `costruzione`, sarà più basso. Il numero unico non
esiste, ed è il motivo per cui questa tabella sta nel README accanto a quello di
testa.

> **Una tentazione da nominare.** Il numero in testa si alza in dieci minuti
> aggiungendo scenari ripetitivi al corpus. Salirebbe davvero, e non
> misurerebbe più niente: è la stessa classe di errore che occupa quasi metà del
> [registro delle correzioni](ecotokens/tuning_log.py), commessa però di
> proposito. Il corpus è versionato (`CORPUS_VERSION`) anche per questo.

### Comprimere la cronologia: conviene solo a una condizione

```bash
ecotokens compaction
```

Sostituire la cronologia vecchia con un riassunto sembra un risparmio ovvio, e
sui numeri non lo e' affatto. Il riassunto sta all'inizio del prompt: se cambia,
cambia il prefisso, e il prompt caching salta su *tutto* il resto. Misurato su
una consulenza di quaranta turni, contro il non comprimere affatto:

| Strategia | Costo | Da cache | Riassunti | Effetto |
|---|---:|---:|---:|---:|
| nessuna compattazione | $1,9479 | 95% | 0 | riferimento |
| taglio a inseguimento | $2,7366 | 54% | 34 | **−40,5%** |
| taglio a scatti | $1,7864 | 82% | 9 | +8,3% |
| scatti + riassunto incrementale | $1,7555 | 82% | 9 | **+9,9%** |

La colonna dei riassunti spiega tutto: un riassunto per turno è un prefisso
nuovo per turno. Il punto di taglio quindi non insegue la coda della
conversazione, avanza a scatti di 12 messaggi — valore scelto misurando una
curva a U su conversazioni da 20, 40 e 60 turni. Quando lo scatto avanza, il
riassunto nuovo riparte da quello vecchio invece di rileggere tutta la
cronologia.

Il costo del riassuntore è contato: è una chiamata a un modello, e senza
addebitarla la compattazione risulterebbe gratuita per costruzione.

### Accorciare il prompt

```bash
ecotokens prompt
```

Tre livelli, in ordine di rischio crescente. Il primo non cambia una parola:
toglie spazi ripetuti, righe vuote in eccesso, caratteri invisibili da copia e
incolla, virgolette tipografiche — e lascia intatti i blocchi di codice
recintati, dove l'indentazione è significato. Il secondo toglie le perifrasi che
introducono un'istruzione senza aggiungerle nulla. Il terzo sostituisce parole
con sinonimi più corti.

| Livello | Costo | Token tolti | vs originale |
|---|---:|---:|---:|
| prompt originale | $0,1855 | 0 | riferimento |
| normalizzazione | $0,1832 | 924 | +1,3% |
| + formule di riempimento | $0,1731 | 8.704 | +6,7% |
| + sostituzioni lessicali | $0,1647 | 14.864 | +11,3% *(non validato)* |

Due cose vanno dette su questi numeri, perché senza si leggono male.

**La resa è un quarto di quello che sembra.** Togliere mille token dal prompt
rende circa **$0,0014**, contro i $0,0050 che quegli stessi token costerebbero a
prezzo pieno su Opus 5. La differenza è lo sconto che il prompt caching aveva
già fatto. Vale anche per la scelta di *dove* tagliare: system e messaggi utente
rendono $0,00125 e $0,00160 per mille token, perché in una conversazione a più
turni finiscono entrambi nel prefisso servito da cache.

**Un livello è marcato "non validato", e resta spento.** Il banco conta i token
dalla lunghezza del testo. Va bene per chiedersi *dove* finiscono i token tolti,
perché lì conta la tariffa a cui vengono fatturati. Non va bene per chiedersi se
«usare» costi davvero meno token di «utilizzare»: quello lo sa solo
`messages.count_tokens`, e sotto una metrica basata sui caratteri qualunque
accorciamento sembra un guadagno *per costruzione*. Le sostituzioni lessicali
restano quindi disattivate finché il tokenizer vero non le ha confermate:

```bash
ecotokens substitutions --live
```

Ogni candidato viene contato prima e dopo, l'esito finisce in tabella per
modello, e quelli bocciati restano inerti. Senza questo passaggio
`prompt.only_verified` impedisce allo stadio di applicarne alcuno.

**La cache non si muove.** Su tutte le varianti la quota di prompt servita da
cache resta all'82%: le riscritture sono deterministiche e idempotenti, quindi
la cronologia che il client rispedisce a ogni turno viene riscritta sempre allo
stesso modo. Una riscrittura instabile qui varrebbe meno di zero — è lo stesso
errore già trovato nella compattazione.

### La chiave della cache

```bash
ecotokens cachekey
```

Due richieste che differiscono per uno spazio doppio, una riga vuota o una
virgoletta tipografica sono la stessa domanda. Con la chiave calcolata sui byte
grezzi finiscono su voci diverse, e la stessa risposta si paga tante volte
quante sono le varianti.

| Carico | Chiave | Costo | Hit |
|---|---|---:|---:|
| domande ripetute, spaziatura variabile | byte grezzi | $0,2759 | 0/12 |
| domande ripetute, spaziatura variabile | testo normalizzato | $0,1213 | **8/12** |
| domande ripetute identiche | byte grezzi | $0,0869 | 8/12 |
| domande ripetute identiche | testo normalizzato | $0,0869 | 8/12 |

**−56%** sul primo carico, nessuna differenza sul secondo — che è la verifica
che serviva: la normalizzazione allarga la cache, non la rende cieca. È
l'ottimizzazione con la resa più alta di tutto il gateway, e la ragione è
aritmetica: ogni altra leva sconta il prezzo di un token, un hit di cache lo
azzera. Il prompt caching serve un token a 0,1×; la cache esatta non lo serve
affatto.

Stessa normalizzazione prima di calcolare gli embedding della cache semantica.

### Il testo che aggiunge il gateway

```bash
ecotokens overhead
```

Il gateway non si limita a inoltrare: aggiunge testo suo. Delimitatori attorno
al riassunto della cronologia, un blocco per i fatti ricordati, un'istruzione
quando il client chiede JSON, le regole date al riassuntore. Sono token che
l'utente paga senza averli scritti, e sparsi per il codice non li contava
nessuno.

Raccolti in [wording.py](ecotokens/wording.py) con la formulazione precedente
accanto, così il guadagno è verificabile invece che dichiarato: **254 → 174
token per occorrenza, il 31% in meno**. `<riassunto-conversazione-precedente>`
costava 22 token per delimitare ciò che `<storico>` delimita con 6.

A differenza del prompt dell'utente, questo testo è nostro: accorciarlo non
cambia il comportamento di nessuna applicazione. Ma va detto onestamente che
sono token *per occorrenza*, non per richiesta, e sulla fattura incidono poco.
È stato fatto perché è gratis e senza rischio, non perché sposti l'ago.

### Un parametro che sembra da ottimizzare e non lo è

`keep_recent_messages` decide quanti messaggi restano integrali invece di finire
nel riassunto. Misurando, il costo scende in modo monotono man mano che si
abbassa: su cinquanta turni, tenerne 4 costa $1,86 e tenerne 24 costa $3,39.

Non è una scoperta, è una tautologia — comprimere di più costa sempre meno — e
il banco non ha nulla da dire sulla **qualità** della risposta, che è
esattamente ciò che si perde. Il valore resta 8 per fedeltà, non per costo: è un
giudizio, non un ottimo misurato. Chi lo abbassa scambia soldi contro dettaglio,
e ora sa di farlo.

### L'effort sui turni con tool

Il router rifiutava in blocco di abbassare l'effort appena c'erano tool
dichiarati. Contando quante volte lo stadio interveniva davvero: **12 richieste
su 51**, e il blocco dominante non era la soglia sulla domanda ma quel veto —
23 richieste, il 45% del traffico, incluso il carico di costruzione che da solo
vale il 61% della spesa.

La distinzione giusta non è «ci sono tool dichiarati» ma «il modello deve
decidere se e quale usarne»: con `tool_choice: none` i tool ci sono e sono
inutilizzabili, e trattare quel caso come agentico costava effort per niente.

| Regola | Costo totale | vs prima |
|---|---:|---:|
| veto in blocco (com'era) | $1,7387 | riferimento |
| nessun veto, effort `low` | $1,5407 | **+11,4%** |
| nessun veto, effort `medium` | $1,6938 | +2,6% |

**Il default è `medium`, non `low`, e la ragione va detta.** Il banco modella la
*lunghezza* della risposta in funzione dell'effort, non la sua *qualità*. Un
effort basso su un turno agentico può produrre la chiamata sbagliata, e un
tentativo in più costa più di quanto l'effort abbia risparmiato. Quel rischio
qui non è misurabile, quindi il default prende la parte sicura del premio e
l'11,4% pieno resta una scelta esplicita (`router.effort_with_tools = "low"`)
con il rischio scritto accanto.

Il veto resta invece intatto per il **cambio di modello**: lì sbagliare la
scelta di un tool non si paga in token ma in tentativi.

### Trovare la configurazione migliore

```bash
ecotokens optimize
```

Prova più configurazioni sugli stessi carichi e consiglia quella che ha speso
meno — misurata, non dedotta.

### Dashboard

```bash
ecotokens dashboard
```

Genera una pagina HTML autonoma con tutti i parametri: confronto con e senza
gateway, dove finiscono i token di prompt, contributo di ogni stadio,
interazioni fra stadi, strategie di compattazione, livelli di riscrittura del
prompt, storico delle misure e registro delle correzioni. Il gateway la serve
anche su `/admin/dashboard`.

Include una sezione **Progressi rispetto alla versione precedente**: ogni
ottimizzazione confrontata con la misura precedente dello stesso corpus, con la
variazione del suo contributo e l'esito (`migliorato`, `peggiorato`,
`invariato`, `nuovo`). Il confronto è limitato a misure dello stesso corpus di
scenari, ed è un vincolo sostanziale: aggiungere uno scenario cambia il
denominatore di tutte le percentuali, e accostare corpus diversi mostrerebbe
progressi che non ci sono stati. Per questo il corpus è versionato (`v2` da
quando esiste lo scenario dei prompt verbosi).

### Cosa è cambiato misurando

Il registro completo è in [tuning_log.py](ecotokens/tuning_log.py). In sintesi,
otto difetti del *metro* e undici del *gateway*:

- Il marker `cache_control` finiva dentro l'impronta del prefisso del
  simulatore. La misura dava il gateway per dannoso (+6,6% di costo); corretta
  l'impronta, lo stesso carico mostra il 72% di risparmio. Il gateway non era
  cambiato: era sbagliato il metro.
- Il simulatore non modellava né l'effetto dell'effort sui token generati né la
  potatura del contesto: due stadi risultavano inutili per costruzione.
- L'effort adattivo giudicava la difficoltà dai token dell'intero prompt, con
  soglia 400: qualunque system prompt reale la supera, quindi non interveniva
  mai. Ora misura la domanda, non il contesto.
- Il primo turno non scriveva in cache, per un ragionamento plausibile e
  sbagliato: il pareggio è a due richieste, quindi la prima scrittura sembrava
  una perdita. Ma il pezzo più grosso del prefisso — system prompt e definizioni
  dei tool — è condiviso anche fra conversazioni *diverse*, quindi quella
  scrittura la rilegge la richiesta successiva di chiunque. Invertito il default,
  il risparmio complessivo passa dal 68% al 70%, e su venti richieste isolate che
  condividono il system prompt la differenza è del 155%.
- Il punto di taglio della compattazione inseguiva la coda della conversazione,
  quindi si spostava a ogni turno: il riassunto veniva rifatto ogni volta,
  diverso ogni volta, e il prefisso cambiava sempre. Su quaranta turni la
  compattazione costava il **40,5% più** del non comprimere affatto. Il codice
  memorizzava il riassunto per riusarlo, ma la chiave conteneva il punto di
  taglio: una chiave che si muove con la conversazione non combacia mai. Il test
  lo verificava a cronologia ferma, l'unico caso in cui funzionava.
- La spesa delle chiamate che il gateway fa per conto proprio — il riassunto di
  compattazione — non era contata da nessuna parte: non compare in
  `response.usage` della richiesta dell'utente. Uno stadio che sembra gratuito
  viene acceso quando non conviene.
- Il simulatore ignorava `max_tokens`, quindi qualunque tetto imposto dal
  gateway era invisibile alla misura. Corretto: e la misura dice che il tetto sul
  riassunto non morde mai su questi carichi. Resta come paracadute, non come
  risparmio.
- Accorciare il prompt rende circa **un quarto** di quello che sembra: quasi
  tutti i token tolti sarebbero comunque stati serviti dalla cache a un decimo
  del prezzo. Lo stadio resta utile su prompt scritti male (−11%), marginale sul
  corpus completo (0,2%), ed è documentato per quello che è.
- Il risparmio in token delle sostituzioni lessicali **non è misurabile qui**:
  il banco conta i token dai caratteri, quindi una tabella di sinonimi più corti
  si autoconfermerebbe. Restano spente finché `ecotokens substitutions --live`
  non le confronta col conteggio vero.
- La chiave della cache esatta si calcolava sui byte grezzi, quindi due
  richieste uguali a meno di uno spazio finivano su voci diverse. Il
  riconoscimento di sessione normalizzava già la spaziatura; la cache no, e non
  se n'era accorto nessuno perché tutti gli scenari ripetevano le domande
  identiche. Corretto: **−56%** su un carico con spaziatura variabile.
- `keep_recent_messages` sembra un parametro da ottimizzare e non lo è: il costo
  scende sempre abbassandolo, ma quello che si perde — la qualità della
  risposta — il banco non lo misura. Qui la misura era corretta e rispondeva a
  una domanda diversa da quella che sembrava.

## Come funziona dentro

```
Client OpenAI ─► /v1/chat/completions ─┐
                                       │  traduzione + sanificazione
Client nativo ─► /v1/messages ─────────┤  (solo per il dialetto OpenAI)
                                       │
  1. sessione        │  riconosce a quale conversazione appartiene
  2. cache esatta    │──► hit: risposta immediata, zero token
  3. cache semantica │──► hit: risposta immediata, zero token
  4. budget          │──► blocco se il tetto di spesa è superato
  5. prompt          │  riscrive il testo in forma più concisa
  6. memoria         │  inietta in coda i fatti pertinenti
  7. contesto        │  pota i tool result, riassume la parte vecchia
  8. router          │  abbassa l'effort, sceglie il modello
  9. cache planner   │  piazza i breakpoint cache_control
                     ▼
              API Anthropic
                     │
 10. contabilità     │  usage reale → costo, risparmio, cache hit rate
                     ▼
   risposta nel dialetto di chi ha chiesto (streaming incluso)
```

L'ordine non è casuale. Il budget sta **dopo** le cache perché una risposta
servita dalla cache non spende nulla e non ha senso rifiutarla; sta comunque
prima di qualunque chiamata vera all'API. Il cache planner sta per ultimo
perché piazzare i breakpoint prima significherebbe marcarli su un testo che
gli stadi successivi cambiano ancora.

### Il riconoscimento di sessione

Un client OpenAI rispedisce l'intera cronologia a ogni turno e non conosce il
concetto di sessione. Senza risolvere questo, memoria e compattazione sono
impossibili.

EcoTokens riconosce la conversazione in due passaggi: l'incipit (`system` più
primo messaggio) individua una *famiglia* di conversazioni, e il confronto
della cronologia sceglie quale sessione della famiglia sta continuando —
quella la cui storia registrata è un prefisso di quella appena arrivata.

Un client che può collaborare manda l'header `X-EcoTokens-Session` e salta del
tutto l'euristica.

### Parametri che vengono scartati

I modelli Claude attuali **rifiutano con un 400** parametri che i client
OpenAI mandano di routine. Il gateway li rimuove e lo annota:

- `temperature`, `top_p`, `top_k`, `frequency_penalty`, `presence_penalty`, `seed`
- un messaggio `assistant` in ultima posizione (prefill)
- `n > 1`: l'API restituisce una sola risposta

Altre traduzioni: `response_format: json_schema` diventa
`output_config.format`; `json_object`, che non ha un equivalente diretto,
diventa un'istruzione in coda ai messaggi, dove non tocca il prefisso in
cache; `reasoning_effort` diventa `output_config.effort`.

### Perché non la compattazione server-side dell'API

L'API offre una compattazione automatica (`compact_20260112`), ma richiede di
riaccodare i blocchi di compattazione a ogni turno successivo. Un client
OpenAI rispedisce la *propria* cronologia e quei blocchi andrebbero persi al
primo giro. EcoTokens usa quindi un riassunto locale, calcolato una volta per
punto di taglio e poi riusato alla lettera: se cambiasse a ogni turno
cambierebbe il prefisso del prompt e la cache mancherebbe sempre.

## Sviluppo

```bash
pip install -e .[dev]
```

```bash
pytest
```

I test girano contro uno stub dell'API collegato via ASGI: nessuna porta,
nessuna rete, nessun token speso. Vale la pena sapere **perché** non si usano
`respx` o `pytest-httpx`: l'SDK `anthropic` 1.x gira su `httpx2`, non su
`httpx`, quindi quelle librerie non intercettano nulla e i test passerebbero
contro il vuoto.

Lo stub simula anche il prompt caching, tenendo traccia dei prefissi marcati
già visti: è così che il test di regressione verifica che dal secondo turno in
poi la cache venga davvero letta.

## Comandi

| Comando | Cosa fa |
|---|---|
| `ecotokens serve` | avvia il gateway |
| `ecotokens stats` | riepilogo di consumi, costi e risparmio |
| `ecotokens purge` | rimuove le voci di cache scadute |
| `ecotokens purge --everything` | svuota le cache |
| `ecotokens bench` | misura lo stesso carico con e senza gateway |
| `ecotokens ablate` | attribuisce il risparmio a ciascuno stadio |
| `ecotokens optimize` | prova più configurazioni e consiglia la migliore |
| `ecotokens compaction` | confronta le strategie di compattazione della cronologia |
| `ecotokens prompt` | misura i livelli di riscrittura del prompt |
| `ecotokens substitutions` | verifica quali sinonimi costano davvero meno token |
| `ecotokens cachekey` | misura l'effetto della normalizzazione sulla chiave di cache |
| `ecotokens overhead` | mostra il testo che il gateway aggiunge ai prompt |
| `ecotokens pruning` | confronta le strategie di potatura del contesto |
| `ecotokens cachewrites` | conta le scritture in cache che nessuno rilegge |
| `ecotokens ceiling` | dice fin dove puo' arrivare il risparmio, e cosa lo ferma |
| `ecotokens dashboard` | genera la dashboard HTML |

## Endpoint

| Endpoint | Descrizione |
|---|---|
| `POST /v1/chat/completions` | compatibile OpenAI, streaming incluso |
| `GET /v1/models` | catalogo dei modelli, con prezzi e finestra di contesto |
| `GET /admin/stats` | statistiche di consumo e risparmio |
| `GET /admin/sessions` | sessioni riconosciute |
| `POST /admin/cache/prune` | pulizia delle voci scadute |
| `POST /admin/cache/clear` | svuotamento delle cache |
| `GET /admin/dashboard` | dashboard delle misure (`?measure=true` per rimisurare) |
| `GET /health` | stato del servizio |

## Licenza

MIT. Vedi [LICENSE](LICENSE).
