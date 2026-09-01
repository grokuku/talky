# -*- coding: utf-8 -*-
"""
app/api/factory.py
==================
Fabrication de l'application FastAPI : interface web (statique, P7),
API REST, WebSocket et cycle de vie (roadmap §5.9).

Le frontend (templates/ + static/) est créé en P7 : tant qu'il est absent,
``/`` répond avec une page minimale et ``/static`` n'est pas monté — l'API
REST et le WebSocket restent pleinement fonctionnels (tests P6 sans
frontend).
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    routes_config,
    routes_devices,
    routes_engine,
    routes_history,
    routes_server,
    websocket,
)
from app.api.lifespan import lifespan
from app.core.config import STATIC_DIR, TEMPLATES_DIR
from app.core.logging import setup_logging

log = setup_logging()


def build_app() -> FastAPI:
    """Construit l'application FastAPI complète (frontend conditionnel)."""
    app = FastAPI(title="Talky - Dictée vocale", version="0.1.0", lifespan=lifespan)

    # Interface web (P7) : fichiers statiques montés seulement si le dossier
    # existe. Les tests P6 passent sans frontend (dossiers absents).
    static_dir = Path(STATIC_DIR)
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        """Page web unique (SPA) — placeholder minimal tant que P7 n'existe pas."""
        index_file = Path(TEMPLATES_DIR) / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return HTMLResponse(
            "<!doctype html><html lang='fr'><body>"
            "<h1>Talky — Dictée vocale</h1>"
            "<p>API locale opérationnelle. Le panneau web (templates/"
            "index.html) sera fourni par la phase P7.</p>"
            "</body></html>"
        )

    # API REST + WebSocket
    app.include_router(routes_config.router)
    app.include_router(routes_engine.router)
    app.include_router(routes_history.router)
    app.include_router(routes_devices.router)
    app.include_router(routes_server.router)
    app.include_router(websocket.router)

    return app
