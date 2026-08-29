"""I nomi delle varianti di misura, e nient'altro.

Questo file esiste per una ragione sola, ed e' misurata. I nomi vivevano in
`bench.py` insieme al motore che esegue le misure, e `bench.py` importa
l'SDK Anthropic, il simulatore e i carichi: **6,67 secondi** solo per essere
caricato. Il quadro, che si vende come "si apre subito" e non misura niente,
ne aveva bisogno per leggere quattro stringhe - e ci metteva 8,4 secondi ad
aprirsi, contraddicendo la propria docstring.

Un modulo di sole costanti non importa niente: chi vuole i nomi paga i nomi,
chi vuole misurare paga il motore.

I nomi non sono decorativi. La scala dell'ablazione e' cumulativa e il
significato di ogni gradino dipende dalla sua posizione, quindi l'ordine di
`NOMI_ABLAZIONE` e' contenuto e non stile: `bench.py` costruisce da qui la
propria tabella, cosi' non esistono due elenchi che possano divergere.
"""

from __future__ import annotations

# --- il confronto A/B -----------------------------------------------------

# Tutti gli stadi spenti. Resta la sola traduzione verso l'API, che non e'
# un'ottimizzazione ma una necessita': senza, la richiesta verrebbe rifiutata.
BASELINE_VARIANT = "senza-gateway"
FULL_VARIANT = "con-gateway"


# --- la scala dell'ablazione ----------------------------------------------

# Il gradino che rappresenta "l'applicazione che si sarebbe scritta comunque":
# un `cache_control` in cima alla richiesta, che Anthropic offre a chiunque.
# E' il riferimento onesto per chi valuta se installare il gateway - non
# "quanto risparmio contro nessuna cache", che e' una domanda senza
# destinatari da quando quel gradino e' gratis.
RIFERIMENTO_MODERNO = "+ caching automatico"

# L'ultimo gradino che non tocca il contenuto delle risposte. Dichiarato per
# nome e non dedotto dalla posizione: la scala cambia, e un indice numerico si
# scollerebbe in silenzio dal significato.
ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA = "+ riscrittura prompt"

NOMI_ABLAZIONE: tuple[str, ...] = (
    BASELINE_VARIANT,
    RIFERIMENTO_MODERNO,
    "+ pianificatore EcoTokens",
    "+ potatura contesto",
    "+ cache esatta",
    "+ effort adattivo",
    ULTIMO_SENZA_CAMBIARE_LA_RISPOSTA,
    "+ effort sempre basso",
    "+ modello economico",
)

# L'ultimo gradino: quello che cambia modello, e da cui viene il grosso della
# differenza fra i due profili.
ULTIMO_GRADINO = NOMI_ABLAZIONE[-1]
