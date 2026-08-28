"""Ritenzione: l'informazione che servira' e' ancora nel prompt?

E' lo strumento che mancava, ed e' quello che tiene spente quattro funzioni del
gateway: memoria, cache semantica, declassamento del modello, effort minimo.
Tutte e quattro hanno un costo misurabile e un beneficio che il banco non
legge, quindi accenderle puo' solo peggiorare ogni numero. Non sono quattro
problemi: e' uno solo, e questo file ne affronta la meta' affrontabile senza
rete.

**La distinzione che rende il problema trattabile.** "La risposta e' giusta?"
non e' misurabile qui: servirebbe un modello, e un modello che giudica un altro
modello e' un metro con opinioni. Ma quella domanda ne nasconde una piu'
piccola, che e' deterministica:

    l'informazione necessaria per rispondere e' arrivata fino al prompt?

Se non c'e', nessun modello puo' rispondere e la funzione ha fallito, senza
ambiguita' e senza giudizio. Se c'e', resta da sapere se il modello l'ha usata
bene - e quella meta' richiede `--live`. La prima meta' costa zero, si esegue a
ogni commit e copre il caso che interessa davvero: potare e riassumere buttano
via cose, e questo dice **quali**.

**Come e' fatta la prova.** Uno scenario di ritenzione pianta un'informazione
in un turno lontano - un nome, una versione, un vincolo - e la richiede molti
turni dopo. Si guarda il prompt che il gateway sta per spedire e si cerca
l'informazione. Nessuna chiamata all'API, nessun modello, nessuna soglia da
tarare: c'e' o non c'e'.

Il confronto interessante non e' mai un numero solo. Serve sempre in coppia:
la stessa conversazione con e senza lo stadio in esame, perche' "l'89% dei
fatti sopravvive" da solo non dice niente, mentre "senza potatura 100%, con
potatura 40%, con potatura e memoria 95%" dice cosa fare.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import Settings

# --- gli scenari ----------------------------------------------------------


@dataclass
class Impianto:
    """Un'informazione piantata in un turno e richiesta molti turni dopo."""

    # Cosa si pianta, e la domanda che la richiede.
    fatto: str
    domanda: str
    # Il segno da cercare nel prompt. Non e' il fatto intero: un riassunto
    # fedele puo' riformularlo, e pretendere la frase alla lettera bocciarebbe
    # una compattazione riuscita. Si cerca la parte che non puo' cambiare -
    # un nome, un numero, un percorso - perche' quella o sopravvive o no.
    segno: str
    # A quale turno viene piantata.
    turno: int


@dataclass
class ScenarioRitenzione:
    name: str
    description: str
    impianti: list[Impianto]
    turni: int
    # Testo di contorno per ogni turno: serve a far crescere la conversazione
    # fino a far scattare potatura e riassunto, che sono cio' che si misura.
    riempimento: str


@dataclass
class EsitoRitenzione:
    scenario: str
    variante: str
    sopravvissuti: list[str] = field(default_factory=list)
    perduti: list[str] = field(default_factory=list)
    # Indicativi, e non confrontabili fra varianti potate: due esecuzioni
    # possono trovarsi in punti diversi del ciclo di compattazione, e chi
    # riassume un turno prima ha un prompt molto piu' corto per un motivo che
    # non ha niente a che vedere con lo stadio in esame. Misurando si e' visto
    # un caso in cui la memoria - che **aggiunge** testo - risultava piu'
    # economica della sola potatura, per questo.
    prompt_tokens: int = 0
    # Riassunti **nuovi**, non riusi: e' l'evento che collassa la cronologia e
    # sposta il prompt di un ordine di grandezza. Contare anche i riusi darebbe
    # lo stesso numero alle due varianti e nasconderebbe proprio la differenza
    # che rende i loro token non confrontabili.
    riassunti_nuovi: int = 0

    @property
    def quota(self) -> float:
        totale = len(self.sopravvissuti) + len(self.perduti)
        return len(self.sopravvissuti) / totale if totale else 0.0


# Il contorno e' volutamente lungo e senza informazione: serve a riempire la
# finestra, non a dire qualcosa. Se contenesse fatti, un riassunto potrebbe
# tenerli al posto di quelli piantati e la misura direbbe la cosa sbagliata.
_RIEMPIMENTO = (
    "Continua con l'analisi del modulo e descrivi i passaggi intermedi, "
    "elencando le alternative considerate e le ragioni per cui sono state "
    "scartate, poi passa al punto successivo. "
) * 12


def scenari_di_ritenzione() -> list[ScenarioRitenzione]:
    """I casi in cui si perde qualcosa, scelti perche' si perde davvero."""
    return [
        ScenarioRitenzione(
            name="identita",
            description="Dati dell'utente dati una volta e richiesti molto dopo",
            turni=24,
            riempimento=_RIEMPIMENTO,
            impianti=[
                Impianto(
                    fatto="Mi chiamo Jorge e lavoro su Windows 10.",
                    domanda="Come mi chiamo e su che sistema lavoro?",
                    segno="Jorge",
                    turno=1,
                ),
                Impianto(
                    fatto="Il progetto si chiama EcoTokens e usa Python 3.13.",
                    domanda="Che versione di Python usa il progetto?",
                    segno="3.13",
                    turno=2,
                ),
            ],
        ),
        ScenarioRitenzione(
            name="parole-diverse",
            description="Fatti telegrafici e domande che non ne condividono le parole",
            turni=20,
            riempimento=_RIEMPIMENTO,
            impianti=[
                # Il caso che il recupero per pertinenza non puo' vincere. La
                # ricerca dei fatti e' lessicale: cerca le parole della domanda
                # dentro i fatti. Un fatto scritto telegrafico - che e' come si
                # devono scrivere, perche' si pagano a ogni richiesta - ha
                # pochissime parole da far combaciare, e una domanda che usa
                # sinonimi non ne trova nessuna. Le due cose giuste da fare,
                # fatte insieme, si rompono a vicenda.
                Impianto(
                    fatto="Porta: 8443",
                    domanda="Su quale interfaccia devo mettermi in ascolto?",
                    segno="8443",
                    turno=2,
                ),
                Impianto(
                    fatto="Budget: 12 USD",
                    domanda="Quanto posso spendere al massimo?",
                    segno="12 USD",
                    turno=5,
                ),
                Impianto(
                    fatto="HTTP: httpx2, mai requests",
                    domanda="Che client di rete uso per le chiamate?",
                    segno="httpx2",
                    turno=9,
                ),
            ],
        ),
        ScenarioRitenzione(
            name="vincoli",
            description="Vincoli dichiarati all'inizio e ancora validi alla fine",
            turni=30,
            riempimento=_RIEMPIMENTO,
            impianti=[
                Impianto(
                    fatto="Non usare mai la libreria requests: il progetto sta su httpx2.",
                    domanda="Quale libreria HTTP devo usare?",
                    segno="httpx2",
                    turno=1,
                ),
                Impianto(
                    fatto="Il budget massimo per questa attivita' e' 12 dollari.",
                    domanda="Qual e' il budget massimo?",
                    segno="12 dollari",
                    turno=6,
                ),
                Impianto(
                    fatto="La porta di ascolto deve restare la 8443, non la 8000.",
                    domanda="Su quale porta deve ascoltare?",
                    segno="8443",
                    turno=14,
                ),
            ],
        ),
    ]


# --- la misura ------------------------------------------------------------


def _testo_del_prompt(params: dict[str, Any]) -> str:
    """Tutto il testo che il gateway sta per spedire, in una stringa sola.

    Compreso il `system` e i blocchi strutturati: un fatto salvato dalla
    memoria arriva in coda ai messaggi, uno salvato dal riassunto arriva dentro
    un blocco di testo, e cercarlo in un posto solo darebbe un falso negativo
    proprio sullo stadio che ha funzionato.
    """
    pezzi: list[str] = []

    def raccogli(valore: Any) -> None:
        if isinstance(valore, str):
            pezzi.append(valore)
        elif isinstance(valore, dict):
            for chiave, dentro in valore.items():
                if chiave not in {"type", "role", "cache_control"}:
                    raccogli(dentro)
        elif isinstance(valore, list):
            for dentro in valore:
                raccogli(dentro)

    raccogli(params.get("system"))
    raccogli(params.get("messages"))
    return "\n".join(pezzi)


def _presente(testo: str, segno: str) -> bool:
    """Il segno c'e', a meno di maiuscole e spazi multipli.

    Non si va oltre: una ricerca "intelligente" - sinonimi, distanza fra
    stringhe - introdurrebbe una soglia, e una soglia e' un giudizio. Il valore
    di questa misura sta nel non averne.
    """
    normale = re.sub(r"\s+", " ", testo).casefold()
    return re.sub(r"\s+", " ", segno).casefold() in normale


# --- esecuzione -----------------------------------------------------------

# Le configurazioni messe a confronto. Una misura sola non dice niente: "l'89%
# dei fatti sopravvive" e' un numero senza termine di paragone. Serve la
# terna - intatto, potato, potato con memoria - perche' e' la differenza fra
# le tre a dire cosa accendere.
VARIANTI: list[tuple[str, str]] = [
    ("intatto", "niente potatura ne' riassunto: il limite superiore"),
    ("potato", "potatura e riassunto accesi, memoria spenta"),
    ("potato + memoria", "gli stessi tagli, memoria con recupero pertinente (in coda)"),
    ("potato + memoria stabile", "gli stessi tagli, memoria nel prefisso in cache"),
]


def _configura(variante: str) -> Settings:
    settings = Settings(profilo="prudente")
    settings.storage.path = ":memory:"
    settings.cache_planner.enabled = True
    settings.exact_cache.enabled = False  # servirebbe la risposta di prima, non il prompt
    settings.semantic_cache.enabled = False
    settings.budget.enabled = False

    intatto = variante == "intatto"
    settings.context.enabled = not intatto
    settings.context.local_compaction = not intatto
    settings.memory.enabled = variante.startswith("potato + memoria")
    # Esplicito in **entrambi** i rami, non solo in uno. Dedurre il primo dal
    # default e' quello che e' appena successo: cambiando il default a
    # "stabile", la variante che si chiama "memoria" ha smesso in silenzio di
    # misurare il recupero per pertinenza, pur continuando a chiamarsi cosi'.
    # Una variante di misura che segue una configurazione non e' una variante.
    settings.memory.retrieval = "stabile" if variante.endswith("stabile") else "pertinente"
    if not intatto:
        # Soglie basse di proposito: senza, su ventiquattro turni corti non
        # scatterebbe niente e la misura direbbe "non si perde nulla" avendo
        # semplicemente non fatto nulla. Il rischio qui e' misurare l'inazione.
        settings.context.trigger_ratio = 0.002
        settings.context.hard_ratio = 0.004
        settings.context.keep_recent_messages = 4
    return settings


async def _esegui(
    scenario: ScenarioRitenzione, variante: str, *, live: bool
) -> EsitoRitenzione:
    """Fa vivere la conversazione, poi chiede e guarda cosa e' arrivato."""
    import anthropic
    import httpx2

    from .api.schemas import ChatCompletionRequest
    from .server import Gateway
    from .simulator import create_stub

    settings = _configura(variante)
    gateway = Gateway(settings)
    if not live:
        stub_app, _ = create_stub()
        gateway.client = anthropic.AsyncAnthropic(
            api_key="ritenzione",
            base_url="http://simulatore",
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.ASGITransport(app=stub_app)
            ),
        )
    await gateway.startup()

    esito = EsitoRitenzione(scenario=scenario.name, variante=variante)
    piantati = {impianto.turno: impianto for impianto in scenario.impianti}
    storia: list[dict[str, Any]] = [
        {"role": "system", "content": "Sei un assistente che segue le istruzioni date."}
    ]

    try:
        sessione = None
        for turno in range(1, scenario.turni + 1):
            impianto = piantati.get(turno)
            storia.append(
                {"role": "user", "content": impianto.fatto if impianto else scenario.riempimento}
            )
            risposta, ctx = await gateway.complete(
                ChatCompletionRequest.model_validate(
                    {"model": "claude-opus-5", "messages": list(storia), "max_tokens": 400}
                )
            )
            sessione = ctx.session_id
            storia.append(
                {
                    "role": "assistant",
                    "content": risposta["choices"][0]["message"]["content"] or "ok",
                }
            )

        if settings.memory.enabled:
            # L'estrattore qui e' **perfetto per ipotesi**: i fatti vengono
            # messi nel deposito senza passare dal modello, che nel simulatore
            # produrrebbe testo inventato. Quindi il numero della memoria e' un
            # limite superiore - dice se il fatto, una volta estratto, arriva
            # fino al prompt - e non dice niente sulla qualita' dell'estrazione,
            # che si misura solo con --live. Dichiararlo e' parte della misura.
            await gateway.store.add_facts(
                sessione, [impianto.fatto for impianto in scenario.impianti]
            )

        # Le domande si fanno **in fila**, ognuna dopo la risposta alla
        # precedente. Farle tutte a partire dalla stessa storia sembrerebbe
        # equivalente e non lo e': sono biforcazioni, e il gateway le riconosce
        # come conversazioni diverse - la seconda non e' un superset della
        # prima, che nel frattempo si e' allungata. Cosi' facendo si misurava
        # una perdita di memoria che era invece un cambio di sessione.
        for impianto in scenario.impianti:
            storia.append({"role": "user", "content": impianto.domanda})
            risposta, ctx = await gateway.complete(
                ChatCompletionRequest.model_validate(
                    {"model": "claude-opus-5", "messages": list(storia), "max_tokens": 400}
                )
            )
            esito.prompt_tokens += ctx.usage.total_prompt_tokens
            esito.riassunti_nuovi += sum(
                1 for nota in ctx.notes if "riassunto esteso" in nota
            )
            if _presente(_testo_del_prompt(ctx.params), impianto.segno):
                esito.sopravvissuti.append(impianto.segno)
            else:
                esito.perduti.append(impianto.segno)
            storia.append(
                {
                    "role": "assistant",
                    "content": risposta["choices"][0]["message"]["content"] or "ok",
                }
            )
    finally:
        await gateway.shutdown()
    return esito


async def misura_ritenzione(*, live: bool = False) -> list[EsitoRitenzione]:
    """Ogni scenario sotto ogni variante, in ordine di lettura."""
    esiti: list[EsitoRitenzione] = []
    for scenario in scenari_di_ritenzione():
        for variante, _ in VARIANTI:
            esiti.append(await _esegui(scenario, variante, live=live))
    return esiti


# --- il costo delle due modalita' di memoria ------------------------------


@dataclass
class EsitoMemoria:
    modalita: str
    turni: int
    cost_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    full_price_tokens: int = 0


async def misura_memoria(turni: list[int] | None = None) -> list[EsitoMemoria]:
    """Quanto costa la memoria in coda contro la memoria nel prefisso.

    La potatura resta **spenta**. Non e' pigrizia: accendendola le due
    esecuzioni finiscono in punti diversi del ciclo di compattazione e la
    differenza di costo diventa illeggibile - si e' visto misurando la
    ritenzione, dove la memoria risultava piu' economica della sola potatura
    per il solo fatto di aver fatto scattare un riassunto un turno prima. Qui
    si isola la sola domanda che interessa: un blocco di fatti fermo dentro il
    prefisso si ripaga, e da quale turno.
    """
    import anthropic
    import httpx2

    from .api.schemas import ChatCompletionRequest
    from .server import Gateway
    from .simulator import create_stub

    fatti = [
        "Nome: Jorge",
        "SO: Windows 10",
        "Progetto: EcoTokens",
        "Python 3.13",
        "HTTP: httpx2, mai requests",
        "Budget: 12 USD",
        "Porta: 8443",
        "DB: SQLite WAL",
    ]
    esiti: list[EsitoMemoria] = []
    for modalita in ("pertinente", "stabile"):
        for quanti in turni or [5, 10, 20, 40]:
            settings = Settings(profilo="prudente")
            settings.storage.path = ":memory:"
            settings.memory.enabled = True
            settings.memory.retrieval = modalita
            settings.context.enabled = False
            settings.exact_cache.enabled = False
            settings.semantic_cache.enabled = False
            settings.budget.enabled = False

            gateway = Gateway(settings)
            stub_app, _ = create_stub()
            gateway.client = anthropic.AsyncAnthropic(
                api_key="memoria",
                base_url="http://simulatore",
                http_client=anthropic.DefaultAsyncHttpxClient(
                    transport=httpx2.ASGITransport(app=stub_app)
                ),
            )
            await gateway.startup()
            esito = EsitoMemoria(modalita=modalita, turni=quanti)
            try:
                storia: list[dict[str, Any]] = [
                    {"role": "system", "content": "Assistente tecnico. " * 120}
                ]
                sessione = None
                for indice in range(quanti):
                    storia.append(
                        {"role": "user", "content": f"Passo {indice}: continua l'analisi."}
                    )
                    risposta, ctx = await gateway.complete(
                        ChatCompletionRequest.model_validate(
                            {"model": "claude-opus-5", "messages": list(storia),
                             "max_tokens": 300}
                        )
                    )
                    if sessione is None:
                        sessione = ctx.session_id
                        # Estrattore perfetto per ipotesi, come nella ritenzione:
                        # qui interessa dove finiscono i fatti, non come nascono.
                        await gateway.store.add_facts(sessione, fatti)
                    esito.cost_usd += ctx.total_cost_usd
                    esito.cache_read_tokens += ctx.usage.cache_read_tokens
                    esito.cache_write_tokens += ctx.usage.cache_creation_tokens
                    esito.full_price_tokens += ctx.usage.input_tokens
                    storia.append(
                        {"role": "assistant",
                         "content": risposta["choices"][0]["message"]["content"] or "ok"}
                    )
            finally:
                await gateway.shutdown()
            esiti.append(esito)
    return esiti
