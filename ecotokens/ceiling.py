"""Fin dove puo' arrivare il risparmio, e cosa lo ferma.

Prima o poi qualcuno chiede al gateway un numero tondo - il 90, il 95, il 99
per cento - e la domanda merita una risposta aritmetica invece di un tentativo.
Questo modulo la calcola: accende le leve una alla volta, dalla piu' sicura
alla piu' invasiva, e alla fine scompone cio' che resta per dire quale pezzo
non si puo' togliere.

## Le leve non sono tutte della stessa natura

Le prime non costano niente che non sia gia' misurato. Le ultime scambiano
denaro contro qualita', e il banco la qualita' **non la misura**: sa quanto e'
lunga una risposta, non se e' giusta. Presentarle tutte nella stessa colonna
farebbe sembrare il 95% un traguardo raggiunto invece che un prezzo pagato, ed
e' esattamente il modo in cui un banco di prova comincia a mentire. Per questo
ogni gradino porta scritto cosa si e' dato in cambio.

## Il pavimento

Sotto una certa cifra non si scende, e la ragione e' che il modello deve pur
rispondere. I token generati si pagano a prezzo pieno sempre: nessuna cache li
sconta, perche' non esistevano prima della richiesta. A questi si aggiunge il
contenuto nuovo di ogni prompt, che va trasmesso almeno una volta.

    pavimento = output + input mai visto prima + riletture

Quando il pavimento supera l'obiettivo, l'obiettivo non e' difficile: e'
impossibile, e conviene saperlo prima di passare una settimana a inseguirlo.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings
from .pricing import CACHE_WRITE_MULTIPLIER, model_info
from .workloads import Scenario, all_scenarios

# Il modello piu' economico del listino: definisce il pavimento, perche'
# nessuna configurazione puo' pagare i token meno di cosi'.
MODELLO_MINIMO = "claude-haiku-4-5"

# La Message Batches API sconta della meta' sia l'input sia l'output, e si
# compone con il prompt caching. E' l'unico sconto del listino che il gateway
# non usa, e la ragione non e' tecnica: le richieste diventano asincrone, con
# esito entro 24 ore. Un gateway che sta in mezzo a un'applicazione interattiva
# non puo' adottarlo di sua iniziativa, ma chi ha traffico che puo' aspettare
# lascerebbe sul tavolo meta' della spesa a non usarlo.
#
# Qui entra come moltiplicatore invece che come misura: uno sconto sulla
# fattura non cambia quanti token passano, quindi simularlo direbbe solo quello
# che l'aritmetica dice gia'. Il gradino resta pero' nella scala, perche' un
# tetto che non lo conta non e' un tetto.
SCONTO_BATCH = 0.5


@dataclass
class CeilingStep:
    """Un gradino della scala, con il suo prezzo in cose non misurate."""

    etichetta: str
    descrizione: str
    #: Cosa si e' dato in cambio del risparmio. Vuoto se non si e' dato niente.
    in_cambio: str
    cost_usd: float
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    full_price_tokens: int

    @property
    def sicura(self) -> bool:
        return not self.in_cambio

    def saved_ratio(self, riferimento: float) -> float:
        return (riferimento - self.cost_usd) / riferimento if riferimento else 0.0


@dataclass
class CeilingFloor:
    """Il costo che nessuna configurazione puo' togliere."""

    output_usd: float
    input_nuovo_usd: float
    riletture_usd: float

    @property
    def totale_usd(self) -> float:
        return self.output_usd + self.input_nuovo_usd + self.riletture_usd


@dataclass
class CeilingReport:
    baseline_usd: float
    steps: list[CeilingStep] = field(default_factory=list)
    floor: CeilingFloor | None = None

    @property
    def migliore(self) -> CeilingStep | None:
        return min(self.steps, key=lambda s: s.cost_usd) if self.steps else None

    @property
    def massimo_sicuro(self) -> CeilingStep | None:
        sicuri = [s for s in self.steps if s.sicura]
        return min(sicuri, key=lambda s: s.cost_usd) if sicuri else None

    def raggiungibile(self, obiettivo: float) -> bool:
        """Se un risparmio dato sia compatibile con il pavimento.

        ``obiettivo`` e' una frazione: 0.99 per il novantanove per cento.
        """
        if self.floor is None:
            return False
        return self.floor.totale_usd <= self.baseline_usd * (1.0 - obiettivo)

    def tetto_teorico(self) -> float:
        """Il risparmio massimo che il pavimento consente, come frazione."""
        if self.floor is None or not self.baseline_usd:
            return 0.0
        return 1.0 - self.floor.totale_usd / self.baseline_usd


def _su_modello(scenari: list[Scenario], modello: str) -> list[Scenario]:
    """Gli stessi carichi, chiesti a un modello diverso.

    Non e' un'impostazione del gateway ma una scelta di chi chiama: si misura
    cosi' perche' e' cosi' che accadrebbe davvero. Il riferimento resta quello
    del modello originale, altrimenti si confronterebbero due domande diverse.
    """
    copie = []
    for scenario in scenari:
        richieste = []
        for payload in scenario.requests:
            nuovo = copy.deepcopy(payload)
            nuovo["model"] = modello
            richieste.append(nuovo)
        copie.append(
            Scenario(
                name=scenario.name, description=scenario.description, requests=richieste
            )
        )
    return copie


def _pavimento(step: CeilingStep, modello: str = MODELLO_MINIMO) -> CeilingFloor:
    """Scompone un gradino in cio' che si potrebbe ancora togliere e cio' che no.

    I token scritti in cache sono, per la maggior parte, contenuto che il
    modello non aveva mai visto: vanno trasmessi comunque, e il conto li
    valuta a prezzo pieno - cioe' **meno** di quanto si sia speso davvero, che
    e' 1,25x. E' voluto: il pavimento deve essere una promessa che nessuno puo'
    battere, non una stima realistica.
    """
    info = model_info(modello)
    return CeilingFloor(
        output_usd=step.output_tokens * info.output_per_mtok / 1_000_000,
        input_nuovo_usd=(step.cache_write_tokens + step.full_price_tokens)
        * info.input_per_mtok
        / 1_000_000,
        riletture_usd=step.cache_read_tokens * info.input_per_mtok * 0.1 / 1_000_000,
    )


def sovrapprezzo_scrittura(tokens: int, modello: str, ttl: str = "5m") -> float:
    """Quanto costa in piu' marcare dei token invece di pagarli e basta."""
    moltiplicatore = CACHE_WRITE_MULTIPLIER.get(ttl, 1.25)
    return tokens * model_info(modello).input_per_mtok * (moltiplicatore - 1.0) / 1_000_000


async def measure_ceiling(*, live: bool = False) -> CeilingReport:
    """Accende le leve una alla volta e calcola dove finisce la strada."""
    from .bench import _abilita_prompt, _run_scenario, make_settings

    scenari = all_scenarios()

    async def somma(applica, carichi: list[Scenario], etichetta: str):
        totali = [0, 0, 0, 0]
        costo = 0.0
        for scenario in carichi:
            misura = await _run_scenario(
                scenario, make_settings(applica), etichetta, live=live
            )
            costo += misura.cost_usd
            totali[0] += misura.output_tokens
            totali[1] += misura.cache_write_tokens
            totali[2] += misura.cache_read_tokens
            totali[3] += misura.full_price_tokens
        return costo, totali

    riferimento, _ = await somma(None, scenari, "senza-gateway")
    report = CeilingReport(baseline_usd=riferimento)

    def predefinita(settings: Settings) -> None:
        _abilita_prompt(settings)

    def effort_spinto(settings: Settings) -> None:
        predefinita(settings)
        settings.router.effort_with_tools = "low"

    def modello_economico(settings: Settings) -> None:
        effort_spinto(settings)
        settings.router.model_downgrade = True

    gradini: list[tuple[str, str, str, Callable[[Settings], None] | None, str]] = [
        (
            "predefinita",
            "Tutti gli stadi sicuri accesi, come esce dalla scatola.",
            "",
            predefinita,
            "",
        ),
        (
            "+ effort minimo sui tool",
            "Abbassa l'effort anche sui turni in cui il modello deve scegliere "
            "uno strumento.",
            "La qualita' delle chiamate ai tool. Il banco misura quanto e' "
            "lunga una risposta, non se la chiamata era quella giusta: un "
            "tentativo sbagliato costa piu' dell'effort risparmiato.",
            effort_spinto,
            "",
        ),
        (
            "+ modello economico dove serve poco",
            "Sulle richieste giudicate semplici il router passa a un modello "
            "meno caro, una volta per sessione.",
            "La qualita' sulle richieste classificate semplici, piu' il "
            "prompt caching di quelle sessioni: le cache sono legate al "
            "modello e cambiarlo le azzera.",
            modello_economico,
            "",
        ),
        (
            f"+ tutto su {MODELLO_MINIMO}",
            "Ogni richiesta al modello piu' economico del listino, senza "
            "giudizio di difficolta'.",
            "Non e' piu' lo stesso prodotto: e' un'altra risposta a un prezzo "
            "diverso. Serve da confine inferiore, non da configurazione.",
            predefinita,
            MODELLO_MINIMO,
        ),
    ]

    for etichetta, descrizione, in_cambio, applica, modello in gradini:
        carichi = _su_modello(scenari, modello) if modello else scenari
        costo, (out, scritti, letti, pieno) = await somma(applica, carichi, etichetta)
        report.steps.append(
            CeilingStep(
                etichetta=etichetta,
                descrizione=descrizione,
                in_cambio=in_cambio,
                cost_usd=costo,
                output_tokens=out,
                cache_write_tokens=scritti,
                cache_read_tokens=letti,
                full_price_tokens=pieno,
            )
        )

    ultimo = report.steps[-1] if report.steps else None
    if ultimo is not None:
        report.steps.append(
            CeilingStep(
                etichetta="+ modalita' batch",
                descrizione=(
                    "La Message Batches API sconta della meta' input e output, e si "
                    "compone con la cache. Calcolato, non simulato: uno sconto sulla "
                    "fattura non cambia quanti token passano."
                ),
                in_cambio=(
                    "L'immediatezza. Le richieste diventano asincrone, con esito entro "
                    "24 ore: utilizzabile su traffico che puo' aspettare, non dietro "
                    "un'interfaccia che aspetta una risposta."
                ),
                cost_usd=ultimo.cost_usd * SCONTO_BATCH,
                output_tokens=ultimo.output_tokens,
                cache_write_tokens=ultimo.cache_write_tokens,
                cache_read_tokens=ultimo.cache_read_tokens,
                full_price_tokens=ultimo.full_price_tokens,
            )
        )
        grezzo = _pavimento(ultimo)
        report.floor = CeilingFloor(
            output_usd=grezzo.output_usd * SCONTO_BATCH,
            input_nuovo_usd=grezzo.input_nuovo_usd * SCONTO_BATCH,
            riletture_usd=grezzo.riletture_usd * SCONTO_BATCH,
        )
    return report


# --- da cosa dipende davvero il numero ------------------------------------


@dataclass
class RepetitionPoint:
    """Un punto della curva: quanto si risparmia a una data ripetitivita'."""

    uniche: int
    ripetizioni: int
    richieste: int
    baseline_usd: float
    cost_usd: float

    @property
    def saved_ratio(self) -> float:
        return (
            (self.baseline_usd - self.cost_usd) / self.baseline_usd
            if self.baseline_usd
            else 0.0
        )


async def measure_repetition_curve(*, live: bool = False) -> list[RepetitionPoint]:
    """Il risparmio in funzione di quanto il carico si ripete.

    E' la misura che rimette la domanda al posto giusto. "Quanto risparmia
    EcoTokens" non ha una risposta sola, perche' non e' una proprieta' del
    gateway: e' una proprieta' del **traffico**. Su richieste tutte diverse la
    sola leva e' il prefisso condiviso; su richieste che si ripetono entra la
    cache esatta, che non sconta il prezzo di un token - lo azzera.

    La curva sale verso il 100% e non lo tocca mai: la prima richiesta va
    comunque pagata. Serve a rispondere a "arriveremo al 99%?" con una
    condizione verificabile invece che con una promessa.
    """
    from .bench import _abilita_prompt, _run_scenario, make_settings
    from .workloads import scenario_ripetitivo

    forme = ((12, 1), (6, 2), (4, 3), (3, 5), (2, 10), (1, 20), (1, 50))
    punti: list[RepetitionPoint] = []
    for uniche, ripetizioni in forme:
        scenario = scenario_ripetitivo(uniche=uniche, ripetizioni=ripetizioni)
        prima = await _run_scenario(scenario, make_settings(None), "senza", live=live)
        dopo = await _run_scenario(
            scenario, make_settings(_abilita_prompt), "con", live=live
        )
        punti.append(
            RepetitionPoint(
                uniche=uniche,
                ripetizioni=ripetizioni,
                richieste=scenario.size,
                baseline_usd=prima.cost_usd,
                cost_usd=dopo.cost_usd,
            )
        )
    return punti


def ripetizioni_per_obiettivo(punti: list[RepetitionPoint], obiettivo: float) -> int | None:
    """Quante ripetizioni della stessa richiesta servono per un dato risparmio.

    Sui carichi tutti uguali il costo con gateway e' costante - una sola
    chiamata arriva all'API, le altre le serve la cache - mentre quello senza
    cresce in proporzione. Il rapporto e' percio' ricavabile invece che
    provato a tentativi: si prende il punto piu' ripetitivo misurato e si
    estrapola il costo per richiesta da li'.

    Restituisce ``None`` se non c'e' un punto adatto da cui estrapolare.
    """
    ripetitivi = [p for p in punti if p.uniche == 1 and p.richieste > 1 and p.cost_usd > 0]
    if not ripetitivi:
        return None
    riferimento = max(ripetitivi, key=lambda p: p.richieste)
    per_richiesta = riferimento.baseline_usd / riferimento.richieste
    if per_richiesta <= 0:
        return None
    # costo_con / (n * per_richiesta) <= 1 - obiettivo
    necessarie = riferimento.cost_usd / ((1.0 - obiettivo) * per_richiesta)
    return max(1, int(necessarie) + 1)
