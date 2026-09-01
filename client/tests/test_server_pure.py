# -*- coding: utf-8 -*-
"""
tests/test_server_pure.py
=========================
Tests purs du serveur talky (sans GPU ni modèles).

Ces tests ne nécessitent ni GPU ni faster-whisper : ils couvrent des
fonctions pures de ``server/server.py`` (decode_wav 24 bits, auth middleware
on/off). L'import est **conditionnel** : si les dépendances serveur
(fastapi, uvicorn, pydantic, huggingface_hub) ne sont pas installées, le
module est ignoré (pytest.skip) — cf. acceptance « fonctions testables via
import conditionnel ».
"""

import io
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

# Ajoute le dossier server/ au path pour importer server.server.
_SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

try:
    import server.server as srv  # noqa: E402
except Exception:  # noqa: BLE001 — deps serveur absentes (ou mock fastapi sans middleware)
    pytest.skip(
        "dépendances serveur absentes (fastapi/uvicorn/pydantic/huggingface_hub) — "
        "tests serveur purs ignorés", allow_module_level=True)


def _make_wav(samples: np.ndarray, sampwidth: int, nch: int = 1,
              sr: int = 16000) -> bytes:
    """Encode un WAV en mémoire (PCM brut, mono par défaut)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sampwidth)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def test_decode_wav_24bit():
    """24-bit empaqueté en 3 octets little-endian -> float32 normalisé 2**23."""
    vals = [0x000000, 0x7FFFFF, 0x800000, 0xFFFFFF]
    packed = bytearray()
    for v in vals:
        packed += bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF])
    wav = _make_wav(np.frombuffer(bytes(packed), dtype=np.uint8), sampwidth=3)
    out = srv.decode_wav(wav)
    expected = np.array([0.0, 1.0, -1.0, -1.0 / 8388608.0], dtype=np.float32)
    np.testing.assert_allclose(out, expected, atol=1e-6)


def test_decode_wav_16bit():
    """Régression : le chemin 16 bits reste correct après le déplacement du fix 24 bits."""
    raw = np.array([0, 32767, -32768], dtype=np.int16)
    wav = _make_wav(raw, sampwidth=2)
    out = srv.decode_wav(wav)
    expected = np.array([0.0, 32767 / 32768.0, -1.0], dtype=np.float32)
    np.testing.assert_allclose(out, expected, atol=1e-6)


def test_auth_middleware_on_off():
    """Le middleware d'auth n'est défini que si TALKY_API_KEY est non vide."""
    if srv.API_KEY:
        assert hasattr(srv, "_require_api_key")
    else:
        assert not hasattr(srv, "_require_api_key")


def test_is_ct2_candidate_faster_whisper_in_id():
    """Un id contenant "faster-whisper" est un candidat CT2 (même sans tag)."""
    assert srv._is_ct2_candidate(
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo", None) is True


def test_is_ct2_candidate_ctranslate2_tag():
    """Un id quelconque + tag ctranslate2 est un candidat CT2."""
    assert srv._is_ct2_candidate(
        "deepdml/faster-whisper-large-v3-turbo-ct2",
        ["ctranslate2", "whisper"]) is True
    # Insensible à la casse.
    assert srv._is_ct2_candidate("foo/bar", ["CTranslate2"]) is True


def test_is_ct2_candidate_rejects_openai_pytorch():
    """openai/whisper-* (PyTorch original) sans tag ctranslate2 est rejeté."""
    assert srv._is_ct2_candidate(
        "openai/whisper-large-v3-turbo", ["whisper", "pytorch"]) is False


def test_is_ct2_candidate_tags_none():
    """tags None et id sans faster-whisper -> False."""
    assert srv._is_ct2_candidate("openai/whisper-tiny", None) is False
    assert srv._is_ct2_candidate("", None) is False
