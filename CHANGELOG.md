# Registro delle versioni

Questo file dice **cosa cambia per chi usa** il gateway. Il registro delle
misure — perché un default è quello che è, e quale misura lo ha deciso — sta in
[tuning_log.py](ecotokens/tuning_log.py) e nella dashboard: sono due domande
diverse, e tenerle in un file solo renderebbe illeggibili entrambe.

Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/), le
versioni [SemVer](https://semver.org/lang/it/). Finché la maggiore è 0, un
cambio della minore può contenere rotture: sono elencate per prime.

## [Non rilasciato]

### Corretto

- **Un nome di modello sconosciuto non produce più cifre inventate.** Il
  gateway ripiega sul modello predefinito quando non riconosce un nome — e
  continua a farlo, perché deve servire la richiesta — ma finora prezzava anche
  il ripiego: `llama-3.3-70b`, `qwen2.5-coder:32b` o un `claude-opuss-5`
  sbagliato di battitura finivano nel conto a 5/25 USD per Mtok, le tariffe di
  Opus 5. Ora queste richieste escono dal confronto (`richieste_confrontabili`
  non le conta) e la risposta porta una nota con il nome ricevuto e la tariffa
  effettivamente usata. La spesa resta registrata e il tetto continua a
  contarla.
- Il nome del modello **come il client lo ha scritto** arriva ora fino al
  registro. Prima veniva normalizzato in traduzione e nessuno stadio a valle
  poteva più accorgersi che il costo stava per essere calcolato con le tariffe
  di un altro modello.

## [0.2.0] — 2026-08-29

Una versione di **robustezza e onestà dei numeri**. Il gateway non può più far
fallire una richiesta che sarebbe passata senza di lui, e il risparmio che
dichiara è quello che aggiunge davvero.

### Rotture

- **Il numero di testa cambia significato.** `stats`, la console, il quadro e
  la dashboard mostrano ora **Merito del gateway** accanto al risparmio totale.
  Il totale si misura contro un client che non usa affatto il prompt caching —
  oggi non lo fa nessuno — e comprende quindi lo sconto che Anthropic dà a
  chiunque. Il merito lo esclude. Chi legge un numero solo leggerà un numero
  più piccolo di prima, e più vero.
- **Le righe di consumo già in archivio non hanno il nuovo confronto**, e non
  vengono usate per calcolarlo: la pagina dice su quante richieste si regge.
  Non serve nessuna migrazione manuale, le colonne si aggiungono da sole.
- `messages` è ora obbligatorio e non vuoto sulla porta OpenAI: un corpo senza
  messaggi riceve `400` invece di arrivare all'API.
- Gli errori di validazione escono con `400` e il campo `error` nel formato
  OpenAI, non più con `422` e il campo `detail` di FastAPI.

### Nuovo

- `ecotokens diagnosi` — nove controlli sull'installazione, e per ognuno che non
  va, cosa fare. Non stampa mai il valore di una credenziale.
- `ecotokens assunzioni` — le undici cose che il progetto dà per vere sul
  comportamento dell'API, con cosa cambierebbe se fossero sbagliate.
- `ecotokens verifica --live` — ne controlla cinque contro l'API vera. Si
  rifiuta di girare contro il simulatore senza `--anche-simulato`.
- `cache_planner.adatta_primo_turno` — il gateway osserva quanto spesso le
  proprie conversazioni proseguono e smette di marcare la coda quando non
  conviene. Il pareggio è il rapporto fra i moltiplicatori dell'API, non una
  soglia scelta.

### Corretto

- **Uno stadio che si rompe non abbatte più la richiesta.** Viene annullato —
  parametri riportati a com'erano — e la catena prosegue; dopo tre guasti
  consecutivi si spegne, e la console dice che è stato il gateway a spegnerlo.
  Il tetto di spesa resta l'unico che può fermare una richiesta.
- **Uno stream chiuso a metà non viene più consegnato come risposta completa.**
  Prima usciva con `finish_reason: "stop"`, indistinguibile da una risposta
  finita. Ora `finish_reason` resta nullo con un blocco di errore esplicito, e
  la risposta tagliata non entra in cache.
- **La spesa di uno stream caduto viene registrata.** Il prompt era già stato
  pagato per intero, ma `stats` non lo vedeva e il tetto di spesa non lo
  contava.
- La chiusura del gateway tenta tutti i passi: una potatura fallita non lascia
  più aperti database e client HTTP.

### Misurato

Contro uno sviluppatore che mette un `cache_control` sul proprio system prompt
— una riga, ed è la pratica documentata:

| Traffico | Merito del gateway |
|---|---:|
| ciclo agentico, 20 turni con tool | **+52,0%** |
| domande che si ripetono | **+87,2%** |
| chat che cresce, 8 turni | **+22,6%** |
| molti utenti, stesso system, turno singolo | **−0,2%** |

537 test, copertura 85%. Nessuna misura ha ancora toccato l'API vera:
`ecotokens assunzioni` dice esattamente cosa resta da verificare.

## [0.1.0] — 2026-08-28

Prima versione numerata. Il gateway funziona ed è misurato; non è ancora
pubblicato su PyPI, quindi si installa dai sorgenti.

### Cosa fa

- **Due porte in ingresso**: `/v1/chat/completions` in dialetto OpenAI, perché
  le applicazioni esistenti cambino solo `base_url`, e `/v1/messages` in
  dialetto nativo Claude. Un solo provider a valle: Anthropic.
- **Prompt caching pianificato**: fino a 4 breakpoint, soglia minima per
  modello, TTL dedotto dalla sessione. Vale **+19,9%** dove molte richieste
  condividono un prefisso, **−0,1%** su una conversazione sola che cresce —
  `cache_planner.mode = "automatico"` delega ad Anthropic quando conviene.
- **Cache esatta** delle risposte, con chiave sul testo normalizzato.
- **Potatura e riassunto del contesto** a scatti, per non distruggere il
  prefisso in cache.
- **Effort adattivo** e, nel profilo aggressivo, declassamento del modello.
- **Tetto di spesa** con preventivo `count_tokens`, spento finché non gli si dà
  una cifra.
- **Tre pagine**: `/quadro` (cruscotto compatto), `/` (console dal vivo),
  `/admin/dashboard` (rapporto esteso).
- **Undici comandi di misura**, fra cui `bench`, `ablate`, `ritenzione`,
  `memoria`, `cachewrites`, `ceiling`.

### Quanto risparmia

Contro un'applicazione che usa **già** il caching automatico di Anthropic —
che è il confronto onesto, perché quello è gratis per chiunque:

| Carico | In meno |
|---|---:|
| domande distinte, stesso prompt di sistema | 81,7% |
| conversazione con system grande e stabile | 35,8% |
| ciclo agentico con tool | 8,2% |
| **totale del corpus** | **23,2%** |

Senza cambiare nessuna risposta. Col profilo aggressivo si arriva all'85,2%,
ma quella è un'altra risposta a un prezzo diverso.

### Da sapere prima di installarlo

- Il profilo predefinito è **`aggressivo`**: declassa il modello e tiene
  l'effort al minimo. Chi vuole le stesse risposte pagate meno metta
  `profilo = "prudente"`.
- Il **tetto di spesa è spento**: non esiste una cifra predefinita sensata.
- Il gateway ascolta su `127.0.0.1`. Su un indirizzo raggiungibile da altre
  macchine **si rifiuta di partire** senza `server.api_key`: quella porta
  inoltra all'API con la chiave Anthropic dell'utente.
