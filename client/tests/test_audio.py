# -*- coding: utf-8 -*-
"""
tests/test_audio.py
===================
AudioRecorder (P3, §5.1) : cycle de vie du flux sounddevice, accumulation
par blocs pendant begin(), end() → audio aplati ou None si vide, erreur
claire si le périphérique est invalide.

Aucun accès au micro réel : sounddevice est mocké par conftest.py.
"""

import types

import numpy as np
import pytest
import sounddevice as sd

from app.core.constants import SAMPLING_RATE
from app.engine import audio as audio_module
from app.engine.audio import AudioRecorder, AudioRecorderError


class Block:
    """Bloc audio factice compatible avec le mock numpy (copy/flatten) ET
    le vrai numpy (via ``__array__``) : le champ ``indata`` de sounddevice
    est un vrai ndarray, donc ``AudioRecorder.end()`` fait
    ``np.concatenate(...)`` dessus ; ``__array__`` permet au vrai numpy de
    convertir ce bloc en tableau 1D (le mock numpy, lui, passe par
    ``flatten()``)."""

    def __init__(self, values):
        self.values = values

    def __array__(self, dtype=None):
        return np.asarray(self.values, dtype=dtype)

    def copy(self):
        return self

    def flatten(self):
        return self.values


def feed(recorder, *blocks):
    """Simule l'arrivée de blocs PortAudio via le callback."""
    for block in blocks:
        recorder._callback(block, None, None, None)


class TestAudioRecorder:
    def test_open_starts_stream(self):
        recorder = AudioRecorder()
        recorder.open()
        assert recorder._stream is not None
        assert recorder._stream.started is True
        recorder.stop()

    def test_open_uses_sampling_rate_16khz_mono(self, monkeypatch):
        captured = {}

        class SpyStream:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(sd, "InputStream", SpyStream)
        recorder = AudioRecorder()
        recorder.open(device=None)
        assert captured["samplerate"] == SAMPLING_RATE
        assert captured["channels"] == 1
        assert captured["device"] is None
        recorder.stop()

    def test_begin_recording_accumulates_blocks(self):
        recorder = AudioRecorder()
        recorder.open()
        assert recorder.is_recording() is False
        recorder.begin()
        assert recorder.is_recording() is True
        feed(recorder, Block([0.1, 0.2]), Block([0.3, 0.4]))
        audio = recorder.end()
        assert list(audio) == [0.1, 0.2, 0.3, 0.4]
        assert recorder.is_recording() is False
        recorder.stop()

    def test_callback_ignored_outside_recording(self):
        recorder = AudioRecorder()
        recorder.open()
        feed(recorder, Block([0.1, 0.2]))  # pas de begin() → rien
        assert recorder.end() is None
        recorder.stop()

    def test_end_returns_none_when_no_samples(self):
        recorder = AudioRecorder()
        recorder.open()
        recorder.begin()
        assert recorder.end() is None
        recorder.stop()

    def test_end_clears_chunks(self):
        recorder = AudioRecorder()
        recorder.open()
        recorder.begin()
        feed(recorder, Block([1.0]))
        first = recorder.end()
        assert list(first) == [1.0]
        # une seconde session ne repart pas avec l'ancien audio
        recorder.begin()
        assert recorder.end() is None
        recorder.stop()

    def test_stop_resets_state_and_closes_stream(self):
        recorder = AudioRecorder()
        recorder.open()
        recorder.begin()
        feed(recorder, Block([0.5]))
        recorder.stop()
        assert recorder.is_recording() is False
        assert recorder._stream is None
        assert recorder.end() is None

    def test_open_invalid_device_raises_clear_error(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("PortAudioError: Invalid device")

        monkeypatch.setattr(sd, "InputStream", boom)
        recorder = AudioRecorder()
        with pytest.raises(AudioRecorderError) as exc_info:
            recorder.open(device=999)
        assert "999" in str(exc_info.value)
        assert recorder._stream is None


# ---------------------------------------------------------------------------
# Monitoring audio permanent (waveform) — callback on_level à 20 fps
# ---------------------------------------------------------------------------
class TestAudioMonitoring:
    """Calcul des niveaux (downsample ~64), throttle 20 fps, callback on_level."""

    def test_on_level_called_with_downsampled_levels(self):
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append((lvl, rec)))
        recorder.open()
        # Bloc plus long que LEVEL_BINS : doit être réduit à 64 valeurs.
        feed(recorder, Block([float(i) / 1000 for i in range(200)]))
        assert len(calls) == 1
        levels, recording = calls[0]
        assert len(levels) == 64
        assert all(-1.0 <= v <= 1.0 for v in levels)
        assert recording is False           # pas de begin() -> False
        recorder.stop()

    def test_on_level_reflects_recording_state(self):
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append((lvl, rec)))
        recorder.open()
        recorder.begin()
        feed(recorder, Block([0.1, 0.2, 0.3]))
        assert len(calls) == 1
        assert calls[0][1] is True          # recording=True pendant begin()
        recorder.stop()

    def test_on_level_pads_short_block_to_64(self):
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append(lvl))
        recorder.open()
        feed(recorder, Block([0.1, 0.2, 0.3]))
        levels = calls[0]
        assert len(levels) == 64
        assert levels[:3] == [0.1, 0.2, 0.3]
        assert levels[3:] == [0.0] * 61     # complété par des zéros
        recorder.stop()

    def test_on_level_clamps_to_range(self):
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append(lvl))
        recorder.open()
        feed(recorder, Block([2.0, -3.0, 0.5]))
        levels = calls[0]
        assert levels[0] == 1.0            # clamp supérieur
        assert levels[1] == -1.0           # clamp inférieur
        assert levels[2] == 0.5
        recorder.stop()

    def test_on_level_zeros_for_empty_block(self):
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append(lvl))
        recorder.open()
        feed(recorder, Block([]))
        assert calls[0] == [0.0] * 64
        recorder.stop()

    def test_on_level_throttled_to_20fps(self, monkeypatch):
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append(lvl))
        recorder.open()
        # Horloge contrôlée : le throttle dépend de time.monotonic().
        t = [0.0]
        monkeypatch.setattr(
            audio_module, "time",
            types.SimpleNamespace(monotonic=lambda: t[0]))
        feed(recorder, Block([0.1] * 100))          # t=0.0 -> émet
        assert len(calls) == 1
        t[0] = 0.03                                   # < 50 ms -> throttlé
        feed(recorder, Block([0.2] * 100))
        assert len(calls) == 1
        t[0] = 0.06                                   # >= 50 ms -> émet
        feed(recorder, Block([0.3] * 100))
        assert len(calls) == 2
        recorder.stop()

    def test_on_level_none_does_not_break_capture(self):
        # Sans callback, l'API existante reste intacte (begin/end/stop).
        recorder = AudioRecorder()
        recorder.open()
        feed(recorder, Block([0.1, 0.2]))             # aucun appel, aucune erreur
        recorder.begin()
        feed(recorder, Block([0.3, 0.4]))
        audio = recorder.end()
        assert list(audio) == [0.3, 0.4]
        recorder.stop()

    def test_on_level_resets_throttle_after_stop(self):
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append(lvl))
        recorder.open()
        feed(recorder, Block([0.1] * 100))           # émet (1er bloc)
        assert len(calls) == 1
        recorder.stop()
        recorder.open()                              # réarmement du throttle
        feed(recorder, Block([0.2] * 100))           # émet à nouveau (1er bloc)
        assert len(calls) == 2
        recorder.stop()

    def test_on_level_does_not_affect_chunks(self):
        # Le monitoring n'altère pas l'audio accumulé pour la transcription.
        calls = []
        recorder = AudioRecorder(on_level=lambda lvl, rec: calls.append(lvl))
        recorder.open()
        recorder.begin()
        feed(recorder, Block([0.1, 0.2]), Block([0.3, 0.4]))
        audio = recorder.end()
        assert list(audio) == [0.1, 0.2, 0.3, 0.4]  # audio intact
        # Au moins un appel on_level (le 2e bloc est throttlé en pratique).
        assert len(calls) >= 1
        recorder.stop()


# ---------------------------------------------------------------------------
# Callback on_chunk — streaming audio vers le mode continu (Realtime API)
# ---------------------------------------------------------------------------
class TestAudioOnChunk:
    """Le callback on_chunk est invoqué pour chaque bloc capturé pendant
    l'enregistrement (begin() actif), et jamais en dehors."""

    def test_on_chunk_called_for_each_block_during_recording(self):
        calls = []
        recorder = AudioRecorder(on_chunk=lambda chunk: calls.append(chunk))
        recorder.open()
        recorder.begin()
        feed(recorder, Block([0.1, 0.2]), Block([0.3, 0.4]))
        assert len(calls) == 2  # un appel par bloc
        # Chaque chunk reçu correspond au bloc passé (via copy()).
        assert list(calls[0].flatten()) == [0.1, 0.2]
        assert list(calls[1].flatten()) == [0.3, 0.4]
        recorder.end()
        recorder.stop()

    def test_on_chunk_not_called_outside_recording(self):
        calls = []
        recorder = AudioRecorder(on_chunk=lambda chunk: calls.append(chunk))
        recorder.open()
        # Pas de begin() → on_chunk ne doit jamais être invoqué.
        feed(recorder, Block([0.1, 0.2]), Block([0.3, 0.4]))
        assert calls == []
        recorder.stop()

    def test_on_chunk_none_does_not_break_capture(self):
        # Sans callback on_chunk, l'API existante reste intacte.
        recorder = AudioRecorder()
        recorder.open()
        recorder.begin()
        feed(recorder, Block([0.1, 0.2]), Block([0.3, 0.4]))
        audio = recorder.end()
        assert list(audio) == [0.1, 0.2, 0.3, 0.4]
        recorder.stop()

    def test_on_chunk_does_not_affect_chunks(self):
        # Le callback on_chunk n'altère pas l'audio accumulé pour end().
        calls = []
        recorder = AudioRecorder(on_chunk=lambda chunk: calls.append(chunk))
        recorder.open()
        recorder.begin()
        feed(recorder, Block([0.1, 0.2]), Block([0.3, 0.4]))
        audio = recorder.end()
        assert list(audio) == [0.1, 0.2, 0.3, 0.4]  # audio intact
        assert len(calls) == 2
        recorder.stop()

    def test_on_chunk_exception_does_not_break_capture(self):
        # Une exception dans le callback ne doit pas interrompre la capture.
        def boom(chunk):
            raise RuntimeError("boom")
        recorder = AudioRecorder(on_chunk=boom)
        recorder.open()
        recorder.begin()
        feed(recorder, Block([0.1, 0.2]), Block([0.3, 0.4]))
        audio = recorder.end()
        assert list(audio) == [0.1, 0.2, 0.3, 0.4]  # capture intacte
        recorder.stop()
