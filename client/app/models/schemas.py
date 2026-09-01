# -*- coding: utf-8 -*-
"""
app/models/schemas.py
=====================
Types et structures de données partagées par le moteur et l'API.

Les valeurs échangées avec le frontend restent des dictionnaires purs
(compatibilité JSON / WebSocket garantie) ; les aliases ci-dessous
documentent leur forme sans en changer la nature.
"""

from typing import Any

# Alias de type : tout échanger via des dicts JSON-sérialisables
ConfigDict = dict[str, Any]
EngineSnapshot = dict[str, Any]
HistoryEntry = dict[str, Any]
Event = dict[str, Any]                      # {"type": ..., "data": ...}


def make_history_entry(text: str, ts: float, language: str, duration: float) -> HistoryEntry:
    """Construit une entrée d'historique de transcription (format JSON)."""
    return {
        "text": text,
        "ts": ts,
        "language": language,
        "duration": round(duration, 2),
    }


def make_transcript_event(text: str, language: str, duration: float, ts: float) -> Event:
    """Construit l'événement « transcription » diffusé aux clients WebSocket."""
    return {
        "type": "transcript",
        "data": {
            "text": text,
            "language": language,
            "duration": round(duration, 2),
            "ts": ts,
        },
    }


def to_int(value: Any, default: int | None = None) -> int | None:
    """Convertit une valeur en entier (index de périphérique audio).

    None, "" ou valeur non convertible -> `default` (None = périphérique
    par défaut). Utile pour /api/devices/audio : une chaîne "1" ne doit
    jamais être confondue avec un NOM de device.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
