# -*- coding: utf-8 -*-
"""
tests/test_api.py
=================
P6 — API locale FastAPI complète (roadmap §5.9) : routes présentes,
configuration (round-trip / validation / apply_config), contrôle moteur,
devices audio, section Serveur (ping / list_models) et WebSocket /ws.

Exécution sans fastapi installé
-------------------------------
L'environnement de développement peut ne pas avoir fastapi/uvicorn
(conftest.py installe alors un mock fonctionnel de `fastapi` : routing +
TestClient + WebSocket, pattern « try import, sinon mock » identique aux
autres dépendances). Dans ce cas les tests tournent sur ce mock — les
routes et le WebSocket sont réellement exécutés, pas seulement importés.

Exécution réelle (production / CI)
----------------------------------
    pip install -r requirements.txt   # fastapi, uvicorn, httpx, ...
    cd client && pytest tests/test_api.py -q

Avec fastapi installé, le vrai TestClient de fastapi (starlette/uvicorn)
est utilisé : mêmes assertions, aucun changement de code nécessaire.
Les mocks réseau utilisent httpx.MockTransport (le serveur whisper-live n'est
JAMAIS contacté) et le moteur ne touche ni au matériel ni au clavier
(sounddevice/evdev mockés, hotkeys neutralisées).
"""

import time

import httpx
import pytest

from app.api.factory import build_app
from app.core.config import DEFAULT_CONFIG
from app.engine.transcriber_client import WHISPERLIVE_MODELS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def wait_until(predicate, timeout=5.0, interval=0.01):
    """Attend que `predicate` soit vrai (polling court, compatible CI)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def _reset_engine():
    """Réinitialise le singleton moteur (app.api.dependencies.engine) entre
    chaque test : moteur arrêté, config par défaut, historique vidé.

    Les routes API utilisent l'instance unique créée à l'import de
    dependencies.py — sans remise à zéro, un POST /api/config ou
    /api/engine/* contaminerait les tests suivants.
    """
    from app.api.dependencies import engine
    from app.core.config import load_config

    def _reset():
        engine.stop()
        engine.config.clear()
        engine.config.update(load_config())
        engine.clear_history()
        engine.pop_events()

    _reset()
    yield
    _reset()


def _mock_server_up(monkeypatch):
    """Mocke ping via httpx.MockTransport (serveur « up »).

    ping sonde désormais GET /docs (repli /openapi.json) : le handler
    retourne 200 sur /docs. list_models n'est pas mocké : il tente un vrai
    GET /v1/models (échoue en test, aucun serveur local) et retombe sur la
    liste locale WHISPERLIVE_MODELS.
    Retourne la liste des URL sondées (assertions sur /docs).
    """
    from app.engine import transcriber_client

    real_ping = transcriber_client.ping
    hits = []

    def handler_docs(request):
        hits.append(str(request.url))
        if request.url.path == "/openapi.json":
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={})

    def fake_ping(server_url, api_key="", timeout=3.0):
        return real_ping(
            server_url, api_key, timeout=timeout,
            transport=httpx.MockTransport(handler_docs))

    monkeypatch.setattr(transcriber_client, "ping", fake_ping)
    return hits


def _mock_server_down(monkeypatch):
    """Mocke ping : serveur injoignable (reachable False). list_models n'est
    pas mocké — GET /v1/models échoue et retombe sur la liste locale."""
    from app.engine import transcriber_client

    monkeypatch.setattr(
        transcriber_client, "ping",
        lambda url, key="", timeout=3.0: {"reachable": False, "error": "HTTP 500"})


# ---------------------------------------------------------------------------
# Routes présentes
# ---------------------------------------------------------------------------
def test_all_routes_registered():
    app = build_app()
    routes = {r.path for r in app.routes}
    for path in ("/", "/api/config", "/api/engine", "/api/engine/start",
                 "/api/engine/stop", "/api/engine/restart",
                 "/api/history", "/api/devices/audio",
                 "/api/server/status", "/api/server/test",
                 "/api/server/registry", "/api/server/models/download",
                 "/ws"):
        assert path in routes, f"Route manquante : {path}"


# ---------------------------------------------------------------------------
# HTTP : page d'accueil + configuration
# ---------------------------------------------------------------------------
class TestHttp:
    def test_index_page(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "Talky" in res.text

    def test_get_config(self, client):
        res = client.get("/api/config")
        assert res.status_code == 200
        body = res.json()
        assert body["model"] == DEFAULT_CONFIG["model"]
        assert body["hotkey"] == DEFAULT_CONFIG["hotkey"]
        assert body["server_url"] == DEFAULT_CONFIG["server_url"]

    def test_post_config_hot_change(self, client):
        res = client.post("/api/config", json={"model": "Systran/faster-whisper-small"})
        assert res.status_code == 200
        body = res.json()
        assert body["saved"] is True
        assert body["reload_needed"] is False
        assert "model" in body["live_changed"]
        assert body["config"]["model"] == "Systran/faster-whisper-small"
        # Persisté sur disque (config.json restauré par la fixture autouse).
        from app.core.config import load_config
        assert load_config()["model"] == "Systran/faster-whisper-small"

    def test_post_config_reload_needed(self, client):
        res = client.post("/api/config", json={"audio_device": 1})
        assert res.status_code == 200
        body = res.json()
        assert body["saved"] is True
        assert body["reload_needed"] is True
        assert "audio_device" not in body["live_changed"]  # RELOAD_FIELD

    def test_post_config_invalid_returns_400(self, client):
        res = client.post("/api/config", json={"server_timeout": 2})
        assert res.status_code == 400
        body = res.json()
        assert body["saved"] is False
        assert "errors" in body

    def test_post_config_empty_url_returns_400(self, client):
        res = client.post("/api/config", json={"server_url": ""})
        assert res.status_code == 400
        assert res.json()["saved"] is False


# ---------------------------------------------------------------------------
# Moteur
# ---------------------------------------------------------------------------
class TestEngine:
    def test_engine_state(self, client):
        res = client.get("/api/engine")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "idle"
        assert body["running"] is False

    def test_engine_start_boots_to_ready(self, client, monkeypatch):
        from app.api.dependencies import engine

        # hotkeys globales : install() lèverait HotkeyError (aucun
        # /dev/input dans l'environnement de test) — on neutralise.
        monkeypatch.setattr(engine, "_install_hotkeys", lambda: None)
        monkeypatch.setattr(engine, "_uninstall_hotkeys", lambda: None)

        res = client.post("/api/engine/start")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["state"]["status"] in ("booting", "ready")

        # Le thread de boot se termine : le moteur passe à ready.
        assert wait_until(lambda: engine.snapshot()["status"] == "ready")
        assert engine.snapshot()["running"] is True

    def test_engine_stop(self, client):
        res = client.post("/api/engine/stop")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["state"]["status"] == "idle"

    def test_engine_restart(self, client, monkeypatch):
        from app.api.dependencies import engine

        monkeypatch.setattr(engine, "_install_hotkeys", lambda: None)
        monkeypatch.setattr(engine, "_uninstall_hotkeys", lambda: None)

        res = client.post("/api/engine/restart")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["state"]["status"] in ("booting", "ready")


# ---------------------------------------------------------------------------
# Historique + devices audio
# ---------------------------------------------------------------------------
class TestHistoryAndDevices:
    def test_history_empty(self, client):
        res = client.get("/api/history")
        assert res.status_code == 200
        assert res.json() == {"history": []}

    def test_clear_history(self, client):
        res = client.delete("/api/history")
        assert res.status_code == 200
        assert res.json() == {"ok": True}

    def test_audio_devices(self, client):
        res = client.get("/api/devices/audio")
        assert res.status_code == 200
        body = res.json()
        assert "devices" in body
        assert "default" in body


# ---------------------------------------------------------------------------
# Section Serveur (ping / list_models via MockTransport)
# ---------------------------------------------------------------------------
class TestServer:
    def test_server_status_reachable(self, client, monkeypatch):
        hits = _mock_server_up(monkeypatch)
        res = client.get("/api/server/status")
        assert res.status_code == 200
        body = res.json()
        assert body["reachable"] is True
        assert body["device"] == "cuda"
        assert body["compute_type"] == "int8"
        assert body["model"] == DEFAULT_CONFIG["model"]
        assert body["models"] == list(WHISPERLIVE_MODELS)
        assert body["message"] == "Serveur talky détecté"
        # ping passe par GET /docs (repli /openapi.json, avec la clé) : le
        # serveur talky expose désormais /health (exempté d'auth) mais ping()
        # ne le sonde pas — le mock ne sert que /docs et /openapi.json.
        assert any(url.endswith("/docs") for url in hits)
        assert not any(url.endswith("/openapi.json") for url in hits)

    def test_server_status_compute_type_from_config(self, client, monkeypatch):
        """Le compute_type retourné vient de la config, pas de SERVER_DEFAULTS."""
        _mock_server_up(monkeypatch)
        # Sauvegarde un compute_type personnalisé dans la config.
        res = client.post("/api/config", json={"compute_type": "float16"})
        assert res.status_code == 200
        res = client.get("/api/server/status")
        assert res.status_code == 200
        body = res.json()
        assert body["compute_type"] == "float16"

    def test_server_status_unreachable_no_exception(self, client, monkeypatch):
        _mock_server_down(monkeypatch)
        res = client.get("/api/server/status")
        assert res.status_code == 200          # pas d'exception, statut 200
        body = res.json()
        assert body["reachable"] is False
        assert "error" in body
        assert body["message"] == "Serveur talky injoignable"
        # list_models retombe sur la liste locale : la liste est affichée
        # même serveur down.
        assert body["models"] == list(WHISPERLIVE_MODELS)

    def test_server_test_reachable(self, client, monkeypatch):
        hits = _mock_server_up(monkeypatch)
        res = client.post("/api/server/test")
        assert res.status_code == 200
        body = res.json()
        assert body["reachable"] is True
        assert body["url"] == DEFAULT_CONFIG["server_url"]
        assert body["latency_ms"] is not None
        assert body["models"] == list(WHISPERLIVE_MODELS)
        assert body["error"] is None
        assert body["message"] == "Serveur talky détecté"
        assert any(url.endswith("/docs") for url in hits)

    def test_server_test_unreachable(self, client, monkeypatch):
        _mock_server_down(monkeypatch)
        res = client.post("/api/server/test")
        assert res.status_code == 200
        body = res.json()
        assert body["reachable"] is False
        assert body["latency_ms"] is None
        assert body["models"] == list(WHISPERLIVE_MODELS)
        assert body["error"] is not None
        assert body["message"] == "Serveur talky injoignable"


# ---------------------------------------------------------------------------
# Section Serveur : registry + download (routes talky ré-ajoutées)
# ---------------------------------------------------------------------------
class TestServerRegistry:
    def test_registry_returns_models(self, client, monkeypatch):
        """GET /api/server/registry relaie GET /v1/registry du serveur talky."""
        from app.engine import transcriber_client

        def fake_list_registry(server_url, api_key="", timeout=10.0,
                               transport=None, task="automatic-speech-recognition"):
            return ["Systran/faster-whisper-tiny", "Systran/faster-whisper-medium"]

        monkeypatch.setattr(transcriber_client, "list_registry", fake_list_registry)
        res = client.get("/api/server/registry")
        assert res.status_code == 200
        body = res.json()
        assert body["models"] == ["Systran/faster-whisper-tiny",
                                  "Systran/faster-whisper-medium"]
        assert body["server_url"] == DEFAULT_CONFIG["server_url"]

    def test_registry_empty_on_error(self, client, monkeypatch):
        """list_registry retourne [] en cas d'échec -> {"models": []}."""
        from app.engine import transcriber_client

        monkeypatch.setattr(transcriber_client, "list_registry",
                            lambda *a, **kw: [])
        res = client.get("/api/server/registry")
        assert res.status_code == 200
        assert res.json()["models"] == []


class TestServerDownloadModel:
    def test_download_success(self, client, monkeypatch):
        """POST /api/server/models/download relaie POST /v1/models (body JSON)."""
        from app.engine import transcriber_client

        captured = {}

        def fake_download(server_url, api_key="", model="", timeout=600.0,
                          transport=None):
            captured["model"] = model
            captured["server_url"] = server_url
            return {"model": model, "repo": f"Systran/faster-whisper-{model}",
                    "status": "downloaded", "cache": "/var/lib/whisper-live/test"}

        monkeypatch.setattr(transcriber_client, "download_model", fake_download)
        res = client.post("/api/server/models/download", json={"model": "Systran/faster-whisper-medium"})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["model"] == "Systran/faster-whisper-medium"
        assert body["result"]["status"] == "downloaded"
        assert captured["model"] == "Systran/faster-whisper-medium"

    def test_download_missing_model_returns_error(self, client):
        """Body sans 'model' -> {"ok": False, "error": "Modèle manquant"}."""
        res = client.post("/api/server/models/download", json={})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert body["model"] == ""
        assert "Modèle manquant" in body["error"]

    def test_download_empty_model_returns_error(self, client):
        """Model vide -> {"ok": False, "error": "Modèle manquant"}."""
        res = client.post("/api/server/models/download", json={"model": "  "})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert "Modèle manquant" in body["error"]

    def test_download_transcription_error_propagated(self, client, monkeypatch):
        """TranscriptionError du client -> {"ok": False, "error": ...}."""
        from app.engine import transcriber_client

        def fake_download(*a, **kw):
            raise transcriber_client.TranscriptionError("Erreur serveur (GPU)")

        monkeypatch.setattr(transcriber_client, "download_model", fake_download)
        res = client.post("/api/server/models/download", json={"model": "Systran/faster-whisper-medium"})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert body["model"] == "Systran/faster-whisper-medium"
        assert "Erreur serveur (GPU)" in body["error"]


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
class TestWebSocket:
    def test_ws_hello(self, client):
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert "state" in hello["data"]
            assert "config" in hello["data"]

    def test_ws_hello_contains_snapshot(self, client):
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            state = hello["data"]["state"]
            assert state["status"] in ("idle", "booting", "ready", "error")
            assert "model" in state
            assert "config" in state
