"""Cosa questo progetto **dà per vero** senza averlo verificato.

Tutte le misure di EcoTokens girano contro un simulatore. È una scelta, non una
mancanza: i test non devono richiedere rete, e un banco di prova che chiama
l'API vera costa soldi a ogni esecuzione e dà numeri diversi ogni volta. Ma ha
un prezzo preciso, e questo file è il prezzo scritto.

Un simulatore è un insieme di assunzioni sul comportamento dell'originale.
Finché restano implicite, «il gateway risparmia il 72%» significa «risparmia il
72% *se* tutte quelle assunzioni sono giuste», e nessuno sa quante siano né
quali. Elencarle non le verifica — ma trasforma un dubbio senza contorni in una
lista finita, dove ogni voce dice **cosa risulterebbe diverso se fosse
sbagliata** e **con quale comando controllarla** il giorno in cui c'è una
chiave.

Tre livelli, e la differenza fra i primi due è tutta:

* ``documentata`` — sta nella documentazione ufficiale dell'API. Può essere
  invecchiata, non inventata.
* ``dichiarata`` — un modello scelto da noi, plausibile e non verificato. Sono
  le voci che possono spostare un numero del README.
* ``verificata`` — confrontata con l'API vera. Oggi **nessuna**, e finché resta
  così va detto.
"""

from __future__ import annotations

from dataclasses import dataclass

DOCUMENTATA = "documentata"
DICHIARATA = "dichiarata"
VERIFICATA = "verificata"


@dataclass(frozen=True)
class Assunzione:
    nome: str
    valore: str
    fonte: str
    dove: str
    # La domanda che rende utile la voce: se questa fosse sbagliata, quale
    # numero di questo progetto sarebbe sbagliato?
    cosa_cambia: str
    come_verificarla: str


ASSUNZIONI: list[Assunzione] = [
    # --- quelle che vengono dalla documentazione --------------------------
    Assunzione(
        nome="Tariffe dei modelli",
        valore="Opus 5 $5/$25, Sonnet 5 $2/$10, Haiku 4.5 $1/$5 per milione",
        fonte=DOCUMENTATA,
        dove="pricing.MODELS",
        cosa_cambia=(
            "Ogni cifra in dollari del progetto. Non i rapporti fra le "
            "configurazioni, che restano validi: il confronto A/B usa le stesse "
            "tariffe da tutte e due le parti."
        ),
        come_verificarla="Confronto con la fattura reale, o con la pagina dei prezzi.",
    ),
    Assunzione(
        nome="Moltiplicatori della cache",
        valore="lettura 0,1x - scrittura 1,25x (5 min) e 2x (1 ora)",
        fonte=DOCUMENTATA,
        dove="pricing.CACHE_READ_MULTIPLIER, pricing.CACHE_WRITE_MULTIPLIER",
        cosa_cambia=(
            "Il punto di pareggio della cache, che è a 2 richieste (5 min) o 3 "
            "(1 ora). Se la scrittura costasse di più, marcare un prefisso che "
            "viene riletto una volta sola diventerebbe una perdita."
        ),
        come_verificarla="`ecotokens bench --live`: i campi usage li riporta l'API.",
    ),
    Assunzione(
        nome="Soglie minime di cache",
        valore="Opus 5 = 512, Sonnet 5 = 1024, Haiku 4.5 = 4096 token",
        fonte=DOCUMENTATA,
        dove="pricing.MODELS.cache_min_tokens",
        cosa_cambia=(
            "Quando il pianificatore rinuncia a marcare. Non sono monotone, ed è "
            "il motivo per cui declassare a Haiku può spegnere la cache in "
            "silenzio: sotto soglia non si forma e **l'API non emette alcun "
            "errore**. Se una soglia fosse più bassa del previsto, si starebbe "
            "rinunciando a un risparmio disponibile."
        ),
        come_verificarla=(
            "`--live` con un prompt appena sotto e appena sopra la soglia: "
            "`cache_creation_input_tokens` resta a zero sotto."
        ),
    ),
    Assunzione(
        nome="Quattro breakpoint al massimo",
        valore="al più 4 blocchi con `cache_control` per richiesta",
        fonte=DOCUMENTATA,
        dove="pipeline.cache_planner",
        cosa_cambia=(
            "Il quinto verrebbe rifiutato con un 400 a metà richiesta, cioè molto "
            "più tardi e molto meno chiaramente di un rifiuto locale."
        ),
        come_verificarla="`--live` con cinque breakpoint: deve tornare un 400.",
    ),
    Assunzione(
        nome="Finestra di lookback di 20 blocchi",
        valore="un breakpoint non trova voci di cache oltre 20 blocchi indietro",
        fonte=DOCUMENTATA,
        dove="simulator.LOOKBACK_BLOCKS",
        cosa_cambia=(
            "Il piazzamento dei breakpoint intermedi nei turni lunghi con molti "
            "tool result. Se la finestra fosse più corta, quei breakpoint "
            "fallirebbero **in silenzio**: si pagherebbe la scrittura senza mai "
            "rileggere."
        ),
        come_verificarla=(
            "`--live` su una conversazione con più di 20 blocchi fra un "
            "breakpoint e il precedente."
        ),
    ),
    Assunzione(
        nome="I parametri rimossi danno 400",
        valore="temperature, top_p, top_k, prefill assistant, n > 1",
        fonte=DOCUMENTATA,
        dove="translate.to_anthropic",
        cosa_cambia=(
            "Se non fossero rifiutati, il gateway starebbe scartando parametri "
            "che il client avrebbe potuto usare - cioè cambiando la risposta "
            "senza motivo."
        ),
        come_verificarla="`--live` mandandone uno: deve tornare un 400.",
    ),
    # --- quelle che abbiamo scelto noi ------------------------------------
    Assunzione(
        nome="Quanti tool result conserva la potatura",
        valore="3 risultati recenti (`clear_tool_uses_20250919`)",
        fonte=DICHIARATA,
        dove="simulator.KEPT_TOOL_RESULTS",
        cosa_cambia=(
            "Quanto risparmia la potatura del contesto, e quindi il confronto fra "
            "le strategie che fa `ecotokens pruning`. Un valore più alto "
            "significa meno risparmio di quello che il banco riporta."
        ),
        come_verificarla=(
            "`--live` con la beta attiva, contando i tool result rimasti nel "
            "prompt restituito."
        ),
    ),
    Assunzione(
        nome="Effetto dell'effort sui token generati",
        valore="low 0,4x - medium 0,7x - high 1,0x - xhigh 1,6x - max 2,6x",
        fonte=DICHIARATA,
        dove="simulator.EFFORT_OUTPUT_MULTIPLIER",
        cosa_cambia=(
            "Tutto il risparmio attribuito all'abbassamento dell'effort, che è "
            "il primo livello del router e quello considerato sicuro. Il verso "
            "è certo - meno effort, meno token di ragionamento - ma il rapporto "
            "fra i livelli dipende dal compito. Senza un modello dichiarato il "
            "simulatore darebbe sempre la stessa lunghezza e lo stadio "
            "risulterebbe inutile **per costruzione**, che è il difetto peggiore "
            "di tutti."
        ),
        come_verificarla=(
            "`--live` sullo stesso prompt ai cinque livelli, confrontando "
            "`output_tokens`."
        ),
    ),
    Assunzione(
        nome="Stima locale dei token",
        valore="3,6 caratteri per token, più 4 per blocco e 8 per messaggio",
        fonte=DICHIARATA,
        dove="tokens._CHARS_PER_TOKEN",
        cosa_cambia=(
            "Il preventivo del tetto di spesa e la scelta di marcare o no un "
            "prefisso vicino alla soglia. Non il conto finale, che viene sempre "
            "da `response.usage`. Lo scarto contro `count_tokens` è già "
            "registrato e visibile nella console."
        ),
        come_verificarla="Già misurabile: la console mostra la taratura.",
    ),
    Assunzione(
        nome="Lunghezza di una risposta tipica",
        valore="600 token di output prima dell'effetto dell'effort",
        fonte=DICHIARATA,
        dove="simulator.OUTPUT_TIPICO",
        cosa_cambia=(
            "Il peso relativo di input e output in ogni misura. Con risposte più "
            "lunghe il risparmio percentuale del gateway **scende**, perché tutte "
            "le sue leve agiscono sul prompt e nessuna sull'output."
        ),
        come_verificarla="`--live`: la lunghezza media reale del carico dell'utente.",
    ),
]


def per_fonte(fonte: str) -> list[Assunzione]:
    return [a for a in ASSUNZIONI if a.fonte == fonte]


def riepilogo() -> str:
    """Una riga sola, da mettere sotto qualunque numero di questo progetto."""
    dichiarate = len(per_fonte(DICHIARATA))
    verificate = len(per_fonte(VERIFICATA))
    return (
        f"{len(ASSUNZIONI)} assunzioni sul comportamento dell'API: "
        f"{len(per_fonte(DOCUMENTATA))} dalla documentazione, "
        f"{dichiarate} dichiarate da noi, {verificate} verificate contro l'API vera."
    )
