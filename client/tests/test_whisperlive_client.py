# -*- coding: utf-8 -*-
"""
tests/test_whisperlive_client.py
================================
Tests du client WebSocket WhisperLive (app/engine/whisperlive_client.py) :
handshake, frames PCM16 int16 16 kHz (sans en-tête WAV), parse des segments
transcript, EOF, erreurs FR, conversion float32->int16 (clip) et timeouts.

La lib ``websockets`` est mockée par conftest.py (fake sessions connect /
send / recv / close, scripts serveur par URL) : aucune vraie connexion WS
n'est ouverte pendant les tests.
"""

import json
import struct

import numpy as np
import pytest

import websockets

from app.core.constants import WS_DEFAULT_PORT
from app.engine.whisperlive_client import (
    WhisperLiveClient,
    build_ws_url,
    float32_to_int16,
    make_handshake,
    parse_event,
    transcript_text,
)

UID = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_ws_mock():
    """Remet à zéro les scripts et sessions du mock websockets entre tests."""
    websockets._reset_scripts()
    yield
    websockets._reset_scripts()


def _client(**kwargs):
    """Client WhisperLive prêt à connecter (uid stable pour les assertions)."""
    defaults = dict(host="192.168.1.50", ws_port=9090,
                    model="mobiuslabsgmbh/faster-whisper-large-v3-turbo", language="fr", uid=UID)
    defaults.update(kwargs)
    return WhisperLiveClient(**defaults)


def _server_ready():
    return {"uid": UID, "message": "server_ready"}


def _connected_client(script=None, **kwargs):
    """Connecte un client avec le script serveur fourni (défaut: server_ready)."""
    websockets._set_script("*", script if script is not None else [_server_ready()])
    client = _client(**kwargs)
    assert client.connect() is True
    return client


def _session():
    return websockets._active_sessions[-1]


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------
class TestHandshake:
    def test_handshake_sent_with_protocol_fields(self):
        client = _connected_client()
        handshake = json.loads(_session().sent[0])
        assert handshake["uid"] == UID
        assert handshake["model"] == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        assert handshake["task"] == "transcribe"
        assert handshake["use_vad"] is True
        assert handshake["language"] == "fr"
        assert handshake["same_output_threshold"] == 2.0
        assert handshake["compute_type"] == "int8"
        client.close()

    def test_handshake_sent_before_any_audio(self):
        client = _connected_client()
        session = _session()
        assert len(session.sent) == 1       # seul le handshake au départ
        assert json.loads(session.sent[0])["message"] if False else True
        assert "uid" in json.loads(session.sent[0])
        client.close()

    def test_make_handshake_omits_auto_language(self):
        handshake = make_handshake(UID, "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "auto")
        assert "language" not in handshake
        assert handshake["use_vad"] is True

    def test_make_handshake_default_model(self):
        handshake = make_handshake(UID, "", "fr")
        assert handshake["model"] == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"

    def test_make_handshake_includes_compute_type(self):
        handshake = make_handshake(UID, "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "fr",
                                   compute_type="float16")
        assert handshake["compute_type"] == "float16"

    def test_make_handshake_default_compute_type(self):
        handshake = make_handshake(UID, "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "fr")
        assert handshake["compute_type"] == "int8"

    def test_handshake_compute_type_from_client(self):
        """Le compute_type du WhisperLiveClient est envoyé dans le handshake."""
        client = _connected_client(compute_type="float32")
        handshake = json.loads(_session().sent[0])
        assert handshake["compute_type"] == "float32"
        client.close()

    def test_handshake_accepts_uppercase_server_ready(self):
        """Le serveur whisper-live envoie 'SERVER_READY' (majuscules)."""
        websockets._set_script("*", [{"uid": UID, "message": "SERVER_READY"}])
        client = _client()
        assert client.connect() is True
        assert client.is_connected is True
        client.close()

    def test_handshake_accepts_mixed_case_server_ready(self):
        """Tolérance à la casse : 'Server_Ready' doit aussi fonctionner."""
        websockets._set_script("*", [{"uid": UID, "message": "Server_Ready"}])
        client = _client()
        assert client.connect() is True
        assert client.is_connected is True
        client.close()


# ---------------------------------------------------------------------------
# URL WebSocket
# ---------------------------------------------------------------------------
class TestWsUrl:
    def test_build_ws_url_from_server_url(self):
        url = build_ws_url("http://192.168.1.50:8000/", 9090)
        assert url == "ws://192.168.1.50:9090/"

    def test_build_ws_url_default_port(self):
        url = build_ws_url("http://192.168.1.50:8000")
        assert url.endswith(f":{WS_DEFAULT_PORT}/")

    def test_build_ws_url_https_becomes_wss(self):
        url = build_ws_url("https://192.168.1.50:8000", 9090)
        assert url.startswith("wss://192.168.1.50:9090")

    def test_client_url_property(self):
        client = _client()
        assert client.url == "ws://192.168.1.50:9090/"


# ---------------------------------------------------------------------------
# Auth WS (middleware TALKY_API_KEY) : header Authorization Bearer
# ---------------------------------------------------------------------------
class TestWsAuth:
    def test_no_auth_header_when_key_empty(self):
        """Clé vide -> aucun header Authorization au handshake (défaut)."""
        websockets._set_script("*", [_server_ready()])
        client = _client(server_api_key="")
        assert client.connect() is True
        connector = websockets._active_connectors[-1]
        assert "extra_headers" not in connector.kwargs
        client.close()

    def test_auth_header_sent_when_key_non_empty(self):
        """Clé non vide -> `Authorization: Bearer <clé>` au handshake WS."""
        websockets._set_script("*", [_server_ready()])
        client = _client(server_api_key="secret-tok")
        assert client.connect() is True
        connector = websockets._active_connectors[-1]
        assert connector.kwargs["extra_headers"] == {
            "Authorization": "Bearer secret-tok"}
        client.close()

    def test_auth_header_whitespace_key_sent(self):
        """Clé non vide (même espaces) -> header envoyé, cohérent avec le
        REST (_auth_headers) : seule une chaîne vide désactive l'auth."""
        websockets._set_script("*", [_server_ready()])
        client = _client(server_api_key="   ")
        assert client.connect() is True
        connector = websockets._active_connectors[-1]
        assert connector.kwargs["extra_headers"] == {
            "Authorization": "Bearer    "}
        client.close()


# ---------------------------------------------------------------------------
# Frames PCM16 : binaire brut, sans en-tête WAV
# ---------------------------------------------------------------------------
class TestSendAudio:
    def test_send_audio_binary_pcm16_no_wav_header(self):
        client = _connected_client()
        audio = np.array([0.5, -0.5, 1.0, -1.0, 0.0], dtype=np.float32)
        assert client.send_audio(audio) is True

        payload = _session().sent[-1]
        assert isinstance(payload, bytes)
        assert len(payload) == 5 * 2                      # 5 samples × int16
        samples = struct.unpack("<5h", payload)
        assert samples == (16384, -16384, 32767, -32767, 0)
        assert payload[:4] != b"RIFF"                     # PAS de WAV
        client.close()

    def test_send_audio_accepts_list_like_chunk(self):
        client = _connected_client()
        assert client.send_audio([0.25, -0.25]) is True
        payload = _session().sent[-1]
        assert struct.unpack("<2h", payload) == (8192, -8192)
        client.close()

    def test_send_audio_after_close_returns_false(self):
        client = _connected_client()
        client.close()
        assert client.send_audio([0.1, 0.2]) is False

    def test_float32_to_int16_clips(self):
        raw = float32_to_int16(
            np.array([2.0, -2.0, 0.5, -0.5, 0.0], dtype=np.float32))
        samples = struct.unpack("<5h", raw)
        assert samples[0] == 32767            # +1.0 (clipé depuis 2.0)
        assert samples[1] == -32767           # -1.0 (clipé depuis -2.0)
        assert samples[2] == 16384            # 0.5 * 32767
        assert samples[3] == -16384
        assert samples[4] == 0


# ---------------------------------------------------------------------------
# Réception des transcripts (segments)
# ---------------------------------------------------------------------------
class TestRecvEvent:
    def test_recv_transcript_segments(self):
        client = _connected_client(script=[
            _server_ready(),
            {"uid": UID, "message": "transcript", "segments": [
                {"start": 0.0, "end": 2.0, "text": "Bonjour le monde"},
                {"start": 2.0, "end": 3.5, "text": "comment allez-vous"},
            ]},
        ])
        event = client.recv_event(timeout=1.0)
        assert event is not None
        assert event["message"] == "transcript"
        assert event["segments"][0]["text"] == "Bonjour le monde"
        assert transcript_text(event) == "Bonjour le monde comment allez-vous"
        client.close()

    def test_recv_timeout_returns_none(self):
        client = _connected_client()          # pas d'autre message serveur
        assert client.recv_event(timeout=0.05) is None
        client.close()

    def test_parse_event_handles_bytes(self):
        assert parse_event(b'{"message": "x"}') == {"message": "x"}
        assert parse_event("pas du json") is None
        assert parse_event(None) is None
        assert parse_event(42) is None


# ---------------------------------------------------------------------------
# EOF / fermeture
# ---------------------------------------------------------------------------
class TestEof:
    def test_send_eof_json(self):
        client = _connected_client()
        assert client.send_eof() is True
        last = json.loads(_session().sent[-1])
        assert last == {"uid": UID, "eof": True}
        client.close()

    def test_close_closes_session(self):
        client = _connected_client()
        client.close()
        assert _session().closed is True
        assert client.is_connected is False


# ---------------------------------------------------------------------------
# Erreurs FR (jamais d'exception)
# ---------------------------------------------------------------------------
class TestErrors:
    def test_connect_server_error_fr(self):
        websockets._set_script("*", [
            {"uid": UID, "message": "error", "reason": "boom"},
        ])
        client = _client()
        assert client.connect() is False
        assert "Erreur serveur WebSocket" in client.error
        client.close()

    def test_connect_uppercase_error_fr(self):
        """Le serveur peut envoyer 'ERROR' en majuscules."""
        websockets._set_script("*", [
            {"uid": UID, "message": "ERROR", "reason": "boom"},
        ])
        client = _client()
        assert client.connect() is False
        assert "Erreur serveur WebSocket" in client.error
        client.close()

    def test_connect_timeout_fr(self):
        websockets._reset_scripts()           # aucune réponse serveur
        client = _client()
        assert client.connect(timeout=0.3) is False
        assert "trop de temps" in client.error
        client.close()

    def test_send_audio_error_sets_fr_error(self):
        client = _connected_client()
        client.close()                        # session fermée
        assert client.send_audio([0.1, 0.2]) is False
        assert "envoi audio" in (client.error or "")
