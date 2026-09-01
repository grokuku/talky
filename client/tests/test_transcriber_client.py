# -*- coding: utf-8 -*-
"""
tests/test_transcriber_client.py
================================
P2 — Client HTTP vers le serveur de transcription whisper-live (§5.4 roadmap.md).

Tous les tests passent par httpx.MockTransport : le serveur whisper-live n'est
JAMAIS contacté. Couvre encode_wav (WAV PCM16 16 kHz mono valide), le
multipart envoyé (URL/méthode/params/headers/fichier, sans task/temperature),
le parse verbose_json, le mapping d'erreurs FR (§3.4), ping (GET /docs
avec repli /openapi.json), list_models (liste locale) et la normalisation
de server_url.
"""

import io
import math
import struct
import wave

import httpx
import numpy as np
import pytest

from app.core.config import DEFAULT_CONFIG
from app.engine.transcriber_client import (
    _ERR_SERVER,
    TranscriptionError,
    TranscriptionResult,
    WHISPERLIVE_MODELS,
    download_model,
    encode_wav,
    list_models,
    list_registry,
    ping,
    transcribe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _config(**overrides):
    """Configuration complète valide, éventuellement surchargée."""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


def _sine(duration=1.0, freq=440.0, rate=16000):
    """Sinus 440 Hz float32 (compatible numpy réel ET mock conftest)."""
    n = int(round(duration * rate))
    return np.array(
        [math.sin(2 * math.pi * freq * i / rate) for i in range(n)],
        dtype=np.float32,
    )


def _multipart_fields(request):
    """Champs multipart reçus par le handler MockTransport -> dict."""
    return dict(request.extensions["multipart"]["fields"])


# ---------------------------------------------------------------------------
# encode_wav
# ---------------------------------------------------------------------------
class TestEncodeWav:
    def test_valid_wav_header_and_duration(self):
        raw = encode_wav(_sine(1.0))
        with wave.open(io.BytesIO(raw), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16000
            assert wav.getsampwidth() == 2
            assert wav.getnframes() == 16000
            assert wav.getnframes() / wav.getframerate() == pytest.approx(1.0)
            frames = wav.readframes(wav.getnframes())
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        assert min(samples) >= -32768
        assert max(samples) <= 32767

    def test_samples_clipped_and_scaled(self):
        audio = np.array([2.0, -2.0, 0.5], dtype=np.float32)
        raw = encode_wav(audio)
        with wave.open(io.BytesIO(raw), "rb") as wav:
            assert wav.getnframes() == 3
            frames = wav.readframes(3)
        samples = struct.unpack("<3h", frames)
        assert samples[0] == 32767           # +1.0 (clipé depuis 2.0) -> 32767
        assert samples[1] == -32767          # -1.0 (clipé depuis -2.0) -> -32767
        assert abs(samples[2] - 16384) <= 1  # 0.5 * 32767 ~ 16384


# ---------------------------------------------------------------------------
# transcribe : multipart, headers, parse, erreurs
# ---------------------------------------------------------------------------
class TestTranscribe:
    def test_post_url_method_and_multipart(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={
                "text": "Bonjour le monde",
                "language": "fr",
                "duration": 1.24,
                "segments": [],
            })

        transport = httpx.MockTransport(handler)
        config = _config(server_url="http://192.168.1.50:8000/",
                         server_api_key="secret")
        result = transcribe(_sine(1.0), config, transport=transport)

        req = captured["request"]
        assert req.method == "POST"
        assert str(req.url) == \
            "http://192.168.1.50:8000/v1/audio/transcriptions"
        assert req.headers.get("Authorization") == "Bearer secret"

        fields = _multipart_fields(req)
        assert fields["model"] == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        assert fields["language"] == "fr"
        assert fields["vad_filter"] == "true"
        assert fields["response_format"] == "verbose_json"
        assert fields["compute_type"] == "int8"
        # task/temperature ne sont PAS OpenAI-compatible côté whisper-live :
        # ils ne doivent plus être envoyés dans le multipart.
        assert "task" not in fields
        assert "temperature" not in fields

        filename, content, content_type = fields["file"]
        assert filename == "audio.wav"
        assert content_type == "audio/wav"
        with wave.open(io.BytesIO(content), "rb") as wav:
            assert wav.getframerate() == 16000

        # parse verbose_json -> TranscriptionResult
        assert isinstance(result, TranscriptionResult)
        assert result.text == "Bonjour le monde"
        assert result.language == "fr"
        assert result.duration == 1.24

    def test_no_api_key_no_authorization_header(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"text": "ok"})

        transport = httpx.MockTransport(handler)
        transcribe(_sine(0.5), _config(server_api_key=""), transport=transport)
        assert "Authorization" not in captured["request"].headers

    @pytest.mark.parametrize("language", ["auto", None, ""])
    def test_language_omitted_when_auto(self, language):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"text": "ok"})

        transport = httpx.MockTransport(handler)
        transcribe(_sine(0.5), _config(language=language), transport=transport)
        assert "language" not in _multipart_fields(captured["request"])

    def test_vad_filter_false(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"text": "ok"})

        transport = httpx.MockTransport(handler)
        transcribe(_sine(0.5), _config(vad_filter=False), transport=transport)
        assert _multipart_fields(captured["request"])["vad_filter"] == "false"

    def test_compute_type_sent_in_form(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"text": "ok"})

        transport = httpx.MockTransport(handler)
        transcribe(_sine(0.5), _config(compute_type="float16"),
                   transport=transport)
        assert _multipart_fields(captured["request"])["compute_type"] == "float16"

    @pytest.mark.parametrize("payload", [
        {"text": "", "language": "fr", "duration": 0.5},
        {"text": "   ", "language": "fr", "duration": 0.5},
        {"language": "fr"},              # clé text absente
        {},                              # réponse vide
    ])
    def test_empty_text_returns_none(self, payload):
        def handler(request):
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(handler)
        assert transcribe(_sine(0.5), _config(), transport=transport) is None

    # --- Mapping erreurs (§3.4 roadmap.md) ---
    @pytest.mark.parametrize("scenario,expected", [
        ("connect_error", "Serveur injoignable — vérifier server_url"),
        ("connect_timeout", "Serveur injoignable — vérifier server_url"),
        ("read_timeout", "Le serveur a mis trop de temps à répondre"),
        (401, "Authentification refusée (API key)"),
        (403, "Authentification refusée (API key)"),
        (404, "Endpoint introuvable — vérifier server_url"),
        (422, "Requête invalide (modèle ou langue)"),
        (413, "Fichier audio trop volumineux"),
        (500, "Erreur serveur (GPU) — réessayer"),
        (503, "Serveur occupé — tous les modèles sont en cours d'utilisation, "
              "réessaie dans un instant"),
        ("bad_json", "Réponse serveur inattendue"),
        ("empty_200", "Réponse serveur inattendue"),
    ])
    def test_error_mapping(self, scenario, expected):
        def handler(request, _s=scenario):
            if _s == "connect_error":
                raise httpx.ConnectError("connection refused")
            if _s == "connect_timeout":
                raise httpx.ConnectTimeout("connect timed out")
            if _s == "read_timeout":
                raise httpx.ReadTimeout("read timed out")
            if _s == "bad_json":
                return httpx.Response(200, text="pas du json valide")
            if _s == "empty_200":
                return httpx.Response(200, text="")
            return httpx.Response(_s, json={"error": "boom"})

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc_info:
            transcribe(_sine(0.5), _config(), transport=transport)
        assert str(exc_info.value) == expected


# ---------------------------------------------------------------------------
# ping (GET /docs, repli /openapi.json sur 404/405)
# ---------------------------------------------------------------------------
class TestPing:
    def test_docs_ok(self):
        """ping sonde d'abord GET /docs (Swagger UI FastAPI whisper-live)."""
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        result = ping("http://192.168.1.50:8000/", transport=transport)
        assert result["reachable"] is True
        assert result["status"] == 200
        assert captured["request"].method == "GET"
        assert str(captured["request"].url) == \
            "http://192.168.1.50:8000/docs"

    def test_openapi_fallback_on_404(self):
        """404 sur /docs (UI non servie) → repli GET /openapi.json."""
        hits = []

        def handler(request):
            hits.append(str(request.url))
            if request.url.path == "/docs":
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        result = ping("http://192.168.1.50:8000", transport=transport)
        assert result["reachable"] is True
        assert result["status"] == 200
        assert hits == [
            "http://192.168.1.50:8000/docs",
            "http://192.168.1.50:8000/openapi.json",
        ]

    def test_openapi_fallback_on_405(self):
        """405 sur /docs → repli GET /openapi.json également."""
        hits = []

        def handler(request):
            hits.append(str(request.url))
            if request.url.path == "/docs":
                return httpx.Response(405, json={"detail": "method not allowed"})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        result = ping("http://192.168.1.50:8000", transport=transport)
        assert result["reachable"] is True
        assert hits[-1].endswith("/openapi.json")

    def test_docs_with_api_key(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        result = ping("http://192.168.1.50:8000", api_key="k",
                      transport=transport)
        assert result["reachable"] is True
        assert captured["request"].headers.get("Authorization") == "Bearer k"

    def test_connect_error_unreachable_no_exception(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        result = ping("http://192.168.1.50:8000", transport=transport)
        assert result["reachable"] is False
        assert "error" in result

    def test_http_error_unreachable_no_exception(self):
        def handler(request):
            return httpx.Response(500, json={"detail": "boom"})

        transport = httpx.MockTransport(handler)
        result = ping("http://192.168.1.50:8000", transport=transport)
        assert result["reachable"] is False
        assert "error" in result

    def test_404_then_openapi_error_unreachable(self):
        """404 sur /docs ET /openapi.json en erreur → injoignable."""
        hits = []

        def handler(request):
            hits.append(str(request.url))
            if request.url.path == "/docs":
                return httpx.Response(404, json={})
            return httpx.Response(500, json={"detail": "boom"})

        transport = httpx.MockTransport(handler)
        result = ping("http://192.168.1.50:8000", transport=transport)
        assert result["reachable"] is False
        assert "error" in result
        assert hits[-1].endswith("/openapi.json")


class TestListModels:
    """list_models interroge GET /v1/models du serveur talky (modèles
    installés) ; en cas d'échec (404, erreur réseau, liste vide), repli sur
    la liste LOCALE WHISPERLIVE_MODELS."""

    def test_returns_server_models_when_200(self):
        """GET /v1/models -> 200 avec liste : on retourne la liste du serveur."""
        def handler(request):
            assert request.method == "GET"
            assert str(request.url) == "http://192.168.1.50:8000/v1/models"
            return httpx.Response(200, json=["mobiuslabsgmbh/faster-whisper-large-v3-turbo", "Systran/faster-whisper-medium", "Systran/faster-whisper-tiny"])

        transport = httpx.MockTransport(handler)
        models = list_models("http://192.168.1.50:8000", transport=transport)
        assert models == ["mobiuslabsgmbh/faster-whisper-large-v3-turbo", "Systran/faster-whisper-medium", "Systran/faster-whisper-tiny"]

    def test_fallback_local_on_404(self):
        """404 sur /v1/models (endpoint absent) -> repli liste locale."""
        def handler(request):
            return httpx.Response(404, json={"detail": "not found"})

        transport = httpx.MockTransport(handler)
        models = list_models("http://192.168.1.50:8000", transport=transport)
        assert models == list(WHISPERLIVE_MODELS)

    def test_fallback_local_on_network_error(self):
        """Erreur réseau (ConnectError) -> repli liste locale."""
        def handler(request):
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        models = list_models("http://192.168.1.50:8000", transport=transport)
        assert models == list(WHISPERLIVE_MODELS)

    def test_fallback_local_on_empty_list(self):
        """200 avec liste vide -> repli liste locale (jamais vide)."""
        def handler(request):
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        models = list_models("http://192.168.1.50:8000", transport=transport)
        assert models == list(WHISPERLIVE_MODELS)

    def test_fallback_local_on_non_list_response(self):
        """200 avec un dict (format inattendu) -> repli liste locale."""
        def handler(request):
            return httpx.Response(200, json={"error": "unexpected"})

        transport = httpx.MockTransport(handler)
        models = list_models("http://192.168.1.50:8000", transport=transport)
        assert models == list(WHISPERLIVE_MODELS)

    def test_returns_fresh_copy(self):
        """La liste retournée est une copie (mutation sans effet global)."""
        models = list_models("")
        models.clear()
        assert list_models("") == list(WHISPERLIVE_MODELS)

    def test_works_with_empty_server_url(self):
        """Serveur injoignable / URL vide -> liste locale quand même dispo."""
        assert list_models("") == list(WHISPERLIVE_MODELS)
        assert list_models(None) == list(WHISPERLIVE_MODELS)

    def test_api_key_sent_as_bearer(self):
        """Une clé API est envoyée en header Authorization."""
        captured = {}

        def handler(request):
            captured["headers"] = request.headers
            return httpx.Response(200, json=["Systran/faster-whisper-tiny"])

        transport = httpx.MockTransport(handler)
        list_models("http://192.168.1.50:8000", api_key="tok", transport=transport)
        assert captured["headers"].get("Authorization") == "Bearer tok"


# ---------------------------------------------------------------------------
# list_registry (GET /v1/registry — serveur talky)
# ---------------------------------------------------------------------------
class TestListRegistry:
    def test_returns_models_from_registry(self):
        """GET /v1/registry -> 200 avec {"models": [...]} : on retourne la liste."""
        def handler(request):
            assert request.method == "GET"
            assert str(request.url).startswith("http://192.168.1.50:8000/v1/registry")
            return httpx.Response(200, json={
                "task": "automatic-speech-recognition",
                "models": [
                    "Systran/faster-whisper-tiny",
                    "Systran/faster-whisper-medium",
                ],
            })

        transport = httpx.MockTransport(handler)
        models = list_registry("http://192.168.1.50:8000", transport=transport)
        assert models == [
            "Systran/faster-whisper-tiny",
            "Systran/faster-whisper-medium",
        ]

    def test_returns_list_when_response_is_plain_list(self):
        """Le serveur peut renvoyer une liste JSON directe."""
        def handler(request):
            return httpx.Response(200, json=["model-a", "model-b"])

        transport = httpx.MockTransport(handler)
        models = list_registry("http://192.168.1.50:8000", transport=transport)
        assert models == ["model-a", "model-b"]

    def test_returns_empty_on_404(self):
        """404 sur /v1/registry -> liste vide (jamais d'exception)."""
        def handler(request):
            return httpx.Response(404, json={"detail": "not found"})

        transport = httpx.MockTransport(handler)
        assert list_registry("http://192.168.1.50:8000", transport=transport) == []

    def test_returns_empty_on_network_error(self):
        """Erreur réseau -> liste vide (jamais d'exception)."""
        def handler(request):
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        assert list_registry("http://192.168.1.50:8000", transport=transport) == []

    def test_returns_empty_on_empty_server_url(self):
        """URL vide -> liste vide (pas d'appel réseau)."""
        def handler(request):
            raise AssertionError("list_registry ne doit pas faire d'appel réseau")

        transport = httpx.MockTransport(handler)
        assert list_registry("", transport=transport) == []
        assert list_registry(None, transport=transport) == []

    def test_api_key_sent_as_bearer(self):
        captured = {}

        def handler(request):
            captured["headers"] = request.headers
            return httpx.Response(200, json={"models": []})

        transport = httpx.MockTransport(handler)
        list_registry("http://192.168.1.50:8000", api_key="k", transport=transport)
        assert captured["headers"].get("Authorization") == "Bearer k"


# ---------------------------------------------------------------------------
# download_model (POST /v1/models — serveur talky, body JSON)
# ---------------------------------------------------------------------------
class TestDownloadModel:
    def test_success_returns_confirmation(self):
        """POST /v1/models (body {"model": ...}) -> 200 : on retourne le dict de confirmation."""
        captured = {}

        def handler(request):
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = request.json()
            return httpx.Response(200, json={
                "model": "Systran/faster-whisper-medium",
                "repo": "Systran/faster-whisper-medium",
                "status": "downloaded",
                "cache": "/var/lib/whisper-live/models--Systran--faster-whisper-medium",
            })

        transport = httpx.MockTransport(handler)
        result = download_model("http://192.168.1.50:8000",
                                model="Systran/faster-whisper-medium",
                                transport=transport)
        assert captured["method"] == "POST"
        assert captured["url"] == "http://192.168.1.50:8000/v1/models"
        assert captured["body"] == {"model": "Systran/faster-whisper-medium"}
        assert result["model"] == "Systran/faster-whisper-medium"
        assert result["status"] == "downloaded"

    def test_model_id_sent_in_json_body(self):
        """Un repo ID avec slash (HF) est envoyé dans le corps JSON, pas dans
        l'URL (fix 404 : FastAPI ne matche pas %2F dans un paramètre de route)."""
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["body"] = request.json()
            return httpx.Response(200, json={"model": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"})

        transport = httpx.MockTransport(handler)
        download_model("http://192.168.1.50:8000",
                       model="mobiuslabsgmbh/faster-whisper-large-v3-turbo", transport=transport)
        assert captured["url"] == "http://192.168.1.50:8000/v1/models"
        assert captured["body"] == \
            {"model": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}

    def test_connect_error_raises(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert "Serveur injoignable" in str(exc.value)

    def test_read_timeout_raises(self):
        def handler(request):
            raise httpx.ReadTimeout("read timed out")

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert "trop de temps" in str(exc.value)

    def test_404_raises_not_found(self):
        def handler(request):
            return httpx.Response(404, json={"detail": "model not found"})

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert "Endpoint introuvable" in str(exc.value)

    def test_500_raises_server_error(self):
        def handler(request):
            return httpx.Response(500, json={"detail": "GPU error"})

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert "Erreur serveur" in str(exc.value)

    def test_500_with_json_detail_appends_server_detail(self):
        """Un 500 avec {"detail": "<cause>"} : le détail serveur est accolé
        au message générique (cause réelle visible par l'utilisateur).
        Ici, le serveur renvoie la cause réelle d'un échec de téléchargement
        (repo introuvable côté HuggingFace)."""
        def handler(request):
            return httpx.Response(
                500, json={"detail": "Échec téléchargement Systran/faster-whisper-tiny : "
                                  "Repository not found for repo Systran/faster-whisper-tiny."})

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000",
                           model="Systran/faster-whisper-tiny", transport=transport)
        assert "Erreur serveur" in str(exc.value)
        assert "Repository not found" in str(exc.value)

    def test_500_without_json_detail_stays_generic(self):
        """Un 500 sans champ ``detail`` exploitable : message générique seul
        (pas de " : " parasite)."""
        def handler(request):
            return httpx.Response(500, json={"error": "boom"})

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert str(exc.value) == _ERR_SERVER

    def test_500_with_long_detail_is_truncated(self):
        """Un détail très long est tronqué (~200 caractères max)."""
        long_detail = "x" * 500

        def handler(request):
            return httpx.Response(500, json={"detail": long_detail})

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert len(str(exc.value)) <= len(_ERR_SERVER) + 3 + 200
        # Le préfixe générique est conservé.
        assert str(exc.value).startswith(_ERR_SERVER)

    def test_500_with_plain_text_no_detail_stays_generic(self):
        """Un 500 dont le body n'est pas du JSON : message générique seul."""
        def handler(request):
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert str(exc.value) == _ERR_SERVER

    def test_500_validation_list_detail_appended(self):
        """Erreur de validation pydantic (liste d'objets avec ``msg``) : le
        message est extrait du premier élément."""
        def handler(request):
            return httpx.Response(500, json=[{"loc": ["model"],
                                              "msg": "trop long", "type": "value_error"}])

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert "trop long" in str(exc.value)

    def test_422_validation_detail_appended(self):
        """Un 422 (validation) relève du chemin < 500 : message générique
        « inattendu » avec le détail pydantic accolé."""
        def handler(request):
            return httpx.Response(422, json=[{"loc": ["body", "model"],
                                              "msg": "field required",
                                              "type": "missing"}])

        transport = httpx.MockTransport(handler)
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="Systran/faster-whisper-medium",
                           transport=transport)
        assert "Réponse serveur inattendue" in str(exc.value)
        assert "field required" in str(exc.value)

    def test_empty_model_raises(self):
        with pytest.raises(TranscriptionError) as exc:
            download_model("http://192.168.1.50:8000", model="")
        assert "Requête invalide" in str(exc.value)

    def test_empty_server_url_raises(self):
        with pytest.raises(TranscriptionError) as exc:
            download_model("", model="Systran/faster-whisper-medium")
        assert "Serveur injoignable" in str(exc.value)

    def test_api_key_sent_as_bearer(self):
        captured = {}

        def handler(request):
            captured["headers"] = request.headers
            return httpx.Response(200, json={"model": "Systran/faster-whisper-medium"})

        transport = httpx.MockTransport(handler)
        download_model("http://192.168.1.50:8000", api_key="tok",
                       model="Systran/faster-whisper-medium", transport=transport)
        assert captured["headers"].get("Authorization") == "Bearer tok"


# ---------------------------------------------------------------------------
# Normalisation de server_url
# ---------------------------------------------------------------------------
class TestServerUrlNormalization:
    def test_transcribe_strips_trailing_slashes(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"text": "ok"})

        transport = httpx.MockTransport(handler)
        transcribe(_sine(0.5), _config(server_url="http://192.168.1.50:8000///"),
                   transport=transport)
        assert str(captured["request"].url) == \
            "http://192.168.1.50:8000/v1/audio/transcriptions"

    def test_ping_strips_trailing_slash(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        ping("http://192.168.1.50:8000/", transport=transport)
        assert str(captured["request"].url) == \
            "http://192.168.1.50:8000/docs"

    def test_empty_server_url_raises_transcription_error(self):
        with pytest.raises(TranscriptionError) as exc_info:
            transcribe(_sine(0.5), _config(server_url=""))
        assert str(exc_info.value) == "Serveur injoignable — vérifier server_url"
