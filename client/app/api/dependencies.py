# -*- coding: utf-8 -*-
"""
app/api/dependencies.py
=======================
Singleton du moteur + registre des clients WebSocket + diffuseur
d'événements temps réel vers le frontend (roadmap §5.9).

Le moteur est créé une seule fois au chargement du module (config lue au
démarrage du processus) ; la tâche ``broadcast_events`` est démarrée par le
lifespan (app/api/lifespan.py) et pousse les événements du moteur (state,
log, transcript) vers tous les clients WebSocket connectés.
"""

import asyncio
import json

from fastapi import WebSocket

from app.core.config import load_config
from app.core.logging import get_logger
from app.engine.dictation import DictationEngine

log = get_logger()

# Intervalle de scrutation de la file d'événements (s). Court pour rester
# réactif, sans monopoliser le CPU (roadmap §5.9 : sleep 0,2 s).
BROADCAST_INTERVAL = 0.2

# Instance unique du moteur (config chargée au démarrage du processus)
engine = DictationEngine(load_config())

# Clients WebSocket actuellement connectés
websocket_clients: set[WebSocket] = set()


async def broadcast_events() -> None:
    """Tâche de fond : envoie les événements du moteur à tous les clients WS."""
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        if not websocket_clients:
            # Aucun abonné : on ne consomme pas les files -> les événements
            # restent disponibles (200 pour state/log/transcript, ~1 s bornée
            # pour l'audio à 20 fps) pour le prochain client.
            continue
        events = engine.pop_events()
        if not events:
            continue
        payload = json.dumps(events, ensure_ascii=False)
        dead = []
        for ws in websocket_clients:
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001 — client WS mort/fermé
                dead.append(ws)
        for ws in dead:
            websocket_clients.discard(ws)
