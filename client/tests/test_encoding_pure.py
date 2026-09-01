# -*- coding: utf-8 -*-
"""
tests/test_encoding_pure.py
===========================
Test PUR de non-régression binaire (M7) : la conversion float32 -> PCM16
int16 petit-boutiste (vectorisée numpy) doit produire EXACTEMENT les mêmes
octets que l'ANCIENNE implémentation (boucle ``struct.pack`` par échantillon).

Couvre les deux points d'entrée :
  * ``encode_wav``        (transcriber_client) — WAV complet (en-tête 44 o + frames)
  * ``float32_to_int16``  (whisperlive_client) — frames PCM16 brutes, sans en-tête

L'ancienne implémentation est copiée ici en référence (``_reference_pcm16`` /
``_reference_encode_wav``) et appliquée AUX MÊMES VALEURS STOCKÉES dans le
tableau (float32), comme le faisait l'original via ``array.flatten()``.

Test « pur » : ne dépend que de numpy réel et des modules engine — aucun mock
conftest requis pour la logique de conversion (le repli boucle Python est
couvert par test_lot2_regressions.py).
"""

import io
import math
import random
import struct
import wave

import numpy as np

from app.core.constants import SAMPLING_RATE
from app.engine.transcriber_client import encode_wav
from app.engine.whisperlive_client import float32_to_int16


# ---------------------------------------------------------------------------
# ANCIENNE implémentation (référence de non-régression binaire)
# ---------------------------------------------------------------------------
def _reference_pcm16(samples) -> bytes:
    """Ancienne boucle struct.pack par échantillon (PCM16 LE, sans en-tête)."""
    flat = samples.flatten() if hasattr(samples, "flatten") else samples
    out = bytearray()
    for sample in flat:
        value = max(-1.0, min(1.0, float(sample)))
        out += struct.pack("<h", int(round(value * 32767)))
    return bytes(out)


def _reference_encode_wav(samples) -> bytes:
    """Ancien encode_wav : en-tête WAV + frames PCM16 (boucle struct.pack)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)                       # PCM16 = 2 octets
        wav.setframerate(SAMPLING_RATE)
        wav.writeframes(_reference_pcm16(samples))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Données de test
# ---------------------------------------------------------------------------
def _random_values(n=4096, seed=1234):
    """Valeurs aléatoires réalistes + cas limites (clip, arrondis .5, NaN)."""
    rng = random.Random(seed)
    values = [rng.uniform(-1.6, 1.6) for _ in range(n - 15)]
    values += [0.0, 1.0, -1.0, 2.0, -2.0, 1e-9, -1e-9,
               (0 + 0.5) / 32767, (1 + 0.5) / 32767, (16383 + 0.5) / 32767,
               (32766 + 0.5) / 32767, -(16383 + 0.5) / 32767,
               math.inf, -math.inf, math.nan]
    return values[:n]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestPcm16ByteIdentity:
    """(M7) La conversion vectorisée doit être byte-identique à l'ancienne
    boucle struct.pack (PCM16 little-endian)."""

    def test_encode_wav_random_sample_byte_identity(self):
        values = _random_values()
        audio = np.array(values, dtype=np.float32)
        reference = _reference_encode_wav(audio)
        assert encode_wav(audio) == reference           # WAV complet
        # L'en-tête fait 44 octets, puis frames PCM16 LE.
        assert float32_to_int16(audio) == reference[44:]

    def test_2d_and_non_contiguous_inputs(self):
        values = _random_values(n=512, seed=99)
        flat = np.array(values, dtype=np.float32)
        two_d = flat.reshape(len(values) // 8, 8)       # shape (n, 8)
        strided = flat[::3]                             # vue non contiguë
        reference = _reference_encode_wav(flat)
        assert encode_wav(two_d) == reference           # flatten() == reshape(-1)
        expected_strided = _reference_encode_wav(strided)
        assert encode_wav(strided) == expected_strided  # ordre identique

    def test_clipping_boundaries(self):
        values = [1.0, -1.0, 0.5001, -0.5001, 32767 / 32767, -32767 / 32767,
                  (16383 + 0.5) / 32767, (32766 + 0.5) / 32767]
        audio = np.array(values, dtype=np.float32)
        reference = _reference_encode_wav(audio)
        assert encode_wav(audio) == reference
        assert float32_to_int16(audio) == reference[44:]

    def test_nan_inf_clip_like_old_loop(self):
        """NaN/+inf -> +1.0, -inf -> -1.0 (même comportement que l'ancienne
        boucle : toute comparaison avec NaN est False)."""
        values = [math.nan, math.inf, -math.inf, 0.0]
        audio = np.array(values, dtype=np.float32)
        reference = _reference_encode_wav(audio)
        assert encode_wav(audio) == reference
        assert float32_to_int16(audio) == reference[44:]

    def test_wav_header_is_valid_pcm16(self):
        """L'en-tête WAV reste valide (RIFF/WAVE, 1 canal, 16 kHz, PCM16)."""
        audio = np.array([0.5, -0.5, 0.0], dtype=np.float32)
        raw = encode_wav(audio)
        assert raw[:4] == b"RIFF"
        assert raw[8:12] == b"WAVE"
        assert raw[20:22] == struct.pack("<h", 1)      # format PCM
        assert raw[22:24] == struct.pack("<h", 1)      # mono
        assert raw[32:34] == struct.pack("<h", 2)      # block align (2 o/éch)
        assert raw[34:36] == struct.pack("<h", 16)     # 16 bits
        assert raw[44:] == float32_to_int16(audio)      # frames == brut
