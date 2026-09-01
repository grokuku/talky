# -*- coding: utf-8 -*-
"""
app/api/routes_history.py
=========================
Historique des transcriptions (lecture et vidage) — roadmap §5.9.
"""

from fastapi import APIRouter

from app.api.dependencies import engine

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
async def history() -> dict:
    return {"history": engine.get_history()}


@router.delete("")
async def clear_history() -> dict:
    engine.clear_history()
    return {"ok": True}
