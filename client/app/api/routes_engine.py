# -*- coding: utf-8 -*-
"""
app/api/routes_engine.py
========================
Contrôle du moteur de dictée (état, démarrage, arrêt, redémarrage) —
roadmap §5.9.
"""

from fastapi import APIRouter

from app.api.dependencies import engine

router = APIRouter(prefix="/api/engine", tags=["engine"])


@router.get("")
async def engine_state() -> dict:
    return engine.snapshot()


@router.post("/start")
async def engine_start() -> dict:
    engine.start()
    return {"ok": True, "state": engine.snapshot()}


@router.post("/stop")
async def engine_stop() -> dict:
    engine.stop()
    return {"ok": True, "state": engine.snapshot()}


@router.post("/restart")
async def engine_restart() -> dict:
    engine.restart()
    return {"ok": True, "state": engine.snapshot()}
