# -*- coding: utf-8 -*-
"""
app/api/websocket.py
====================
WebSocket /ws : événements temps réel (état, logs, transcriptions) + hello
initial (snapshot + config) — roadmap §5.9.

Les événements sont poussés par ``broadcast_events`` (dependencies.py) via
le registre ``websocket_clients`` ; cette route ne fait qu'accepter la
connexion, envoyer l'état initial et rester en vie (keepalive).
"""

import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.dependencies import engine, websocket_clients
from app.core.config import load_config

router = APIRouter(tags=["websocket"])

# Origines autorisées pour le WebSocket /ws (anti-hijacking). Le client se
# connecte depuis la même page (http://127.0.0.1:8000 ou http://localhost:8000).
# Pour un accès LAN, ajouter l'origine via TALKY_ALLOWED_ORIGINS (liste CSV).
_ALLOWED_ORIGINS = {
    "http://127.0.0.1:8000",
    "http://localhost:8000",
}
for _o in os.environ.get("TALKY_ALLOWED_ORIGINS", "").split(","):
    _o = _o.strip()
    if _o:
        _ALLOWED_ORIGINS.add(_o)


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # Anti-hijacking : on n'accepte que les origines connues (même page / LAN).
    # Une connexion sans header Origin (client non-browser : test, app native)
    # est acceptée — les navigateurs envoient toujours Origin, donc un navigateur
    # malveillant avec une origine étrangère est rejeté.
    origin = ws.headers.get("origin", "")
    if origin and origin not in _ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    # Envoi initial : état + config, pour que le frontend s'affiche immédiatement
    try:
        await ws.send_json({
            "type": "hello",
            "data": {"state": engine.snapshot(), "config": load_config()},
        })
    except Exception:  # noqa: BLE001 — échec d'envoi : on ne s'enregistre pas
        await ws.close()
        return
    websocket_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive : on ignore les messages clients
    except WebSocketDisconnect:
        websocket_clients.discard(ws)
    except Exception:  # noqa: BLE001 — toute déconnexion anormale
        websocket_clients.discard(ws)
