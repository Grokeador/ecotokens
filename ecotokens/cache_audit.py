"""Quante scritture in cache non vengono mai rilette.

Il prompt caching vale da solo due terzi del risparmio misurato del gateway;
gli altri quattro stadi messi insieme non arrivano al sette per cento. Ne segue
che l'unico posto dove convenga cercare ancora e' dentro quei due terzi, e la
domanda che il gateway non sapeva rispondere e' *quanto di cio' che scrive
viene poi riletto*.

La domanda ha un peso preciso. Una scrittura costa 1.25x (TTL cinque minuti) o
2x (un'ora); una rilettura costa 0.1x. Una scrittura riletta anche una sola
volta e' gia' in guadagno. Una scrittura mai riletta e' una perdita netta pari
al 25% - o al 100% - del suo prezzo pieno: si e' pagato di piu' per non avere
niente in cambio. Il pianificatore piazza fino a quattro breakpoint per
richiesta e finora nessuno ha mai contato quanti di quei quattro rendano.

## Come si attribuisce una rilettura a una scrittura

L'API non dice *quale* voce di cache ha letto: dice quanti token ha letto e
quanti ne ha scritti, e basta. L'attribuzione si ricostruisce dal fatto che la
cache e' un match di prefisso, e quindi le letture crescono da sinistra:

    estensione coperta dalla cache dopo la richiesta i  =  R_i + W_i

dove R e' ``cache_read_tokens`` e W ``cache_creation_tokens``. Se una richiesta
successiva della stessa sessione legge piu' a fondo di R_i, quella differenza
puo' venire solo da cio' che la richiesta i ha scritto.

A partire da li' si guarda avanti, dalla richiesta i in poi, e ci si ferma alla
prima delle due cose che succede:

* una richiesta legge oltre R_i - la scrittura e' ripagata, per
  ``min(W_i, R_j - R_i)`` token;
* una richiesta **scrive** partendo da una lettura che non supera R_i - il
  prefisso e' ripartito da un punto che sta a monte, quindi cio' che la
  richiesta i aveva scritto non e' piu' raggiungibile da nessuno: e' orfano.

Una richiesta che non fa ne' l'una ne' l'altra - legge poco e non riscrive -
non chiude il conto: la voce puo' essere ancora viva e venire ripresa dopo. Su
questo il conto e' volutamente generoso, e il numero che ne esce resta un
**limite inferiore** allo spreco.

La seconda condizione e' arrivata dopo, e per la via consueta: un test. La
prima versione prendeva semplicemente la lettura piu' profonda fra tutte le
successive, e su ``(R=0,W=1000) (R=0,W=800) (R=800,W=0)`` dava entrambe le
scritture per ripagate. Ma il secondo 800 puo' venire solo dalla seconda
scrittura - la prima era gia' morta quando il prefisso e' ripartito da zero.
Una regola che si limita a "prendi il massimo" accredita due volte la stessa
rilettura, e sbaglia nel verso che fa sembrare il gateway migliore.

## I due sprechi hanno nature diverse, e vanno tenuti separati

* **Di coda.** L'ultima scrittura di una sessione non ha, per definizione, una
  richiesta dopo di se'. Non e' un difetto del pianificatore: e' il prezzo di
  non sapere in anticipo che la conversazione finisce li'. Puo' inoltre essere
  riletta da un'altra sessione che condivide il prefisso di sistema, e questo
  conto - che guarda una sessione alla volta - non lo vedrebbe. Va quindi
  letto come un tetto, non come una perdita accertata.
* **In mezzo.** Una scrittura seguita da altre richieste che non la rileggono
  e' un breakpoint piazzato male, oppure un prefisso invalidato fra un turno e
  l'altro. E' l'unica delle due che il pianificatore possa evitare, ed e'
  quella su cui vale la pena intervenire.

Sommarle darebbe un numero piu' grosso e meno utile: si agirebbe su una quota
che in parte non dipende da nessuna decisione del gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .pricing import CACHE_WRITE_MULTIPLIER, DEFAULT_MODEL, model_info


@dataclass(frozen=True)
class CacheEvent:
    """Una richiesta arrivata davvero all'API, ridotta ai suoi due contatori."""

    session_id: str
    read_tokens: int
    write_tokens: int
    model: str = DEFAULT_MODEL
    cache_ttl: str = "5m"


@dataclass(frozen=True)
class WastedWrite:
    """Una scrittura che nessuna richiesta successiva ha riletto."""

    session_id: str
    model: str
    cache_ttl: str
    tokens: int
    di_coda: bool
    costo_usd: float


@dataclass
class CacheWriteAudit:
    """Esito del conto. I token sprecati restano divisi per natura."""

    sessioni: int = 0
    scritture: int = 0
    token_scritti: int = 0
    token_recuperati: int = 0
    token_sprecati_in_mezzo: int = 0
    token_sprecati_di_coda: int = 0
    costo_sprecato_in_mezzo_usd: float = 0.0
    costo_sprecato_di_coda_usd: float = 0.0
    sprechi: list[WastedWrite] = field(default_factory=list)

    @property
    def token_sprecati(self) -> int:
        return self.token_sprecati_in_mezzo + self.token_sprecati_di_coda

    @property
    def costo_sprecato_usd(self) -> float:
        return self.costo_sprecato_in_mezzo_usd + self.costo_sprecato_di_coda_usd

    @property
    def quota_sprecata(self) -> float:
        """Quota dei token scritti che nessuno ha riletto, coda compresa."""
        if not self.token_scritti:
            return 0.0
        return self.token_sprecati / self.token_scritti

    @property
    def quota_sprecata_in_mezzo(self) -> float:
        """La quota su cui il pianificatore puo' fare qualcosa."""
        if not self.token_scritti:
            return 0.0
        return self.token_sprecati_in_mezzo / self.token_scritti

    def to_dict(self) -> dict[str, object]:
        return {
            "sessioni": self.sessioni,
            "scritture": self.scritture,
            "token_scritti": self.token_scritti,
            "token_recuperati": self.token_recuperati,
            "token_sprecati": self.token_sprecati,
            "token_sprecati_in_mezzo": self.token_sprecati_in_mezzo,
            "token_sprecati_di_coda": self.token_sprecati_di_coda,
            "costo_sprecato_usd": self.costo_sprecato_usd,
            "costo_sprecato_in_mezzo_usd": self.costo_sprecato_in_mezzo_usd,
            "costo_sprecato_di_coda_usd": self.costo_sprecato_di_coda_usd,
            "quota_sprecata": self.quota_sprecata,
            "quota_sprecata_in_mezzo": self.quota_sprecata_in_mezzo,
        }


def costo_scrittura_sprecata(tokens: int, model: str, cache_ttl: str) -> float:
    """Quanto e' costata, in piu' del prezzo pieno, una scrittura mai riletta.

    Non e' il prezzo della scrittura: e' il **sovrapprezzo**. Quegli stessi
    token, senza marker, sarebbero stati fatturati a 1x. Marcandoli si e'
    pagato 1.25x (o 2x) e non si e' riletto niente, quindi la perdita e' la
    differenza, non il totale.
    """
    if tokens <= 0:
        return 0.0
    prezzo = model_info(model).input_per_mtok
    moltiplicatore = CACHE_WRITE_MULTIPLIER.get(cache_ttl, 1.25)
    return tokens / 1_000_000 * prezzo * (moltiplicatore - 1.0)


def _quanto_e_stata_riletta(sessione: list[CacheEvent], i: int) -> int:
    """Token della scrittura ``i`` che una richiesta successiva ha riletto.

    Si guarda avanti e ci si ferma alla prima cosa che chiude il conto: una
    lettura che supera il punto da cui la scrittura partiva, oppure una nuova
    scrittura che riparte da un punto a monte - e allora quella di ``i`` non e'
    piu' raggiungibile. Le richieste che non fanno ne' l'una ne' l'altra si
    scavalcano: la voce potrebbe essere ancora viva.
    """
    scrittura = sessione[i]
    for successiva in sessione[i + 1 :]:
        if successiva.read_tokens > scrittura.read_tokens:
            guadagno = successiva.read_tokens - scrittura.read_tokens
            return min(scrittura.write_tokens, guadagno)
        if successiva.write_tokens > 0:
            # Il prefisso e' ripartito da un punto che non sta oltre il nostro:
            # cio' che avevamo scritto e' orfano da qui in poi.
            return 0
    return 0


def audit_cache_writes(events: Iterable[CacheEvent]) -> CacheWriteAudit:
    """Conta le scritture non rilette. Gli eventi vanno in ordine cronologico.

    Le richieste servite dalla cache locale del gateway non vanno passate: non
    toccano la cache dell'API e allungherebbero le sessioni con eventi vuoti.
    """
    per_sessione: dict[str, list[CacheEvent]] = {}
    for evento in events:
        per_sessione.setdefault(evento.session_id, []).append(evento)

    esito = CacheWriteAudit(sessioni=len(per_sessione))

    for sessione in per_sessione.values():
        n = len(sessione)
        for i, evento in enumerate(sessione):
            if evento.write_tokens <= 0:
                continue
            esito.scritture += 1
            esito.token_scritti += evento.write_tokens

            recuperato = _quanto_e_stata_riletta(sessione, i)
            sprecato = evento.write_tokens - recuperato
            esito.token_recuperati += recuperato
            if sprecato <= 0:
                continue

            # Di coda vuol dire: non c'era nessuna richiesta dopo che potesse
            # rileggerla. Non e' un difetto del pianificatore, e' il prezzo di
            # non sapere che la conversazione finiva li'.
            di_coda = i == n - 1
            costo = costo_scrittura_sprecata(sprecato, evento.model, evento.cache_ttl)
            if di_coda:
                esito.token_sprecati_di_coda += sprecato
                esito.costo_sprecato_di_coda_usd += costo
            else:
                esito.token_sprecati_in_mezzo += sprecato
                esito.costo_sprecato_in_mezzo_usd += costo
            esito.sprechi.append(
                WastedWrite(
                    session_id=evento.session_id,
                    model=evento.model,
                    cache_ttl=evento.cache_ttl,
                    tokens=sprecato,
                    di_coda=di_coda,
                    costo_usd=costo,
                )
            )

    return esito
