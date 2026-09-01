# -*- coding: utf-8 -*-
"""
app/api/lifespan.py
===================
Cycle de vie de l'application FastAPI : tâche de diffusion des événements
(moteur -> WebSocket) et démarrage automatique du moteur si configuré
(roadmap §5.9).

À l'arrêt du serveur, la tâche de diffusion est annulée puis le moteur est
arrêté proprement (hooks clavier retirés, flux audio fermé).
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dependencies import broadcast_events, engine
from app.core.logging import get_logger

log = get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Démarre la tâche de diffusion puis le moteur si auto_start est actif."""
    broadcaster = asyncio.create_task(broadcast_events())
    log.info("Serveur web de dictée démarré.")
    if engine.config.get("auto_start"):
        engine.start()
    try:
        yield
    finally:
        broadcaster.cancel()
        engine.stop()
