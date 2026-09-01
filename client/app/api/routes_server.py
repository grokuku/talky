# -*- coding: utf-8 -*-
"""
app/api/routes_server.py
========================
Section « Serveur » de l'interface web (roadmap §3.5, §5.9, R12) :
état de la connexion au serveur talky, test de connectivité et gestion
(installation) de modèles.

  * GET  /api/server/status          -> aperçu léger (polling frontend 5 s) ;
  * POST /api/server/test            -> test complet (bouton « Tester la connexion ») ;
  * GET  /api/server/registry        -> modèles disponibles à l'installation ;
  * POST /api/server/models/download -> installation d'un modèle.

`device` / `compute_type` sont ceux du serveur talky (cf. app/core/constants.py
SERVER_DEFAULTS). La disponibilité se sonde via /docs (repli /openapi.json) et
la liste des modèles vient de GET /v1/models (repli local).

Tous les appels httpx (ping, list_models, list_registry, download_model) sont
offloadés via asyncio.to_thread : les fonctions clientes restent bloquantes
(httpx synchrone) et ne doivent jamais geler la boucle d'événements — notamment
pendant le polling /api/server/status toutes les 5 s par le frontend.
"""

import asyncio
import time

from fastapi import APIRouter

from app.api.dependencies import engine
from app.core.constants import SERVER_DEFAULTS
from app.engine import transcriber_client

router = APIRouter(prefix="/api/server", tags=["server"])

# Timeout court de la sonde réseau (ping, list_models) : l'interface web ne
# doit jamais rester bloquée sur un serveur injoignable. list_models fait un
# GET /v1/models côté serveur (repli sur la liste locale en cas d'échec) :
# il est appelé sans condition, la liste est toujours dispo.
SERVER_PROBE_TIMEOUT = 3.0


def _server_context() -> tuple[str, str]:
    """(server_url, api_key) depuis la configuration courante du moteur."""
    return (
        engine.config.get("server_url", ""),
        engine.config.get("server_api_key", ""),
    )


@router.get("/status")
async def server_status() -> dict:
    """État de la connexion au serveur talky (interrogé par le frontend)."""
    server_url, api_key = _server_context()
    ping_result = await asyncio.to_thread(
        transcriber_client.ping, server_url, api_key,
        timeout=SERVER_PROBE_TIMEOUT)
    reachable = bool(ping_result.get("reachable", False))
    # list_models interroge GET /v1/models côté serveur, avec repli sur la
    # liste locale des modèles connus (whisper-live) : le frontend a donc
    # toujours une liste à afficher, même serveur injoignable.
    models = await asyncio.to_thread(
        transcriber_client.list_models, server_url, api_key,
        timeout=SERVER_PROBE_TIMEOUT)

    payload = {
        "reachable": reachable,
        "server_url": server_url,
        "model": engine.config.get("model", ""),
        "device": SERVER_DEFAULTS["device"],
        "compute_type": engine.config.get("compute_type", "int8"),
        "models": models,
    }
    if not reachable:
        payload["error"] = ping_result.get("error", "Serveur injoignable")
        payload["message"] = "Serveur talky injoignable"
    else:
        payload["message"] = "Serveur talky détecté"
    return payload


@router.post("/test")
async def server_test(body: dict | None = None) -> dict:
    """Test de connexion détaillé (ping + list_models + latence).

    Accepte un body optionnel ``{"server_url": ..., "server_api_key": ...}``
    pour tester avec les valeurs en cours d'édition dans le formulaire
    (avant sauvegarde). Si le body est absent ou ne contient pas ces
    champs, on retombe sur la configuration sauvegardée.
    """
    saved_url, saved_key = _server_context()
    body = body or {}
    # server_url : priorité au body (si non vide), sinon config sauvegardée.
    server_url = (body.get("server_url") or "").strip() or saved_url
    # server_api_key : si la clé est présente dans le body (même vide),
    # on l'utilise ; sinon on retombe sur la config sauvegardée.
    api_key = body.get("server_api_key", saved_key)
    started = time.monotonic()
    ping_result = await asyncio.to_thread(
        transcriber_client.ping, server_url, api_key,
        timeout=SERVER_PROBE_TIMEOUT)
    reachable = bool(ping_result.get("reachable", False))
    latency_ms = round((time.monotonic() - started) * 1000, 1) if reachable else None
    # list_models LOCAL en repli (modèles connus) — le serveur répond via
    # GET /v1/models quand il est joignable.
    models = await asyncio.to_thread(
        transcriber_client.list_models, server_url, api_key,
        timeout=SERVER_PROBE_TIMEOUT)
    return {
        "reachable": reachable,
        "url": server_url,
        "latency_ms": latency_ms,
        "models": models,
        "error": None if reachable else ping_result.get("error", "Serveur injoignable"),
        "message": "Serveur talky détecté" if reachable else "Serveur talky injoignable",
    }


@router.get("/registry")
async def server_registry() -> dict:
    """Modèles disponibles à l'installation (relaie GET /v1/registry du
    serveur talky). Toujours un dict {"models": [...]} ([] si échec)."""
    server_url, api_key = _server_context()
    models = await asyncio.to_thread(
        transcriber_client.list_registry, server_url, api_key,
        timeout=SERVER_PROBE_TIMEOUT)
    return {"models": models, "server_url": server_url}


@router.post("/models/download")
async def server_download_model(body: dict) -> dict:
    """Installe un modèle sur le serveur (relaie POST /v1/models, body JSON).

    Body : {"model": "Systran/faster-whisper-medium"}. Peut prendre plusieurs minutes (téléchargement
    HuggingFace) — le client affiche un indicateur d'attente.
    """
    model = (body.get("model") or "").strip()
    if not model:
        return {"ok": False, "model": "", "error": "Modèle manquant"}
    server_url, api_key = _server_context()
    try:
        result = await asyncio.to_thread(
            transcriber_client.download_model, server_url, api_key,
            model=model, timeout=600.0)
        return {"ok": True, "model": model, "result": result}
    except transcriber_client.TranscriptionError as exc:
        return {"ok": False, "model": model, "error": str(exc)}
