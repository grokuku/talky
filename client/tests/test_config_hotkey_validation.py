# -*- coding: utf-8 -*-
"""
tests/test_config_hotkey_validation.py
======================================
Lot 2a — test dédié des correctifs M2 et m10 :

* M2 — validation des hotkeys AVANT toute écriture :
    - ``validate_config`` refuse une hotkey malformée via ``parse_hotkey``
      (app/core/config.py) → POST /api/config répond 400 et ne modifie NI
      config.json NI le moteur ;
    - ``DictationEngine.apply_config`` est transactionnel
      (app/engine/dictation.py) : la NOUVELLE hotkey est installée d'abord,
      ``config.update`` + swap n'ont lieu qu'après succès ; en cas d'échec
      la config ET l'installation courante restent inchangées (rollback).

* m10 — POST /api/config filtre le payload sur ``DEFAULT_CONFIG.keys()`` :
    toute clé inconnue est ignorée (jamais appliquée, jamais persistée).

ACCEPTANCE (lot 2a) : POST /api/config avec hotkey invalide → 400,
config NON persistée, hotkeys précédentes toujours actives.

Utilise les mocks de conftest.py (sounddevice/evdev/pyperclip/httpx
simulés si absents, protection automatique de config.json). Aucun appel
réseau : le « ping » du transcriber est neutralisé.

Exécution : ``cd client && python -m pytest tests/test_config_hotkey_validation.py -q``
"""

import json
import time

import pytest

from app.core.config import (
    CONFIG_PATH,
    DEFAULT_CONFIG,
    load_config,
    validate_config,
)
from app.core.constants import STATE_READY
from app.engine import dictation as dictation_module
from app.engine import transcriber_client
from app.engine.dictation import DictationEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def wait_status(engine, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if engine.snapshot()["status"] == status:
            return True
        time.sleep(0.01)
    return False


class FakeHotkeyManager:
    """HotkeyManager factice : installe/désinstalle sans evdev, trace son état."""

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
    """Moteur isolé (ni l'instance singleton de l'API ni config.json)."""
    return DictationEngine(dict(DEFAULT_CONFIG))


# ===========================================================================
# M2 (1/2) — validate_config refuse les hotkeys malformées AVANT toute écriture
# ===========================================================================
class TestValidateHotkeyBeforeWrite:
    """La hotkey est validée par parse_hotkey dès validate_config (mêmes
    règles que HotkeyManager.install) : un appel ultérieur d'install() ne
    peut plus lever ValueError « touche inconnue » sur une config passée
    par la route."""

    @pytest.mark.parametrize("bad", [
        "f25",            # touche hors table (f1-f12)
        "ctrl+f25",       # modificateur + touche inconnue
        "ctrl+ctrl",      # modificateur seul comme cible
        "not+then+f8",    # modificateur inconnu
        "++",             # combinaison vide
        "ctrl+badkey+",   # éléments vides -> cible inconnue
        3.14,             # type absurde (cast str, touche inconnue)
    ])
    def test_invalid_hotkey_rejected(self, bad):
        with pytest.raises(ValueError) as exc_info:
            validate_config({**DEFAULT_CONFIG, "hotkey": bad})
        # Contrat de la route : ValueError.args[0] = JSON {"champ": message}.
        errors = json.loads(exc_info.value.args[0])
        assert "hotkey" in errors

    @pytest.mark.parametrize("good", [
        "f8", "ctrl+space", "ctrl+alt+f9", "CTRL+Space", "a", "9",
    ])
    def test_valid_hotkeys_accepted(self, good):
        cfg = validate_config({**DEFAULT_CONFIG, "hotkey": good})
        assert cfg["hotkey"] == good

    def test_empty_hotkey_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            validate_config({**DEFAULT_CONFIG, "hotkey": ""})
        assert "hotkey" in json.loads(exc_info.value.args[0])


# ===========================================================================
# M2 (2/2) — apply_config transactionnel (install d'abord, swap ensuite)
# ===========================================================================
class TestApplyConfigAtomic:
    """Si l'installation de la nouvelle hotkey échoue, ``self.config`` et
    l'installation courante restent inchangées (rollback / no-swap)."""

    def test_failed_install_leaves_engine_untouched(self, engine):
        config = dict(DEFAULT_CONFIG)
        engine.start()
        assert wait_status(engine, STATE_READY)
        old_manager = FakeHotkeyManager.instances[0]
        old_manager.install()
        engine._hotkeys = old_manager

        class FailingManager(FakeHotkeyManager):
            def install(self):
                raise ValueError(
                    "Touche inconnue : « f25 » (clés supportées : lettres, "
                    "chiffres, f1-f12, space, enter, tab, esc, ...).")

        dictation_module.HotkeyManager = FailingManager
        with pytest.raises(ValueError):
            engine.apply_config({**DEFAULT_CONFIG, "hotkey": "f25"})

        # Config moteur inchangée + anciennes hotkeys toujours actives :
        # même manager, jamais désinstallé, aucun nouveau manager gardé.
        assert engine.config["hotkey"] == "f8"
        assert engine._hotkeys is old_manager
        assert old_manager.installed is True
        assert old_manager.uninstalled is False
        engine.stop()

    def test_hotkey_error_also_rolls_back(self, engine):
        """Même scénario qu'install() en échec, mais avec HotkeyError
        (aucun /dev/input lisible) : mêmes garanties transactionnelles."""
        from app.engine.hotkeys import HotkeyError

        engine.start()
        assert wait_status(engine, STATE_READY)
        old_manager = FakeHotkeyManager.instances[0]
        engine._hotkeys = old_manager

        class FailingManager(FakeHotkeyManager):
            def install(self):
                raise HotkeyError("Aucun périphérique d'input trouvé.")

        dictation_module.HotkeyManager = FailingManager
        with pytest.raises(HotkeyError):
            engine.apply_config({**DEFAULT_CONFIG, "hotkey": "f9"})

        assert engine.config["hotkey"] == "f8"
        assert engine._hotkeys is old_manager
        assert old_manager.uninstalled is False
        engine.stop()

    def test_successful_apply_swaps_manager_and_uninstalls_old(self, engine):
        config = dict(DEFAULT_CONFIG)
        engine.start()
        assert wait_status(engine, STATE_READY)
        old_manager = FakeHotkeyManager.instances[0]
        engine._hotkeys = old_manager

        reload_needed, live_changed = engine.apply_config(
            {**DEFAULT_CONFIG, "hotkey": "ctrl+space"})

        assert reload_needed is False
        assert "hotkey" in live_changed
        assert engine.config["hotkey"] == "ctrl+space"
        new_manager = engine._hotkeys
        assert new_manager is not old_manager
        assert new_manager.hotkey == "ctrl+space"
        assert new_manager.installed is True
        assert old_manager.uninstalled is True  # retrait de l'ancien
        engine.stop()


# ===========================================================================
# ACCEPTANCE lot 2a — POST /api/config, hotkey invalide : 400, rien persisté,
# hotkeys précédentes toujours actives (+ m10 : clés inconnues filtrées)
# ===========================================================================
@pytest.fixture()
def api_client(client, monkeypatch, fake_hotkeys):
    """TestClient + moteur singleton démarré avec hotkeys factices.

    - transcriber_client.ping neutralisé (jamais de serveur distant) ;
    - config.json protégé par la fixture autouse _protect_config (conftest).
    """
    from app.api import dependencies

    monkeypatch.setattr(
        transcriber_client, "ping",
        lambda *a, **kw: {"reachable": True, "status": 200})
    dependencies.engine.start()
    assert wait_status(dependencies.engine, STATE_READY)
    yield client, dependencies.engine
    dependencies.engine.stop()


class TestPostConfigInvalidHotkey:
    """ACCEPTANCE : hotkey invalide → 400, config NON persistée (disque +
    moteur), hotkeys précédentes toujours actives."""

    def test_invalid_hotkey_400_no_persist_hotkeys_still_active(
            self, api_client):
        client, engine = api_client

        # État initial : un seul manager, hotkey par défaut f8, installé.
        assert len(FakeHotkeyManager.instances) == 1
        old_manager = FakeHotkeyManager.instances[0]
        assert old_manager.hotkey == "f8"
        assert engine._hotkeys is old_manager
        assert old_manager.installed is True
        assert old_manager.uninstalled is False

        res = client.post("/api/config", json={"hotkey": "ctrl+f25"})
        assert res.status_code == 400
        body = res.json()
        assert body["saved"] is False
        assert "hotkey" in body["errors"]

        # (1) Rien de persisté sur disque (config.json : f8, pas de f25).
        persisted = load_config()
        assert persisted["hotkey"] == "f8"
        if CONFIG_PATH.exists():
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            assert on_disk.get("hotkey") != "ctrl+f25"

        # (2) Rien de muté dans la config du moteur.
        assert engine.config["hotkey"] == "f8"

        # (3) Hotkeys précédentes toujours actives : même manager,
        #     toujours installé, jamais désinstallé, aucun manager
        #     supplémentaire créé (l'ancien n'a PAS été remplacé par
        #     un manager qui échouerait à s'installer, ni laissé à None).
        assert engine._hotkeys is old_manager
        assert old_manager.installed is True
        assert old_manager.uninstalled is False
        assert len(FakeHotkeyManager.instances) == 1


class TestPostConfigUnknownKeysFiltered:
    """m10 : le payload POST est filtré sur DEFAULT_CONFIG.keys()."""

    def test_unknown_keys_not_applied_nor_persisted(self, api_client):
        client, engine = api_client

        res = client.post("/api/config", json={
            "model": "Systran/faster-whisper-small",
            "unknown_key": 42,
            "nested": {"payload": True},
            "__proto__": "pwn",
            "hotkey": "f8",
        })
        assert res.status_code == 200
        body = res.json()
        assert body["saved"] is True

        # Clés inconnues absentes de la réponse ET du disque.
        assert "unknown_key" not in body["config"]
        assert "nested" not in body["config"]
        assert "__proto__" not in body["config"]
        persisted = load_config()
        assert persisted["model"] == "Systran/faster-whisper-small"
        assert set(persisted) == set(DEFAULT_CONFIG)

        # Champs connus bien appliqués au moteur.
        assert engine.config["model"] == "Systran/faster-whisper-small"

    def test_invalid_hotkey_rejected_even_mixed_with_unknown_keys(
            self, api_client):
        """Une hotkey invalide au milieu de clés inconnues est quand même
        rejetée (la validation précède le filtrage / merge / persistance)."""
        client, engine = api_client

        res = client.post("/api/config", json={
            "unknown_key": 1, "hotkey": "f88"})
        assert res.status_code == 400
        assert res.json()["saved"] is False
        assert load_config()["hotkey"] == "f8"
        assert engine.config["hotkey"] == "f8"
        assert engine._hotkeys is FakeHotkeyManager.instances[0]