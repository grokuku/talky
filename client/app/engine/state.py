# -*- coding: utf-8 -*-
"""
app/engine/state.py
===================
État du moteur + file d'événements + historique, entièrement protégés
par le verrou partagé (RLock) du moteur.

Porté de l'original (ref/app/engine/state.py) et adapté au modèle de
Talky : statut/message, file d'événements (deque maxlen 200) et
historique (deque maxlen max_history) sont conservés, mais les états
suivent app/core/constants.py (idle/booting/ready/recording/...).

Événements émis vers le frontend : ``state``, ``log``, ``transcript`` et
``audio`` (waveform temps réel). ``audio`` suit exactement le même chemin
que les autres (files d'événements -> pop_events -> WebSocket) mais n'alimente
jamais l'historique des transcriptions (deque séparée).
Pour respecter l'ordre consommé par le frontend (state → log → transcript),
``set_status`` émet l'événement ``state`` AVANT le ``log`` STATUS.

File d'événements SÉPARÉE pour l'audio (m2) : les événements ``audio``
arrivent à 20 fps pendant l'enregistrement — en partageant une seule deque,
un burst audio évinerait les événements critiques (log ERROR, transcript).
L'audio a donc sa deque bornée à ~1 s (AUDIO_EVENTS_MAXLEN = 20 événements),
le reste du trafic garde une deque de 200 ; ``pop_events`` fusionne les
deux (état/log/transcript d'abord, puis audio).
"""

import threading
import time
from collections import deque

from app.core.constants import (
    SERVER_DEFAULTS,
    STATE_IDLE,
    STATE_STOPPING,
)
from app.models.schemas import make_history_entry

# Taille de la file d'événements critiques (state/log/transcript/...) : le
# frontend consomme toutes les 0,2 s (BROADCAST_INTERVAL) ; 200 couvre déjà
# une longue déconnexion sans exploser en mémoire.
EVENTS_MAXLEN = 200

# Bornage de la file audio : le AudioRecorder émet à 20 fps
# (LEVEL_INTERVAL = 0,05 s) -> 20 événements ≈ 1 s de waveform. Un pic audio
# n'évince JAMAIS un log ERROR ni un transcript (files séparées).
AUDIO_EVENTS_MAXLEN = 20


class EngineState:
    """
    Détient l'état courant (status, message), la file d'événements destinée
    au frontend (WebSocket) et l'historique des transcriptions.
    """

    def __init__(self, config: dict, lock: threading.RLock) -> None:
        self._config = config
        self._lock = lock
        self._status = STATE_IDLE
        self._status_msg = ""
        # Files séparées (m2) : l'audio à 20 fps ne doit pas évincer les
        # événements critiques (state/log/transcript) ni réciproquement.
        self._events: deque = deque(maxlen=EVENTS_MAXLEN)
        self._audio_events: deque = deque(maxlen=AUDIO_EVENTS_MAXLEN)
        self._history: deque = deque(
            maxlen=max(int(config.get("max_history", 50)), 1))

    # ------------------------------------------------------------------
    # Événements (push vers le frontend)
    # ------------------------------------------------------------------
    def emit(self, etype: str, data: dict) -> None:
        """Ajoute un événement à la file destinée au frontend (WebSocket).

        Les événements ``audio`` (20 fps) vont dans leur propre deque bornée
        à ~1 s ; les autres (state, log, transcript, ...) dans la deque de
        200. Aucun événement n'alimente l'historique des transcriptions
        (deque ``_history`` séparée) : « audio » ne pollue donc jamais
        l'historique.
        """
        event = {"type": etype, "data": data}
        with self._lock:
            if etype == "audio":
                self._audio_events.append(event)
            else:
                self._events.append(event)

    def pop_events(self) -> list:
        """Retourne et vide les files d'événements (appelé par le broadcast
        WS). Les événements critiques (state/log/transcript) sont servis en
        premier, l'audio ensuite (ordre non critique pour la waveform)."""
        with self._lock:
            events = list(self._events) + list(self._audio_events)
            self._events = deque(maxlen=EVENTS_MAXLEN)
            self._audio_events = deque(maxlen=AUDIO_EVENTS_MAXLEN)
        return events

    def log(self, level: str, message: str) -> None:
        self.emit("log", {"level": level, "message": message, "ts": time.time()})

    # ------------------------------------------------------------------
    # Statut courant
    # ------------------------------------------------------------------
    def set_status(self, status: str, message: str = "") -> None:
        with self._lock:
            self._status = status
            self._status_msg = message
        # « state » puis « log » : l'ordre state → log → transcript est celui
        # attendu par le frontend (roadmap §5.6).
        self.emit("state", self.snapshot(self._config.get("model", "")))
        self.log("STATUS", f"{status}: {message}" if message else status)

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_msg(self) -> str:
        return self._status_msg

    def snapshot(self, model_name: str) -> dict:
        """Instantané courant (utilisé par l'API REST et le frontend)."""
        with self._lock:
            return {
                "running": self._status not in (STATE_IDLE, STATE_STOPPING),
                "status": self._status,
                "status_msg": self._status_msg,
                "model": model_name,
                "device": SERVER_DEFAULTS["device"],
                "compute_type": self._config.get("compute_type", "int8"),
                "hotkey": self._config.get("hotkey", ""),
                "input_mode": self._config.get("input_mode", "push_to_talk"),
                "language": self._config.get("language"),
                "config": dict(self._config),
            }

    # ------------------------------------------------------------------
    # Historique des transcriptions
    # ------------------------------------------------------------------
    def history_append(self, text: str, duration: float, language: str) -> None:
        with self._lock:
            self._history.append(
                make_history_entry(text, time.time(), language, duration))

    def history_get(self) -> list:
        with self._lock:
            return list(self._history)

    def history_clear(self) -> None:
        with self._lock:
            self._history.clear()

    def history_resize(self, max_len: int) -> None:
        with self._lock:
            self._history = deque(self._history, maxlen=max(max_len, 1))
            self._config["max_history"] = max_len
