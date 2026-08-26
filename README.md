# EcoTokens

Gateway locale che si mette tra le tue applicazioni e l'API Anthropic, espone
un'interfaccia **compatibile OpenAI** e riduce la spesa in token.

Le applicazioni non vanno riscritte: si cambia `base_url` e basta.

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
| **Effort adattivo** | taglia i token di ragionamento sulle richieste semplici | nessuno |
| **Potatura del contesto** | difesa contro l'overflow, non un risparmio | può azzerare il caching |
| **Compattazione con riassunto** | sostituisce la cronologia vecchia con un riassunto stabile | perdita di dettaglio |
| **Cache esatta** | richieste identiche servite a costo zero | nessuno |
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
| chat, 8 turni con system prompt grande | $0,4359 | $0,1283 | **71%** | 86% |
| ciclo agentico, 6 turni da 6 tool | $0,6249 | $0,3195 | **49%** | 73% |
| domande frequenti ripetute | $0,3779 | $0,0509 | **86%** | 73% |
| costruzione di EcoTokens | $2,9926 | $0,8255 | **72%** | 90% |
| **totale** | **$4,4312** | **$1,3242** | **70%** | 87% |

Lo scenario `costruzione` non è inventato: legge i sorgenti veri del progetto e
ricostruisce il traffico che un agente di codice produce scrivendolo.

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
| prompt caching | **60%** |
| cache esatta | 6% |
| effort adattivo | 2,5% |
| potatura del contesto | 0% (non interviene mai con la soglia predefinita) |

### Due ottimizzazioni che litigano

Potare i vecchi risultati dei tool toglie token dal prompt, ma sposta il confine
di taglio a ogni turno: il prefisso cambia e il prompt caching salta. Misurato:

| Carico | Solo caching | Caching + potatura | Effetto |
|---|---:|---:|---:|
| ciclo agentico | $0,3233 (72% da cache) | $0,2888 (40% da cache) | **+11%** |
| costruzione | $0,8183 (90% da cache) | $1,1196 (21% da cache) | **−37%** |

Per questo la potatura resta una difesa contro l'esaurimento della finestra di
contesto, non un modo per risparmiare, e la sua soglia resta alta.

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
interazioni fra stadi, storico delle misure e registro delle correzioni. Il
gateway la serve anche su `/admin/dashboard`.

### Cosa è cambiato misurando

Il registro completo è in [tuning_log.py](ecotokens/tuning_log.py). In sintesi,
tre difetti del *metro* e due del *gateway*:

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

## Come funziona dentro

```
Client OpenAI ─► FastAPI /v1/chat/completions
                     │
                     ▼  traduzione + sanificazione OpenAI → Anthropic
                     │
  1. sessione        │  riconosce a quale conversazione appartiene
  2. cache esatta    │──► hit: risposta immediata, zero token
  3. cache semantica │──► hit: risposta immediata, zero token
  4. budget          │──► blocco se il tetto di spesa è superato
  5. memoria         │  inietta in coda i fatti pertinenti
  6. contesto        │  pota i tool result, riassume la parte vecchia
  7. router          │  abbassa l'effort, sceglie il modello
  8. cache planner   │  piazza i breakpoint cache_control
                     ▼
              API Anthropic
                     │
  9. contabilità     │  usage reale → costo, risparmio, cache hit rate
                     ▼
        traduzione Anthropic → OpenAI (streaming incluso)
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
