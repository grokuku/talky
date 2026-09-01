# -*- coding: utf-8 -*-
"""
app/api/routes_config.py
========================
Routes de configuration : lecture et sauvegarde dynamique (config.json),
appliquée au moteur à chaud (roadmap §5.9).

POST /api/config retourne ``reload_needed`` (redémarrage complet requis,
ex. audio_device) et ``live_changed`` (liste des champs HOT_FIELDS
appliqués à chaud par engine.apply_config).
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.dependencies import engine
from app.core.config import DEFAULT_CONFIG, load_config, save_config, validate_config

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_config() -> dict:
    return load_config()


@router.post("")
async def set_config(payload: dict) -> JSONResponse:
    """
    Applique puis sauvegarde la configuration validée.

    (m10) Le payload est filtré sur les clés connues de DEFAULT_CONFIG :
    toute clé inconnue est ignorée (et donc jamais persistée).

    (M2) L'application au moteur (apply_config) précède la persistance
    (save_config) : une validation qui échoue (ex. hotkey invalide levant
    ValueError depuis parse_hotkey) ne modifie NI config.json NI l'état
    du moteur, dont les hotkeys précédentes restent actives.

    Retourne `reload_needed` (booléen — un redémarrage est requis) et
    `live_changed` (liste des champs modifiés à chaud). Erreurs de
    validation -> 400 {"saved": False, "errors": ...}.
    """
    # (m10) Ne conserver que les clés connues de DEFAULT_CONFIG.
    payload = {k: v for k, v in payload.items() if k in DEFAULT_CONFIG}
    try:
        new_cfg = validate_config(load_config() | payload)
        if new_cfg.get("language") is None:
            new_cfg["language"] = "auto"  # stocké explicitement dans config.json
        # (M2) apply_config d'abord : valide + installe les hotkeys en
        # mémoire ; échec -> ValueError propagée, rien n'est écrit.
        reload_needed, live_changed = engine.apply_config(new_cfg)
        save_config(new_cfg)
        return JSONResponse({
            "saved": True,
            "reload_needed": reload_needed,
            "live_changed": live_changed,
            "config": new_cfg,
        })
    except ValueError as exc:
        return JSONResponse({"saved": False, "errors": exc.args[0]}, status_code=400)
