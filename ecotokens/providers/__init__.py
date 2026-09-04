"""Adattatori verso i provider, dietro un'unica faccia.

Il gateway ha sempre parlato con un solo provider. Da qui in poi parla con
diversi, ma **non cambia forma dentro**: ogni adattatore espone la faccia
``client.messages.create(**parametri)`` e restituisce un ``Message`` Anthropic.
La pipeline, le tariffe, le cache e il registro non sanno chi c'e' a valle, e
non devono saperlo - e' quello che rende il multi-provider un'aggiunta invece
che una riscrittura.

La normalizzazione avviene **al confine**, non negli stadi: un adattatore che
restituisse una forma propria costringerebbe ogni stadio a conoscerla.
"""

from __future__ import annotations

from .base import AdattatoreBase, Conteggio, FacciaMessaggi
from .fabbrica import PROVIDER_NOTI, costruisci_client

__all__ = [
    "AdattatoreBase",
    "Conteggio",
    "FacciaMessaggi",
    "PROVIDER_NOTI",
    "costruisci_client",
]
