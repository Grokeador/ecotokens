# Registro delle versioni

Questo file dice **cosa cambia per chi usa** il gateway. Il registro delle
misure — perché un default è quello che è, e quale misura lo ha deciso — sta in
[tuning_log.py](ecotokens/tuning_log.py) e nella dashboard: sono due domande
diverse, e tenerle in un file solo renderebbe illeggibili entrambe.

Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/), le
versioni [SemVer](https://semver.org/lang/it/). Finché la maggiore è 0, un
cambio della minore può contenere rotture: sono elencate per prime.

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
