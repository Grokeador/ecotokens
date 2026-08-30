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

    @property
    def simulato(self) -> bool:
        return self.modo != "live"


async def calcola(*, live: bool = False) -> Rapporto:
    """Esegue i cinque carichi con il profilo completo e misura le tre colonne."""
    run = BenchRun(
        id=uuid.uuid4().hex[:12],
        label="merito",
        mode="live" if live else "simulato",
        created_at=time.time(),
    )
    righe: list[Riga] = []
    for etichetta, scenario in carichi():
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
    return Rapporto(righe=righe, modo=run.mode)
