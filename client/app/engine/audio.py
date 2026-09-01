# -*- coding: utf-8 -*-
"""
app/engine/audio.py
===================
Capture audio du microphone via sounddevice (flux PCM 16 kHz mono),
avec gestion du périphérique d'entrée configuré dynamiquement (P3, §5.1).

Porté de l'original Windows (ref/app/engine/audio.py) : même API publique
(open/stop/begin/is_recording/end), seule la gestion d'erreur est renforcée
pour produire un message actionnable quand le périphérique est invalide.
Aucune dépendance à torch/whisper.

Monitoring audio permanent (waveform)
------------------------------------
Tant que le flux est ouvert, le callback PortAudio calcule — en plus de
l'accumulation des blocs quand `begin()` est actif — une forme d'onde
downsamplée (~64 valeurs dans [-1.0, 1.0]) et la transmet via le callback
optionnel `on_level(levels, recording)`. L'émission est throttlée à 20 fps
(50 ms) pour ne pas saturer le WebSocket ni le CPU. Le callback est optionnel :
s'il n'est pas fourni, l'API existante (begin/end/is_recording/stop) est
strictement préservée.
"""

import logging
import time
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from app.core.constants import SAMPLING_RATE

log = logging.getLogger("talky")

# Intervalle minimal (s) entre deux émissions de niveaux audio -> 20 fps.
LEVEL_INTERVAL = 0.05
# Nombre de valeurs de la forme d'onde transmise au frontend (waveform).
LEVEL_BINS = 64


class AudioRecorderError(RuntimeError):
    """Erreur claire d'ouverture/configuration du périphérique audio."""


class AudioRecorder:
    """Flux micro 16 kHz mono + capture par blocs lorsque `begin()` est actif.

    Paramètre optionnel ``on_level`` : callback ``(levels, recording) -> None``
    invoqué à 20 fps tant que le flux est ouvert, pour pousser la forme d'onde
    vers le frontend via WebSocket. Aucune obligation de le fournir.

    Paramètre optionnel ``on_chunk`` : callback ``(chunk) -> None`` invoqué
    pour chaque bloc audio capturé pendant l'enregistrement (``begin()``
    actif). Utilisé par le mode continu pour streamer l'audio vers le thread
    segmenter (chunked HTTP batch). Aucune obligation de le fournir.
    """

    def __init__(
        self,
        on_level: Optional[Callable[[list, bool], None]] = None,
        on_chunk: Optional[Callable[[object], None]] = None,
    ) -> None:
        self._stream: Optional[sd.InputStream] = None
        self._recording = False
        self._chunks: list = []
        self._on_level = on_level
        self._on_chunk = on_chunk
        # Timestamp (monotonic) du dernier envoi de niveaux ; None -> le
        # prochain calcul émet immédiatement (premier bloc du flux).
        self._last_level_ts: Optional[float] = None

    # ------------------------------------------------------------------
    # Cycle de vie du flux
    # ------------------------------------------------------------------
    def open(self, device: Optional[int] = None) -> None:
        """Ouvre et démarre le flux (None = périphérique d'entrée par défaut).

        Lève `AudioRecorderError` avec un message clair si le périphérique
        est invalide ou si PortAudio n'arrive pas à ouvrir le flux
        (route ALSA/PipeWire manquante, device inexistant, ...).
        """
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLING_RATE,
                channels=1,
                callback=self._callback,
                device=device,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 — PortAudio lève des exceptions variées
            self._stream = None
            label = device if device is not None else "défaut"
            raise AudioRecorderError(
                f"Impossible d'ouvrir le périphérique audio « {label} » "
                f"(16 kHz mono) : {exc}. Vérifier la sélection du micro dans "
                f"la configuration (audio_device) et la route ALSA→PipeWire "
                f"(paquets pipewire-alsa / portaudio)."
            ) from exc

    def stop(self) -> None:
        """Arrête et ferme le flux s'il existe."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001 — fermeture best-effort
                pass
            self._stream = None
        self._recording = False
        self._chunks = []
        # Réarmement du throttle : le prochain flux émettra dès le 1er bloc.
        self._last_level_ts = None

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        """Callback PortAudio : capture si enregistrement + monitoring audio.

        Les niveaux (waveform downsamplée) sont calculés en permanence, tant
        que le flux est ouvert, indépendamment de l'état d'enregistrement :
        le frontend affiche ainsi « ce que le programme entend » même au repos.

        Si ``on_chunk`` est fourni, chaque bloc capturé pendant
        l'enregistrement lui est transmis (mode continu / chunked HTTP batch).
        """
        if self._recording:
            self._chunks.append(indata.copy())
            if self._on_chunk is not None:
                try:
                    self._on_chunk(indata.copy())
                except Exception:  # noqa: BLE001 — best-effort, jamais fatal
                    log.debug("audio: échec callback on_chunk", exc_info=True)
        self._emit_levels(indata)

    # ------------------------------------------------------------------
    # Monitoring audio (waveform) — 20 fps, downsampling ~64 valeurs
    # ------------------------------------------------------------------
    def _emit_levels(self, indata) -> None:
        """Calcule (downsample ~64 valeurs) et émet les niveaux à 20 fps.

        Ne lève jamais : le monitoring ne doit pas interrompre la capture.
        """
        if self._on_level is None:
            return
        now = time.monotonic()
        if (
            self._last_level_ts is not None
            and now - self._last_level_ts < LEVEL_INTERVAL
        ):
            return
        self._last_level_ts = now
        try:
            levels = self._downsample(indata)
            self._on_level(levels, self._recording)
        except Exception:  # noqa: BLE001 — best-effort, jamais fatal
            log.debug("audio: échec calcul/émission des niveaux", exc_info=True)

    @staticmethod
    def _downsample(indata) -> list:
        """Retourne LEVEL_BINS floats dans [-1.0, 1.0] (forme d'onde).

        - bloc plus court que LEVEL_BINS : on garde les échantillons et on
          complète par des zéros (signal « plat ») ;
        - bloc plus long : on prélève LEVEL_BINS échantillons répartis
          uniformément sur le bloc (pas de FFT, coût négligeable) ;
        - les valeurs sont ramenées (clamp) dans [-1.0, 1.0].
        """
        flat = indata.flatten() if hasattr(indata, "flatten") else indata
        n = len(flat)
        if n == 0:
            return [0.0] * LEVEL_BINS
        if n <= LEVEL_BINS:
            samples = [float(flat[i]) for i in range(n)]
            samples += [0.0] * (LEVEL_BINS - n)
        else:
            samples = [
                float(flat[int(i * n / LEVEL_BINS)]) for i in range(LEVEL_BINS)
            ]
        return [max(-1.0, min(1.0, v)) for v in samples]

    def begin(self) -> None:
        """Démarre une session d'enregistrement (blocs accumulés)."""
        self._chunks = []
        self._recording = True

    def is_recording(self) -> bool:
        return self._recording

    def end(self) -> Optional[np.ndarray]:
        """
        Termine la session et retourne l'audio mono aplati (16 kHz).
        Retourne None si aucun échantillon n'a été capturé.
        """
        self._recording = False
        if not self._chunks:
            return None
        audio = np.concatenate(self._chunks, axis=0).flatten()
        self._chunks = []
        return audio
