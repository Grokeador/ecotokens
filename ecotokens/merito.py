"""`ecotokens merito`: quanto aggiunge il gateway a chi la cache se la mette da solo.

I quattro numeri piu' citati di questo progetto - +52% su un ciclo agentico,
+87,2% su domande ripetute, +22,6% su una chat che cresce, -0,2% su molti utenti
a turno singolo - **non erano ricalcolabili da nessun comando**. Venivano da uno
script scritto una volta e buttato, e vivevano in due copie a mano: nel README e
in `consiglia.MERITO`.

Il difetto non e' che fossero sbagliati. E' che non potevano accorgersene. Il 30
agosto 2026 l'effetto dell'effort e' stato misurato invece che assunto e i
moltiplicatori sono cambiati; rieseguire `ablate` ha mostrato che il valore
aggiunto del gateway era sceso di un quarto, mentre questi quattro sono rimasti
identici - perche' nessun comando li produceva. **Una misura che nessun comando
ricalcola non invecchia male: invecchia invisibile.**

Questo modulo e' quel comando.

Le tre colonne, e la differenza fra la seconda e la terza e' tutto il punto:

* **totale** - contro chi non usa affatto la cache. E' un fantoccio: nessuno
  integra l'API cosi'. Serve solo a poter isolare la colonna successiva.
* **di cui Anthropic** - lo sconto che chiunque ottiene mettendo un
  `cache_control` sul proprio system prompt. Una riga di codice, la pratica
  documentata, nessun gateway. **Non e' merito di EcoTokens**, e prendersela e'
  la meta' piu' grossa del numero.
* **di cui EcoTokens** - quello che resta. E' l'unica colonna che risponde a
  «conviene installarlo».

I carichi stanno qui e non in `all_scenarios` di proposito: aggiungerne uno la'
invaliderebbe i confronti storici del banco (`CORPUS_VERSION`), che e' una
trappola gia' calpestata. Questi cinque servono a una domanda sola e cambiano
con essa.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .bench import _REALIZZA, BenchRun, Measurement, _run_scenario, make_settings
from .varianti import ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA
from .workloads import Scenario, scenario_agente, scenario_chat, scenario_ripetitivo

# La configurazione misurata e' l'ultimo gradino **che non cambia la risposta**,
# cioe' il profilo `prudente` che il gateway installa di suo. Non il gradino
# finale: quello declassa il modello, e un risparmio ottenuto rispondendo con un
# modello diverso non appartiene a questa tabella - e' un'altra risposta a un
# altro prezzo. Il nome e la funzione si prendono dalla stessa mappa che usa
# l'ablazione, cosi' i due comandi non possono descrivere configurazioni
# diverse credendo di descrivere la stessa.
VARIANTE = ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA

# Quante volte si chiede al testimone prima di rinunciare.
#
# Misurato su 42 sonde il 30 agosto 2026: la rilettura di un prefisso appena
# scritto riesce **24 volte su 42**, cioe' il 57%, e ne' la pausa fra scrittura
# e rilettura ne' la dimensione del prefisso cambiano il tasso (3/6 contro 3/6;
# 4/8 contro 4/8). Con p=0,57 tre tentativi falliscono tutti l'8% delle volte -
# ed e' successo due volte di seguito, bloccando una misura che non aveva
# niente che non andasse. Cinque portano il rifiuto ingiustificato all'1,5%, e
# costano due chiamate corte ciascuno.
TENTATIVI_TESTIMONE = 5

# Il turno singolo non esiste fra gli scenari condivisi, e senza di esso la
# tabella perderebbe la riga piu' scomoda - quella dove il gateway **non**
# conviene. Un elenco di casi favorevoli non e' una misura, e' una brochure.
def scenario_turno_singolo(utenti: int = 12) -> Scenario:
    """Molti utenti diversi, stesso system prompt, una domanda a testa.

    E' il caso in cui chi marca il proprio system prompt cattura gia' tutto:
    il prefisso condiviso e' esattamente quello che lui ha marcato, e la
    conversazione non cresce perche' non c'e' conversazione.
    """
    system = {
        "role": "system",
        "content": "Sei l'assistente del prodotto. Rispondi con precisione. " * 300,
    }
    scenario = Scenario(
        name="turno-singolo",
        description=f"{utenti} utenti diversi, stesso system, una domanda ciascuno",
    )
    for indice in range(utenti):
        scenario.requests.append(
            {
                "model": "claude-opus-5",
                "messages": [
                    system,
                    {
                        "role": "user",
                        "content": (
                            f"Domanda dell'utente numero {indice}: come faccio a "
                            f"configurare la funzione {indice} del prodotto?"
                        ),
                    },
                ],
            }
        )
    return scenario


def carichi() -> list[tuple[str, Scenario]]:
    """I cinque regimi, con l'etichetta che compare nella tabella."""
    return [
        ("ciclo agentico, 20 turni con tool", scenario_agente(turns=20, tool_per_turno=3)),
        ("ciclo agentico, 8 chiamate per turno", scenario_agente(turns=6, tool_per_turno=8)),
        ("domande che si ripetono", scenario_ripetitivo()),
        ("una conversazione che cresce, 8 turni", scenario_chat(turns=8)),
        ("molti utenti, stesso system, turno singolo", scenario_turno_singolo()),
    ]


@dataclass
class Riga:
    etichetta: str
    richieste: int
    piena_usd: float
    ingenua_usd: float
    nostro_usd: float

    @property
    def totale(self) -> float:
        """Risparmio contro chi non usa la cache. Il fantoccio."""
        return (self.piena_usd - self.nostro_usd) / self.piena_usd if self.piena_usd else 0.0

    @property
    def di_anthropic(self) -> float:
        """La parte che si otterrebbe comunque, senza installare niente."""
        return (self.piena_usd - self.ingenua_usd) / self.piena_usd if self.piena_usd else 0.0

    @property
    def di_ecotokens(self) -> float:
        """Cio' che il gateway aggiunge, e l'unica colonna che decide.

        Il denominatore e' la baseline **ingenua**, non quella piena: la
        domanda e' «quanto risparmio in piu' rispetto a dove sarei senza», e
        rapportarla al fantoccio la diluirebbe fino a sembrare piccola quando
        e' grande, e viceversa.
        """
        if not self.ingenua_usd:
            return 0.0
        return (self.ingenua_usd - self.nostro_usd) / self.ingenua_usd


@dataclass
class Rapporto:
    righe: list[Riga]
    modo: str
    # Zero quando il testimone e' caduto: la misura non e' stata eseguita.
    testimone: int = -1

    @property
    def simulato(self) -> bool:
        return self.modo != "live"

    @property
    def eseguita(self) -> bool:
        return bool(self.righe)


async def calcola(*, live: bool = False, solo: str | None = None) -> Rapporto:
    """Esegue i carichi con il profilo completo e misura le tre colonne.

    `solo` filtra per sottostringa dell'etichetta, e serve alla misura vera: i
    cinque carichi insieme fanno sessanta richieste con prompt agentici che
    crescono fino a decine di migliaia di token, cioe' qualche dollaro. Partire
    da un carico solo per calibrare e' la stessa prudenza che `bench` consiglia
    nel proprio aiuto, e qui costa meno ripeterla che scoprirla.
    """
    run = BenchRun(
        id=uuid.uuid4().hex[:12],
        label="merito",
        mode="live" if live else "simulato",
        created_at=time.time(),
    )
    # La guardia, e vale i pochi centesimi che costa.
    #
    # Senza, questi carichi spendono qualche dollaro per concludere che il
    # gateway non serve a niente, in un momento in cui la cache non stava
    # rileggendo - e la conclusione descriverebbe il momento, non il gateway.
    #
    # **Tre tentativi, non uno**, e il perche' e' misurato. La rilettura di un
    # prefisso appena scritto riesce circa tre volte su cinque (11 su 18 sonde
    # il 30 agosto 2026, e la pausa fra scrittura e rilettura non cambia
    # niente: 3/6 contro 3/6). Con quel tasso un testimone a colpo singolo
    # rifiuterebbe di misurare **due volte su cinque senza motivo**, cioe'
    # sarebbe piu' severo della cosa che sorveglia - lo stesso difetto che
    # rimprovera agli altri. Tre tentativi portano il rifiuto ingiustificato
    # sotto il 7%.
    testimone = -1
    if live:
        import anthropic

        from .config import intestazioni_upstream
        from .verifica import testimone_di_cache

        cliente = anthropic.AsyncAnthropic(
            default_headers=intestazioni_upstream() or None
        )
        for _ in range(TENTATIVI_TESTIMONE):
            testimone = await testimone_di_cache(cliente, "claude-opus-5")
            if testimone > 0:
                break
        if testimone <= 0:
            return Rapporto(righe=[], modo=run.mode, testimone=0)

    righe: list[Riga] = []
    scelti = [
        (etichetta, scenario)
        for etichetta, scenario in carichi()
        if solo is None or solo.lower() in etichetta.lower()
    ]
    if not scelti:
        raise ValueError(
            f"nessun carico corrisponde a {solo!r}: "
            + ", ".join(nome for nome, _ in carichi())
        )
    for etichetta, scenario in scelti:
        misura: Measurement = await _run_scenario(
            scenario, make_settings(_REALIZZA[VARIANTE]), VARIANTE, live=live
        )
        run.measurements.append(misura)
        righe.append(
            Riga(
                etichetta=etichetta,
                richieste=misura.requests,
                piena_usd=misura.baseline_piena_usd,
                ingenua_usd=misura.baseline_ingenua_usd,
                nostro_usd=misura.cost_usd,
            )
        )
    return Rapporto(righe=righe, modo=run.mode, testimone=testimone)
