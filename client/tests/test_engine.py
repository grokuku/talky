# -*- coding: utf-8 -*-
"""
tests/test_engine.py
====================
P5 — Moteur de dictée (state + config_apply + dictation) : cycle de vie
start/stop/restart, cycle hotkey → recording → transcribing → success → ready,
transcription None / TranscriptionError, boot avec serveur injoignable,
apply_config (hotkey à chaud, audio_device → restart, langue/modèle live)
et ordre des événements (state → log → transcript).

Aucun accès au matériel ni au serveur : transcriber_client (ping/transcribe)
et inject_text sont mockés ; HotkeyManager est remplacé par une classe
factice qui expose les callbacks du moteur (simulation d'un appui hotkey).
"""

import time

import numpy as np
import pytest

from app.core.config import DEFAULT_CONFIG
from app.core.constants import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_READY,
    STATE_RECORDING,
    STATE_SUCCESS,
    STATE_TRANSCRIBING,
)
from app.engine import dictation as dictation_module
from app.engine.dictation import DictationEngine
from app.engine.transcriber_client import TranscriptionError, TranscriptionResult


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


def wait_status(engine, status, timeout=5.0):
    return wait_until(lambda: engine.snapshot()["status"] == status, timeout)


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


def feed_audio(engine, *blocks):
    """Simule l'arrivée de blocs PortAudio pendant l'enregistrement."""
    for block in blocks:
        engine._audio._callback(block, None, None, None)


class FakeHotkeyManager:
    """HotkeyManager factice : installe/désinstalle sans evdev et expose les
    callbacks du moteur pour simuler un appui/relâchement de la hotkey."""

    instances = []

    def __init__(self, hotkey, mode, on_record_start, on_record_stop):
        self.hotkey = hotkey
        self.mode = mode
        self.on_record_start = on_record_start
        self.on_record_stop = on_record_stop
        self.installed = False
        self.uninstalled = False
        self._recording_predicate = None
        FakeHotkeyManager.instances.append(self)

    def install(self):
        self.installed = True

    def uninstall(self):
        self.uninstalled = True

    def bind_recording_state(self, predicate):
        self._recording_predicate = predicate

    def press(self):
        """Simule un appui (push_to_talk -> start, toggle -> bascule)."""
        if (self.mode == "toggle" and self._recording_predicate
                and self._recording_predicate()):
            self.on_record_stop()
        else:
            self.on_record_start()

    def release(self):
        """Simule un relâchement (stop)."""
        self.on_record_stop()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fast_delays(monkeypatch):
    """Réduit les délais avant retour à ready et le drain EOF du mode WS
    (0,8 s / 2,5 s -> dizaines de ms en test)."""
    monkeypatch.setattr(dictation_module, "READY_DELAY", 0.05)
    monkeypatch.setattr(dictation_module, "WS_EOF_DRAIN_TIMEOUT", 0.3)
    monkeypatch.setattr(dictation_module, "WS_EOF_QUIET", 0.05)


@pytest.fixture
def fake_hotkeys(monkeypatch):
    """Remplace HotkeyManager par la classe factice (pas d'accès /dev/input)."""
    monkeypatch.setattr(dictation_module, "HotkeyManager", FakeHotkeyManager)
    FakeHotkeyManager.instances = []
    yield FakeHotkeyManager
    FakeHotkeyManager.instances = []


@pytest.fixture
def engine(fake_hotkeys):
    return DictationEngine(dict(DEFAULT_CONFIG))


class FakeWSClient:
    """WhisperLiveClient factice pour les tests du mode continu WS.

    Session scriptée : ``FakeWSClient.script`` (attribut de classe) est copié
    dans chaque instance à la création ; ``recv_event`` consomme la file.
    ``connect_result=False`` simule un échec de connexion (erreur FR).
    """

    instances = []
    script = []
    connect_result = True      # défaut classe : copié à la création de l'instance

    def __init__(self, host, ws_port, model, language, uid=None,
                 compute_type="int8", server_api_key=""):
        self.host = host
        self.ws_port = ws_port
        self.model = model
        self.language = language
        self.compute_type = compute_type
        self.uid = uid or "test-uid"
        self.server_api_key = str(server_api_key or "")
        self.error = None
        self.connect_result = bool(type(self).connect_result)
        self.connect_url = None
        self.sent_audio = []          # chunks reçus via send_audio
        self.eof_sent = False
        self.closed = False
        self.script = list(type(self).script)
        FakeWSClient.instances.append(self)

    def connect(self, url=None, timeout=8.0):
        self.connect_url = url
        if self.connect_result is False:
            self.error = ("Connexion WebSocket impossible — vérifier "
                          "server_url et ws_port")
            return False
        return True

    def send_audio(self, chunk):
        self.sent_audio.append(chunk)
        return True

    def recv_event(self, timeout=0.1):
        if self.script:
            return self.script.pop(0)
        return None

    def send_eof(self):
        self.eof_sent = True
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_ws_client(monkeypatch):
    """Remplace WhisperLiveClient par FakeWSClient (session scriptée)."""
    monkeypatch.setattr(dictation_module, "WhisperLiveClient", FakeWSClient)
    FakeWSClient.instances = []
    FakeWSClient.script = []
    FakeWSClient.connect_result = True
    yield FakeWSClient
    FakeWSClient.instances = []
    FakeWSClient.script = []
    FakeWSClient.connect_result = True


def _result(text="Bonjour le monde", language="fr", duration=1.24):
    return TranscriptionResult(text=text, language=language, duration=duration)


def _mock_transcribe(monkeypatch, result, delay=0.0):
    """Mocke transcriber_client.transcribe ; `delay` laisse observer le
    statut « transcribing » de manière déterministe."""
    def fake_transcribe(audio, config, transport=None):
        if delay:
            time.sleep(delay)
        return result
    monkeypatch.setattr(
        dictation_module.transcriber_client, "transcribe", fake_transcribe)


def _mock_inject(monkeypatch):
    """Mocke inject_text : enregistre les arguments, retourne True."""
    calls = []

    def fake_inject(text, *, add_space=True, inject=True,
                    keep_in_clipboard=False, log_callback=None):
        calls.append({
            "text": text,
            "add_space": add_space,
            "inject": inject,
            "keep_in_clipboard": keep_in_clipboard,
        })
        return True

    monkeypatch.setattr(dictation_module, "inject_text", fake_inject)
    return calls


def _start_and_wait_ready(engine, fake_hotkeys):
    engine.start()
    assert wait_status(engine, STATE_READY)
    return fake_hotkeys.instances[-1]


# ---------------------------------------------------------------------------
# Cycle de vie : start / boot / ready
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_start_boots_to_ready(self, engine, fake_hotkeys):
        engine.start()
        assert wait_status(engine, STATE_READY)

        snap = engine.snapshot()
        assert snap["status"] == STATE_READY
        assert snap["running"] is True
        assert snap["model"] == DEFAULT_CONFIG["model"]
        assert snap["device"] == "cuda"          # lecture seule (roadmap §5.10)
        assert snap["compute_type"] == "int8"

        assert engine._audio._stream is not None   # micro ouvert
        manager = fake_hotkeys.instances[-1]
        assert manager.installed                   # hotkeys installées
        engine.stop()

    def test_start_while_running_is_ignored(self, engine, fake_hotkeys):
        engine.start()
        assert wait_status(engine, STATE_READY)
        n_instances = len(fake_hotkeys.instances)
        engine.start()                             # déjà démarré : ignoré
        assert len(fake_hotkeys.instances) == n_instances
        engine.stop()

    def test_stop_uninstalls_hotkeys_closes_audio_and_idle(self, engine,
                                                           fake_hotkeys):
        engine.start()
        assert wait_status(engine, STATE_READY)
        manager = fake_hotkeys.instances[-1]

        engine.stop()
        assert manager.uninstalled                 # hotkeys désinstallées
        assert engine._audio._stream is None       # flux audio fermé
        assert engine.snapshot()["status"] == STATE_IDLE
        assert engine.snapshot()["running"] is False

    def test_restart_stops_then_boots_again(self, engine, fake_hotkeys):
        engine.start()
        assert wait_status(engine, STATE_READY)
        engine.restart()
        assert wait_status(engine, STATE_READY)
        assert len(fake_hotkeys.instances) == 2    # boot initial + boot restart
        assert fake_hotkeys.instances[0].uninstalled
        engine.stop()

    def test_boot_audio_error_sets_clear_error(self, engine, fake_hotkeys,
                                               monkeypatch):
        from app.engine.audio import AudioRecorderError

        def boom(device=None):
            raise AudioRecorderError(
                "Impossible d'ouvrir le périphérique audio « défaut » "
                "(16 kHz mono) : boom. Vérifier la sélection du micro.")

        monkeypatch.setattr(engine._audio, "open", boom)
        engine.start()
        assert wait_status(engine, STATE_ERROR)
        assert "périphérique audio" in engine.snapshot()["status_msg"]
        assert fake_hotkeys.instances == []        # pas de hotkeys installées
        engine.stop()

    def test_boot_hotkey_error_sets_clear_error(self, engine, fake_hotkeys,
                                                monkeypatch):
        from app.engine.hotkeys import HotkeyError

        class FailingHotkeys(FakeHotkeyManager):
            def install(self):
                raise HotkeyError(
                    "Aucun périphérique d'entrée trouvé dans /dev/input. "
                    "Ajouter l'utilisateur au groupe input.")

        monkeypatch.setattr(dictation_module, "HotkeyManager", FailingHotkeys)
        engine.start()
        assert wait_status(engine, STATE_ERROR)
        assert "groupe input" in engine.snapshot()["status_msg"]
        assert engine._audio._stream is None       # flux refermé sur échec
        engine.stop()


# ---------------------------------------------------------------------------
# Cycle hotkey : recording → transcribing → success → ready
# ---------------------------------------------------------------------------
class TestHotkeyCycle:
    def test_full_cycle_success(self, engine, fake_hotkeys, monkeypatch):
        _mock_transcribe(monkeypatch, _result(), delay=0.05)
        injected = _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)

        manager.press()                            # on_record_start
        assert engine.snapshot()["status"] == STATE_RECORDING
        assert engine.snapshot()["running"] is True
        feed_audio(engine, Block([0.1, 0.2]), Block([0.3, 0.4]))

        manager.release()                          # on_record_stop
        assert wait_status(engine, STATE_TRANSCRIBING)
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)

        # Injection appelée avec les bons paramètres (issus de la config).
        assert len(injected) == 1
        assert injected[0]["text"] == "Bonjour le monde"
        assert injected[0]["add_space"] is True
        assert injected[0]["inject"] is True
        assert injected[0]["keep_in_clipboard"] is False

        # Historique alimenté.
        hist = engine.get_history()
        assert len(hist) == 1
        assert hist[0]["text"] == "Bonjour le monde"
        assert hist[0]["language"] == "fr"
        assert hist[0]["duration"] == 1.24
        engine.stop()

    def test_transcription_none_returns_ready_without_transcript(
            self, engine, fake_hotkeys, monkeypatch):
        _mock_transcribe(monkeypatch, None, delay=0.05)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()                        # vide les événements du boot

        manager.press()
        feed_audio(engine, Block([0.1]))
        manager.release()
        assert wait_status(engine, STATE_READY)
        events = engine.pop_events()
        assert not any(e["type"] == "transcript" for e in events)
        assert engine.get_history() == []
        engine.stop()

    def test_transcription_error_error_then_ready(self, engine, fake_hotkeys,
                                                  monkeypatch):
        def boom(audio, config, transport=None):
            time.sleep(0.05)                       # laisse voir « transcribing »
            raise TranscriptionError(
                "Serveur injoignable — vérifier server_url")

        monkeypatch.setattr(
            dictation_module.transcriber_client, "transcribe", boom)
        manager = _start_and_wait_ready(engine, fake_hotkeys)

        manager.press()
        feed_audio(engine, Block([0.1]))
        manager.release()
        assert wait_status(engine, STATE_TRANSCRIBING)
        assert wait_status(engine, STATE_ERROR)
        assert "Serveur injoignable" in engine.snapshot()["status_msg"]
        assert wait_status(engine, STATE_READY)    # retour automatique à ready
        engine.stop()


# ---------------------------------------------------------------------------
# Boot : ping serveur
# ---------------------------------------------------------------------------
class TestBootPing:
    def test_boot_pings_server_with_short_timeout(self, engine, fake_hotkeys,
                                                  monkeypatch):
        captured = {}

        def fake_ping(server_url, api_key="", timeout=5.0, transport=None):
            captured.update(server_url=server_url, api_key=api_key,
                            timeout=timeout)
            return {"reachable": True, "status": 200}

        monkeypatch.setattr(
            dictation_module.transcriber_client, "ping", fake_ping)
        engine.start()
        assert wait_status(engine, STATE_READY)
        assert captured["server_url"] == DEFAULT_CONFIG["server_url"]
        assert captured["api_key"] == DEFAULT_CONFIG["server_api_key"]
        assert captured["timeout"] == 3.0          # ping court, non bloquant
        engine.stop()

    def test_boot_ok_when_server_unreachable(self, engine, fake_hotkeys,
                                             monkeypatch):
        monkeypatch.setattr(
            dictation_module.transcriber_client, "ping",
            lambda *a, **kw: {"reachable": False, "error": "boom"})
        engine.start()
        assert wait_status(engine, STATE_READY)    # boot OK quand même

        events = engine.pop_events()
        logs = [e["data"]["message"] for e in events if e["type"] == "log"]
        assert any("Serveur injoignable" in msg for msg in logs)
        engine.stop()


# ---------------------------------------------------------------------------
# apply_config
# ---------------------------------------------------------------------------
class TestApplyConfig:
    def test_hotkey_changed_reinstalled_live(self, engine, fake_hotkeys):
        manager = _start_and_wait_ready(engine, fake_hotkeys)

        reload_needed, live_changed = engine.apply_config(
            {**DEFAULT_CONFIG, "hotkey": "ctrl+space"})

        assert reload_needed is False
        assert "hotkey" in live_changed
        assert manager.uninstalled                 # ancienne instance retirée
        assert len(fake_hotkeys.instances) == 2    # réinstallation à chaud
        assert fake_hotkeys.instances[-1].installed
        assert fake_hotkeys.instances[-1].hotkey == "ctrl+space"
        engine.stop()

    def test_audio_device_change_restarts(self, engine, fake_hotkeys):
        _start_and_wait_ready(engine, fake_hotkeys)

        reload_needed, live_changed = engine.apply_config(
            {**DEFAULT_CONFIG, "audio_device": 3})

        assert reload_needed is True
        assert wait_status(engine, STATE_READY)    # restart → boot → ready
        assert len(fake_hotkeys.instances) == 2    # nouveau boot après restart
        assert engine.config["audio_device"] == 3
        engine.stop()

    def test_language_model_change_live_no_reload(self, engine, fake_hotkeys):
        _start_and_wait_ready(engine, fake_hotkeys)

        reload_needed, live_changed = engine.apply_config(
            {**DEFAULT_CONFIG, "language": "en", "model": "Systran/faster-whisper-small"})

        assert reload_needed is False
        assert "language" in live_changed
        assert "model" in live_changed
        assert len(fake_hotkeys.instances) == 1    # pas de réinstallation
        assert engine.config["language"] == "en"
        assert engine.config["model"] == "Systran/faster-whisper-small"
        engine.stop()


# ---------------------------------------------------------------------------
# Événements & historique
# ---------------------------------------------------------------------------
class TestEventsAndHistory:
    def test_events_in_order_state_log_transcript(self, engine, fake_hotkeys,
                                                  monkeypatch):
        _mock_transcribe(monkeypatch, _result(), delay=0.01)
        _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()                        # vide les événements du boot

        manager.press()
        feed_audio(engine, Block([0.1]))
        manager.release()
        assert wait_status(engine, STATE_READY)

        events = engine.pop_events()
        types = [e["type"] for e in events]
        assert "transcript" in types
        # Premières occurrences : state → log → transcript.
        assert types.index("state") < types.index("log") < types.index("transcript")

        tr = next(e for e in events if e["type"] == "transcript")
        assert tr["data"]["text"] == "Bonjour le monde"
        assert tr["data"]["language"] == "fr"
        assert tr["data"]["duration"] == 1.24
        assert "ts" in tr["data"]
        engine.stop()

    def test_clear_history(self, engine, fake_hotkeys, monkeypatch):
        _mock_transcribe(monkeypatch, _result(), delay=0.01)
        _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)

        manager.press()
        feed_audio(engine, Block([0.1]))
        manager.release()
        assert wait_status(engine, STATE_READY)
        assert len(engine.get_history()) == 1

        engine.clear_history()
        assert engine.get_history() == []
        engine.stop()


# ---------------------------------------------------------------------------
# Événements audio (waveform temps réel) — monitoring permanent
# ---------------------------------------------------------------------------
class TestAudioEvents:
    def test_audio_event_emitted_when_not_recording(self, engine, fake_hotkeys):
        """Le monitoring pousse un événement « audio » même au repos (ready)."""
        _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()                        # vide les événements du boot

        feed_audio(engine, Block([0.5, 0.4]))
        events = engine.pop_events()
        audio_events = [e for e in events if e["type"] == "audio"]
        assert len(audio_events) == 1
        assert audio_events[0]["data"]["recording"] is False
        assert len(audio_events[0]["data"]["levels"]) == 64
        engine.stop()

    def test_audio_event_emitted_during_recording(self, engine, fake_hotkeys):
        """L'événement audio porte recording=True pendant l'enregistrement."""
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        manager.press()                            # STATE_RECORDING
        feed_audio(engine, Block([0.1, 0.2, 0.3]))
        events = engine.pop_events()
        audio_events = [e for e in events if e["type"] == "audio"]
        assert len(audio_events) == 1
        assert audio_events[0]["data"]["recording"] is True
        assert audio_events[0]["data"]["levels"][:3] == [0.1, 0.2, 0.3]

        manager.release()
        assert wait_status(engine, STATE_READY)
        engine.stop()

    def test_audio_event_throttled(self, engine, fake_hotkeys):
        """Deux blocs nourris quasi-instantanément ne produisent qu'un seul
        événement audio (throttle 50 ms)."""
        _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        feed_audio(engine, Block([0.1, 0.2]), Block([0.3, 0.4]))
        events = engine.pop_events()
        audio_events = [e for e in events if e["type"] == "audio"]
        assert len(audio_events) == 1
        engine.stop()

    def test_audio_event_does_not_pollute_history(self, engine, fake_hotkeys):
        """Les événements audio n'alimentent jamais l'historique."""
        _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        feed_audio(engine, Block([0.1, 0.2]))
        engine.pop_events()                        # consomme l'événement audio
        assert engine.get_history() == []
        engine.stop()


# ---------------------------------------------------------------------------
# Mode continu (WebSocket WhisperLive)
# ---------------------------------------------------------------------------
# Le frontend accumule lui-même les segments finaux (is_final=true) :
# handlePartialTranscript -> live.finalText. Le moteur émet donc chaque
# segment reçu du serveur en partial_transcript {text, is_final: True,
# recording: True}, et conserve sa propre copie cumulée (self._ws_text) pour
# l'injection finale à la relâche.
def _partial_events(engine):
    """Retourne les événements partial_transcript accumulés (et les consomme)."""
    return [e for e in engine.pop_events()
            if e["type"] == "partial_transcript"]


def wait_partial(engine, text, timeout=5.0):
    """Attend qu'un partial_transcript avec le texte donné soit émis."""
    def found():
        return any(
            p["data"]["text"] == text for p in _partial_events(engine))
    return wait_until(found, timeout)


def _transcript_event(segments):
    """Construit un événement serveur « transcript » (format WhisperLive)."""
    return {"uid": "test-uid", "message": "transcript", "segments": segments}


class TestContinuousWs:
    """Tests du mode continu « WebSocket WhisperLive » : pendant
    l'enregistrement, les chunks audio sont streamés (send_audio) et chaque
    segment renvoyé par le serveur est émis en partial_transcript
    (is_final=True, recording=True) ; à la relâche, EOF + drain des derniers
    segments + injection du texte final ; fallback batch complet si la
    session WS échoue.

    Le mode batch classique (continuous_mode=False) reste intact : F8 →
    transcription complète à la relâche.
    """

    def test_ws_full_cycle_segments_partial_release_inject(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Cycle complet : connexion (handshake) → envoi audio → segments
        reçus → partial_transcript → EOF → injection du texte cumulé."""
        fake_ws_client.script = [
            _transcript_event([{"start": 0.0, "end": 2.0,
                                "text": "Bonjour tout le monde"}]),
            _transcript_event([{"start": 2.0, "end": 3.0,
                                "text": "encore"}]),
        ]
        injected = _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()  # vide les événements du boot

        manager.press()
        assert engine.snapshot()["status"] == STATE_RECORDING
        assert wait_until(lambda: len(fake_ws_client.instances) >= 1)
        client = fake_ws_client.instances[-1]
        assert wait_until(
            lambda: engine._ws_client is not None and engine._ws_thread is not None)

        # URL WS dérivée de server_url + ws_port (config par défaut).
        assert client.host == "192.168.1.50"
        assert client.ws_port == 9090
        assert client.connect_url == \
            "ws://192.168.1.50:9090/"
        assert client.model == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        assert client.language == "fr"

        # 2 s d'audio → le thread sender les envoie (float32 -> int16 brut).
        feed_audio(engine, Block([0.1] * 32000))
        assert wait_until(lambda: len(client.sent_audio) >= 1)

        # Le 1er segment reçu est émis en partial_transcript (is_final=True).
        assert wait_partial(engine, "Bonjour tout le monde")
        # Le 2e segment est consommé par le receveur (texte cumulé moteur).
        assert wait_until(lambda: "encore" in (engine._ws_text or ""))

        manager.release()
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)

        assert client.eof_sent is True          # EOF envoyé à la relâche
        assert client.closed is True            # session fermée
        assert len(injected) == 1
        assert injected[0]["text"] == "Bonjour tout le monde encore"
        hist = engine.get_history()
        assert len(hist) == 1
        assert hist[0]["text"] == "Bonjour tout le monde encore"

        # Un partial final (recording=False) est émis avant l'injection.
        finals = [p for p in _partial_events(engine)
                  if p["data"]["is_final"] and not p["data"]["recording"]]
        assert any(f["data"]["text"] == "Bonjour tout le monde encore"
                   for f in finals)
        engine.stop()

    def test_ws_drains_final_segments_after_eof(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Les segments reçus APRÈS la relâche (drain post-EOF) sont bien
        accumulés et injectés (le serveur peut finaliser après l'EOF)."""
        # Fenêtre de drain large : le segment tardif poussé par le test est
        # forcément attrapé par le stop worker (évite la course au timeout).
        monkeypatch.setattr(dictation_module, "WS_EOF_QUIET", 0.5)
        fake_ws_client.script = [
            _transcript_event([{"start": 0.0, "end": 2.0, "text": "Bonjour"}]),
        ]
        injected = _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        manager.press()
        assert wait_until(lambda: len(fake_ws_client.instances) >= 1)
        client = fake_ws_client.instances[-1]
        assert wait_until(lambda: engine._ws_thread is not None)
        feed_audio(engine, Block([0.1] * 32000))
        assert wait_partial(engine, "Bonjour")

        manager.release()
        # Le receveur consomme « Bonjour » ; le stop worker draine « le monde »
        # (poussé pendant le drain) → texte final « Bonjour le monde ».
        assert wait_status(engine, STATE_TRANSCRIBING)
        # Pendant le drain du stop worker, un dernier segment arrive.
        fake_ws_client.instances[-1].script.append(
            _transcript_event([{"start": 2.0, "end": 3.0, "text": "le monde"}]))
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)

        assert len(injected) == 1
        assert injected[0]["text"] == "Bonjour le monde"
        engine.stop()

    def test_ws_fallback_to_batch_on_connect_error(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Échec de connexion WS → self._ws_failed → fallback batch complet
        à la relâche (la dictée n'est jamais perdue)."""
        fake_ws_client.script = []
        _mock_transcribe(monkeypatch, _result(), delay=0.05)
        injected = _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        fake_ws_client.connect_result = False   # connexion refusée dès l'instance
        manager.press()
        assert wait_until(lambda: len(fake_ws_client.instances) >= 1)
        client = fake_ws_client.instances[-1]
        assert client.connect() is False        # l'instance copie connect_result
        assert wait_until(lambda: engine._ws_failed is True)

        feed_audio(engine, Block([0.1, 0.2]))
        manager.release()
        # Fallback batch → transcribe(audio complet) → succès + injection.
        assert wait_status(engine, STATE_TRANSCRIBING)
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)
        assert len(injected) == 1
        assert injected[0]["text"] == "Bonjour le monde"
        engine.stop()

    def test_ws_fallback_to_batch_on_server_error(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Un message « error » du serveur pendant l'enregistrement marque
        l'échec WS → fallback batch complet à la relâche."""
        fake_ws_client.script = [
            {"uid": "test-uid", "message": "error", "reason": "boom"},
        ]
        _mock_transcribe(monkeypatch, _result(), delay=0.05)
        injected = _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        manager.press()
        feed_audio(engine, Block([0.1] * 32000))
        assert wait_until(lambda: engine._ws_failed is True)

        manager.release()
        assert wait_status(engine, STATE_TRANSCRIBING)
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)
        assert len(injected) == 1
        assert injected[0]["text"] == "Bonjour le monde"
        engine.stop()

    def test_ws_short_recording_uses_batch(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Aucun segment reçu pendant l'enregistrement → repli sur le flux
        batch existant à la relâche (comportement classique)."""
        fake_ws_client.script = []
        _mock_transcribe(monkeypatch, _result(), delay=0.05)
        injected = _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        manager.press()
        assert wait_until(lambda: engine._ws_thread is not None)
        feed_audio(engine, Block([0.1] * 4800))
        manager.release()
        assert wait_status(engine, STATE_TRANSCRIBING)  # batch fallback
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)

        assert len(injected) == 1
        assert injected[0]["text"] == "Bonjour le monde"
        engine.stop()

    def test_ws_mode_disabled_uses_batch(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """continuous_mode=False → batch complet à la relâche (pas de WS)."""
        engine.config["continuous_mode"] = False
        _mock_transcribe(monkeypatch, _result(), delay=0.05)
        injected = _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        manager.press()
        assert engine._ws_connect_thread is None  # pas de mode continu
        assert engine._ws_send_queue is None

        feed_audio(engine, Block([0.1, 0.2]))

        manager.release()
        assert wait_status(engine, STATE_TRANSCRIBING)
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)

        assert len(injected) == 1
        assert injected[0]["text"] == "Bonjour le monde"
        engine.stop()

    def test_ws_chunks_pushed_to_sender(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Les blocs audio capturés pendant l'enregistrement arrivent au
        thread sender qui les envoie sur la session WS (send_audio)."""
        fake_ws_client.script = []
        _mock_transcribe(monkeypatch, None)   # repli batch silencieux
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        manager.press()
        assert wait_until(lambda: len(fake_ws_client.instances) >= 1)
        client = fake_ws_client.instances[-1]
        assert wait_until(lambda: engine._ws_thread is not None)
        assert engine._ws_send_queue is not None

        feed_audio(
            engine,
            Block([0.1, 0.2]),
            Block([0.3, 0.4]),
            Block([0.5, 0.6]),
        )
        # Le thread sender convertit et envoie chaque chunk (PCM16 brut).
        assert wait_until(lambda: len(client.sent_audio) == 3)
        assert list(client.sent_audio[0].flatten()) == [0.1, 0.2]

        manager.release()
        assert wait_status(engine, STATE_READY)
        engine.stop()

    def test_ws_no_chunks_when_queue_is_none(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Quand self._ws_send_queue est None (mode batch), _on_audio_chunk ne
        fait rien — pas d'exception."""
        engine.config["continuous_mode"] = False
        _mock_transcribe(monkeypatch, _result(), delay=0.05)
        _mock_inject(monkeypatch)
        manager = _start_and_wait_ready(engine, fake_hotkeys)
        engine.pop_events()

        manager.press()
        assert engine._ws_send_queue is None

        feed_audio(engine, Block([0.1, 0.2]), Block([0.3, 0.4]))

        manager.release()
        assert wait_status(engine, STATE_SUCCESS)
        assert wait_status(engine, STATE_READY)
        engine.stop()

    def test_ws_stop_cleans_up_ws_state(
            self, engine, fake_hotkeys, fake_ws_client, monkeypatch):
        """Après stop du moteur, les ressources du mode continu WS sont
        libérées et la session fermée."""
        fake_ws_client.script = []
        manager = _start_and_wait_ready(engine, fake_hotkeys)

        manager.press()
        assert wait_until(lambda: len(fake_ws_client.instances) >= 1)
        client = fake_ws_client.instances[-1]
        assert wait_until(lambda: engine._ws_thread is not None)

        engine.stop()
        assert engine._ws_thread is None
        assert engine._ws_recv_thread is None
        assert engine._ws_connect_thread is None
        assert engine._ws_send_queue is None
        assert engine._ws_stop_event is None
        assert engine._ws_client is None
        assert engine._ws_text == ""
        assert engine._ws_failed is False
        assert client.closed is True          # session fermée au stop

    def test_ws_mode_toggle_via_apply_config(
            self, engine, fake_hotkeys, fake_ws_client):
        """continuous_mode est un HOT_FIELD : changement à chaud sans reload."""
        _start_and_wait_ready(engine, fake_hotkeys)

        reload_needed, live_changed = engine.apply_config(
            {**DEFAULT_CONFIG, "continuous_mode": False})

        assert reload_needed is False
        assert "continuous_mode" in live_changed
        assert engine.config["continuous_mode"] is False
        engine.stop()
