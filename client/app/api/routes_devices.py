# -*- coding: utf-8 -*-
"""
app/api/routes_devices.py
=========================
Détection des microphones système (sounddevice) exposée à l'interface web
(roadmap §5.9, R9).

On n'expose que des types JSON-safe (int / str) : les types propres à
sounddevice (_InputOutputPair, ...) ne sont pas sérialisables par JSON.
``to_int`` (app/models/schemas.py) blinde la conversion.
"""

from fastapi import APIRouter

from app.core.logging import get_logger
from app.models.schemas import to_int

router = APIRouter(prefix="/api/devices", tags=["devices"])
log = get_logger()


@router.get("/audio", tags=["devices"])
async def audio_devices() -> dict:
    try:
        import sounddevice as sd

        try:
            device_list = list(sd.query_devices())
        except Exception:  # noqa: BLE001 — PortAudio indisponible
            device_list = []

        # Seuls les périphériques d'ENTRÉE (max_input_channels > 0) sont listés.
        inputs = [
            {
                "index": int(i),
                "name": str((d.get("name") if isinstance(d, dict) else "?") or "?"),
                "channels": to_int(d.get("max_input_channels") if isinstance(d, dict) else 0) or 0,
                "samplerate": to_int(d.get("default_samplerate") if isinstance(d, dict) else 0) or 0,
            }
            for i, d in enumerate(device_list)
            if isinstance(d, dict)
            and (to_int(d.get("max_input_channels")) or 0) > 0
        ]

        try:
            default = sd.default.device
        except Exception:  # noqa: BLE001
            default = None

        return {"devices": inputs, "default": to_int(default)}
    except Exception as exc:  # noqa: BLE001 — repli propre sans sounddevice
        log.warning(f"Liste des périphériques audio indisponible : {exc}")
        return {"devices": [], "default": None, "error": str(exc)}
