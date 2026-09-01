# -*- coding: utf-8 -*-
"""
app/api/routes_engine.py
========================
Contrôle du moteur de dictée (état, démarrage, arrêt, redémarrage) —
roadmap §5.9.
"""

import asyncio

from fastapi import APIRouter

from app.api.dependencies import engine

router = APIRouter(prefix="/api/engine", tags=["engine"])

# NOTE (gel UI) : engine.start()/stop()/restart() sont des méthodes SYNCHRONES
# qui font du travail bloquant (audio.stop(), fermeture du client WebSocket,
# puis jusqu'à 4 join() de threads daemon — boot/sender/receiver/connect —
# chacun borné à 1 s, soit un pire cas proche de 4 s). Appeler cela en direct
# dans une route `async def` gèle la boucle d'événements uvicorn : le WS /ws et
# le polling REST sont alors morts pendant tout l'arrêt = interface web figée.
# On offload donc chaque action moteur sur le pool de threads via
# asyncio.to_thread (même pattern que routes_server.py) pour ne jamais bloquer
# la boucle.


@router.get("")
async def engine_state() -> dict:
    return engine.snapshot()


@router.post("/start")
async def engine_start() -> dict:
    await asyncio.to_thread(engine.start)
    return {"ok": True, "state": engine.snapshot()}


@router.post("/stop")
async def engine_stop() -> dict:
    await asyncio.to_thread(engine.stop)
    return {"ok": True, "state": engine.snapshot()}


@router.post("/restart")
async def engine_restart() -> dict:
    await asyncio.to_thread(engine.restart)
    return {"ok": True, "state": engine.snapshot()}
