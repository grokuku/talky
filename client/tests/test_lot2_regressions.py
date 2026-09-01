# -*- coding: utf-8 -*-
"""
tests/test_lot2_regressions.py
==============================
Lot 2 — Régressions review fact-checkée :

* M2  — cohérence d'état config : POST /api/config avec hotkey invalide
        refuse (400), ne persiste RIEN (config.json + engine.config) et
        laisse les hotkeys précédentes installées ; validate_config valide
        la hotkey via parse_hotkey AVANT toute écriture ; apply_config est
        transactionnel (nouvelle hotkey installée avant mutation, rollback).
* m10 — POST /api/config filtre les clés inconnues (jamais persistées).
* M7  — encode_wav / float32_to_int16 vectorisés numpy : sortie
        byte-à-byte identique à l'ancienne implémentation (boucle
        struct.pack), réimplémentée ici en référence.
* m2  — files d'événements séparées : un burst audio (20 fps) n'évince
        plus un log ERROR ; l'audio reste borné à ~1 s (20 événements).

Exécution : ``cd client && python -m pytest tests/test_lot2_regressions.py -q``
"""

import io
import math
import random
import struct
import threading
import time
import wave

import pytest

from app.core.config import (
    DEFAULT_CONFIG,
    load_config,
    validate_config,
)
from app.core.constants import SAMPLING_RATE, STATE_READY
from app.engine import dictation as dictation_module
from app.engine import transcriber_client
from app.engine.dictation import DictationEngine
from app.engine.state import AUDIO_EVENTS_MAXLEN, EVENTS_MAXLEN, EngineState
from app.engine.transcriber_client import encode_wav
from app.engine.whisperlive_client import float32_to_int16


# ---------------------------------------------------------------------------
# Helpers (réplique compacte des fixtures de test_engine.py)
# ---------------------------------------------------------------------------
def wait_status(engine, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if engine.snapshot()["status"] == status:
            return True
        time.sleep(0.01)
    return False


class FakeHotkeyManager:
    """HotkeyManager factice : installe/désinstalle sans evdev."""

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


@pytest.fixture()
def fake_hotkeys(monkeypatch):
    monkeypatch.setattr(dictation_module, "HotkeyManager", FakeHotkeyManager)
    FakeHotkeyManager.instances = []
    yield FakeHotkeyManager
    FakeHotkeyManager.instances = []


@pytest.fixture()
def engine(fake_hotkeys):
    return DictationEngine(dict(DEFAULT_CONFIG))


@pytest.fixture(autouse=True)
def _protect_config():
    """Ne jamais toucher au vrai config.json"""
    from app.core.config import CONFIG_PATH

    backup = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None
    yield
    if backup is None:
        CONFIG_PATH.unlink(missing_ok=True)
    else:
        CONFIG_PATH.write_text(backup, encoding="utf-8")


# ===========================================================================
# M2 — validation hotkey AVANT toute persistance
# ===========================================================================
class TestValidateHotkey:
    """validate_config doit refuser les hotkeys malformées (parse_hotkey)."""

    @pytest.mark.parametrize("bad", [
        "f25",            # touche inconnue
        "ctrl+f25",       # touche cible inconnue avec modificateur
        "ctrl",           # modificateur seul
        "ctrl+shift",     # modificateur seul (cible)
        "ctrl+badkey",    # inconnue
        "not+then+f8",    # modificateur inconnu
        "++",             # vide
        3.14,             # type absurde
    ])
    def test_invalid_hotkey_rejected_before_write(self, bad, tmp_path):
        with pytest.raises(ValueError) as exc_info:
            validate_config({**DEFAULT_CONFIG, "hotkey": bad})
        # erreur sérialisable JSON (contrat de la route : {"errors": ...})
        import json
        errors = json.loads(exc_info.value.args[0])
        assert "hotkey" in errors

    @pytest.mark.parametrize("good", ["f8", "ctrl+space", "ctrl+alt+f9",
                                      "CTRL+Space", "a"])
    def test_valid_hotkeys_accepted(self, good):
        cfg = validate_config({**DEFAULT_CONFIG, "hotkey": good})
        assert cfg["hotkey"] == good

    def test_empty_hotkey_still_rejected(self):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "hotkey": ""})

    def test_hotkeys_previous_stay_active_on_invalid_apply(self, engine):
        """apply_config transactionnel : si l'installation de la nouvelle
        hotkey échoue (ValueError parse_hotkey / HotkeyError), la config du
        moteur ET l'installation courante restent inchangées."""
        config = dict(DEFAULT_CONFIG)
        engine.start()
        assert wait_status(engine, STATE_READY)
        assert engine.config["hotkey"] == "f8"
        assert len(FakeHotkeyManager.instances) == 1
        old_manager = FakeHotkeyManager.instances[0]
        old_manager.install()  # simule l'installation réussie au boot
        engine._hotkeys = old_manager

        class FailingManager(FakeHotkeyManager):
            def install(self):
                raise ValueError(
                    "Touche inconnue : « f25 » (clés supportées : lettres, "
                    "chiffres, f1-f12, space, enter, tab, esc, ...).")

        dictation_module.HotkeyManager = FailingManager
        with pytest.raises(ValueError):
            engine.apply_config({**DEFAULT_CONFIG, "hotkey": "f25"})

        # Config du moteur inchangée + ancienne hotkey toujours active.
        assert engine.config["hotkey"] == "f8"
        assert engine._hotkeys is old_manager
        assert old_manager.installed and not old_manager.uninstalled
        # stop() réinstalle l'état initial pour les tests suivants.
        engine.stop()


# ===========================================================================
# M2 + m10 — POST /api/config : refus sans persistance, clés filtrées
# ===========================================================================
@pytest.fixture()
def api_client(client, monkeypatch, fake_hotkeys):
    """TestClient + moteur (singleton) démarré avec hotkeys factices."""
    from app.api import dependencies

    monkeypatch.setattr(
        transcriber_client, "ping",
        lambda *a, **kw: {"reachable": True, "status": 200})
    dependencies.engine.start()
    assert wait_status(dependencies.engine, STATE_READY)
    yield client, dependencies.engine
    dependencies.engine.stop()


class TestPostConfigInvalidHotkey:
    """POST /api/config avec hotkey invalide : 400, rien de persisté,
    hotkeys précédentes toujours actives (acceptance du lot 2)."""

    def test_invalid_hotkey_400_no_persist_hotkeys_still_active(
            self, api_client, monkeypatch):
        client, engine = api_client

        assert len(FakeHotkeyManager.instances) == 1
        old_manager = FakeHotkeyManager.instances[0]
        assert old_manager.hotkey == "f8"
        assert engine._hotkeys is old_manager

        res = client.post("/api/config", json={"hotkey": "ctrl+f25"})
        assert res.status_code == 400
        body = res.json()
        assert body["saved"] is False
        assert "hotkey" in body["errors"]

        # (1) rien de persisté sur disque
        assert load_config()["hotkey"] == "f8"
        # (2) rien de muté dans la config du moteur
        assert engine.config["hotkey"] == "f8"
        # (3) hotkeys précédentes toujours actives : même manager, installé,
        #     jamais désinstallé, aucun nouveau manager instancié
        assert engine._hotkeys is old_manager
        assert old_manager.installed is True
        assert old_manager.uninstalled is False
        assert len(FakeHotkeyManager.instances) == 1


class TestPostConfigUnknownKeys:
    def test_unknown_keys_not_persisted(self, api_client):
        client, engine = api_client

        res = client.post("/api/config", json={
            "model": "Systran/faster-whisper-small",
            "cle_inconnue": 42,
            "autre": {"nested": True},
            "__proto__": "pwn",
        })
        assert res.status_code == 200
        body = res.json()
        assert body["saved"] is True
        # Le payload filtré disparaît de la réponse ET du disque.
        assert "cle_inconnue" not in body["config"]
        assert "autre" not in body["config"]
        assert "__proto__" not in body["config"]
        assert body["config"]["model"] == "Systran/faster-whisper-small"
        persisted = load_config()
        assert persisted["model"] == "Systran/faster-whisper-small"
        assert set(persisted) == set(DEFAULT_CONFIG)
        # L'état du moteur est appliqué (champ à chaud connu).
        assert engine.config["model"] == "Systran/faster-whisper-small"


# ===========================================================================
# M7 — encode_wav / float32_to_int16 vectorisés, octets identiques
# ===========================================================================
def _reference_encode_wav(samples) -> bytes:
    """ANCIENNE implémentation de encode_wav (boucle struct.pack par
    échantillon, accès audio.flatten()) — référence de non-régression
    binaire. Compatible numpy réel ET mock conftest (_Flat)."""
    flat = samples.flatten() if hasattr(samples, "flatten") else samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)                       # PCM16 = 2 octets
        wav.setframerate(SAMPLING_RATE)
        frames = bytearray()
        for sample in flat:
            value = max(-1.0, min(1.0, float(sample)))
            frames += struct.pack("<h", int(round(value * 32767)))
        wav.writeframes(bytes(frames))
    return buf.getvalue()


def _random_values(n=4096, seed=1234):
    """Valeurs aléatoires réalistes + cas limites (clip, arrondis .5, NaN).

    Retourne EXACTEMENT ``n`` valeurs (floats Python, compatibles numpy réel
    ET mock conftest) dans [-1.6, 1.6], avec les bornes exactes [-1.0, 1.0]
    garanties dans le lot.
    """
    rng = random.Random(seed)
    values = [rng.uniform(-1.6, 1.6) for _ in range(n - 15)]
    # Bornes et cas remarquables :
    values += [0.0, 1.0, -1.0, 2.0, -2.0, 1e-9, -1e-9,
               (0 + 0.5) / 32767, (1 + 0.5) / 32767, (16383 + 0.5) / 32767,
               (32766 + 0.5) / 32767, -(16383 + 0.5) / 32767,
               math.inf, -math.inf, math.nan]
    return values[:n]


class TestPcm16ByteIdentity:
    """(M7) La conversion vectorisée numpy doit produire EXACTEMENT les
    mêmes octets que l'ancienne boucle struct.pack (PCM16 LE)."""

    def test_encode_wav_random_sample_byte_identity(self):
        values = _random_values()
        audio = transcriber_client.np.array(values, dtype=np_float32())
        # Référence = ancienne implémentation appliquée AUX MÊMES VALEURS
        # STOCKÉES dans le tableau (float32) : l'ancienne boucle lisait
        # array.flatten(), pas les floats Python d'origine.
        reference = _reference_encode_wav(audio)
        assert encode_wav(audio) == reference           # WAV complet
        # L'en-tête fait 44 octets, puis frames PCM16 LE.
        assert float32_to_int16(audio) == reference[44:]

    def test_2d_and_non_contiguous_inputs(self):
        if not transcriber_client._VECTOR_PCM16:
            pytest.skip("numpy réel requis (vue 2D / pas à pas sous mock absent)")
        values = _random_values(n=512, seed=99)
        np = transcriber_client.np
        flat = np.array(values, dtype=np_float32())
        two_d = flat.reshape(len(values) // 8, 8)       # shape (n, 8)
        strided = flat[::3]                             # vue non contiguë
        reference = _reference_encode_wav(flat)
        assert encode_wav(two_d) == reference           # flatten() == reshape(-1)
        expected_strided = _reference_encode_wav(strided)
        assert encode_wav(strided) == expected_strided  # ordre identique

    def test_clipping_boundaries(self):
        np = transcriber_client.np
        values = [1.0, -1.0, 0.5001, -0.5001, 32767 / 32767, -32767 / 32767,
                  (16383 + 0.5) / 32767, (32766 + 0.5) / 32767]
        audio = np.array(values, dtype=np_float32())
        reference = _reference_encode_wav(audio)
        assert encode_wav(audio) == reference
        assert float32_to_int16(audio) == reference[44:]

    def test_performance_reasonable(self):
        """Sanity perf : 60 s d'audio (960 000 échantillons) encodé en
        Vectorisé < 0,5 s (l'ancienne boucle prenait plusieurs secondes).
        Sous numpy mocké (boucle Python), seuil relaxé."""
        values = [random.Random(7).uniform(-1.0, 1.0) for _ in range(960_000)]
        np = transcriber_client.np
        audio = np.array(values, dtype=np_float32())
        start = time.monotonic()
        encode_wav(audio)
        elapsed = time.monotonic() - start
        limit = 5.0 if not transcriber_client._VECTOR_PCM16 else 0.5
        assert elapsed < limit


def np_float32():
    """dtype float32 : réel numpy float32 en prod, float sous mock conftest."""
    np = transcriber_client.np
    return np.float32


# ===========================================================================
# m2 — files d'événements séparées (state.py)
# ===========================================================================
class TestSeparatedEventQueues:
    """L'audio à 20 fps ne doit plus évincer les événements critiques."""

    def _state(self) -> EngineState:
        return EngineState(dict(DEFAULT_CONFIG), threading.RLock())

    def test_error_log_survives_audio_burst(self):
        state = self._state()
        # 250 événements audio (12,5 s à 20 fps) — déborderait le maxlen 200.
        for i in range(250):
            state.emit("audio", {"levels": [i / 250.0] * 64,
                                 "recording": True})
        state.log("ERROR", "Erreur critique — ne doit pas être évincée")
        state.log("INFO", "log info après erreur")

        events = state.pop_events()
        types = [e["type"] for e in events]
        assert "log" in types
        logs = [e for e in events if e["type"] == "log"]
        assert logs[0]["data"]["level"] == "ERROR"
        assert logs[0]["data"]["message"] == (
            "Erreur critique — ne doit pas être évincée")
        # L'audio a été borné (~20 événements pour 250 émis) et l'erreur est
        # bien servie dans le même lot que le burst.
        assert len([e for e in events if e["type"] == "audio"]) == AUDIO_EVENTS_MAXLEN

    def test_audio_burst_bounded_to_one_second(self):
        state = self._state()
        for i in range(64):  # 64 événements audio d'affilée (3,2 s à 20 fps)
            state.emit("audio", {"levels": [0.0] * 64, "recording": True})
        events = state.pop_events()
        audio = [e for e in events if e["type"] == "audio"]
        # Bornage ~1 s : seuls les 20 derniers événements restent en file.
        assert len(audio) == AUDIO_EVENTS_MAXLEN == 20
        assert EVENTS_MAXLEN == 200

        # Le burst a bien laissé le dernier événement en fin de file (les
        # plus anciens sont évincés en premier, style deque standard).
        assert audio[-1]["data"]["levels"][0] == 0.0

    def test_critical_backlog_capped_at_200(self):
        state = self._state()
        for i in range(250):
            state.emit("log", {"level": "INFO", "message": f"n{i}"})
        events = state.pop_events()
        logs = [e for e in events if e["type"] == "log"]
        assert len(logs) == EVENTS_MAXLEN     # ancienne borne conservée (200)
        assert logs[0]["data"]["message"] == "n50"   # éviction FIFO intacte
        assert logs[-1]["data"]["message"] == "n249"

    def test_pop_returns_priority_events_then_audio(self):
        state = self._state()
        state.log("INFO", "log 1")
        state.emit("audio", {"levels": [0.1] * 64, "recording": False})
        state.log("INFO", "log 2")
        state.emit("audio", {"levels": [0.2] * 64, "recording": True})

        events = state.pop_events()
        # Les événements critiques d'abord, l'audio ensuite (ordre non
        # critique entre les deux groupes, mais log 1/log 2 restent dans
        # leur ordre d'émission et l'audio dans le sien).
        audio = [e for e in events if e["type"] == "audio"]
        others = [e for e in events if e["type"] != "audio"]
        assert others[0]["data"]["message"] == "log 1"
        assert others[1]["data"]["message"] == "log 2"
        assert [e["data"]["levels"][0] for e in audio] == [0.1, 0.2]

        # pop_events vide tout (pas de rejeu).
        assert state.pop_events() == []

    def test_transcript_not_evicted_by_audio(self):
        state = self._state()
        for i in range(300):
            state.emit("audio", {"levels": [0.0] * 64, "recording": True})
        state.emit("transcript", {"text": "Salut", "language": "fr"})
        events = state.pop_events()
        transcripts = [e for e in events if e["type"] == "transcript"]
        assert len(transcripts) == 1
        assert transcripts[0]["data"]["text"] == "Salut"