# -*- coding: utf-8 -*-
"""
tests/test_hotkeys.py
=====================
HotkeyManager evdev (P3, §5.2 / §6.1) : mapping hotkey string → code evdev,
détection de combinaisons sur des événements simulés (mock evdev de
conftest.py), modes push_to_talk / toggle, repeat ignoré, déduplication
multi-claviers et désinstallation propre.

Aucun accès au matériel : evdev.InputDevice / evdev.list_devices sont
mockés, les événements sont injectés via read_loop() de devices factices.
"""

import time
import types

import pytest

from app.engine.hotkeys import (
    HotkeyError,
    HotkeyManager,
    ecodes,
    evdev,
    key_code,
    parse_hotkey,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
KEY_A = getattr(ecodes, "KEY_A", 30)
KEY_0 = getattr(ecodes, "KEY_0", 11)
KEY_F9 = getattr(ecodes, "KEY_F9", 67)


class MockCallback:
    """Compte les appels d'un callback (on_record_start/on_record_stop)."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


class FakeDevice:
    """Device evdev simulé : read_loop() rejoue une liste d'événements puis
    se termine (le thread de lecture sort naturellement)."""

    def __init__(self, path, events, capabilities=None):
        self.path = path
        self.events = list(events)
        self.closed = False
        self.caps = capabilities if capabilities is not None else {ecodes.EV_KEY: []}

    def capabilities(self, verbose=False):
        return self.caps

    def read_loop(self):
        yield from self.events

    def close(self):
        self.closed = True


class BlockingDevice(FakeDevice):
    """Device dont la lecture ne se termine jamais (thread vivant) — utilisé
    pour vérifier que uninstall() stoppe bien les threads."""

    def read_loop(self):
        while True:
            time.sleep(0.01)
            yield ev(type_=0, code=0, value=0)  # EV_SYN : filtré par le handler


def ev(type_=None, code=0, value=0):
    """Construit un événement evdev brut (type/code/value)."""
    if type_ is None:
        type_ = ecodes.EV_KEY
    return types.SimpleNamespace(type=type_, code=code, value=value)


def install_devices(monkeypatch, devices):
    """Branche evdev.list_devices + evdev.InputDevice sur des devices factices."""
    by_path = {d.path: d for d in devices}
    monkeypatch.setattr(evdev, "list_devices", lambda: list(by_path))
    monkeypatch.setattr(evdev, "InputDevice", lambda path: by_path[path])


def run(manager, devices):
    """Installe le manager, attend la fin des threads (read_loop terminé),
    puis désinstalle. Retourne le manager."""
    manager.install()
    for thread in manager._threads:
        thread.join(timeout=2.0)
    manager.uninstall()
    return manager


def push_to_talk_manager(hotkey="f8"):
    start = MockCallback()
    stop = MockCallback()
    manager = HotkeyManager(hotkey, "push_to_talk", start, stop)
    return manager, start, stop


# ---------------------------------------------------------------------------
# Mapping hotkey string → code evdev
# ---------------------------------------------------------------------------
class TestMapping:
    def test_f8_maps_to_key_f8(self):
        code, mods = parse_hotkey("f8")
        assert code == ecodes.KEY_F8
        assert mods == frozenset()

    def test_ctrl_space_maps_to_combination(self):
        code, mods = parse_hotkey("ctrl+space")
        assert code == ecodes.KEY_SPACE
        assert mods == frozenset({"ctrl"})

    def test_ctrl_alt_f9_maps_to_combination(self):
        code, mods = parse_hotkey("ctrl+alt+f9")
        assert code == ecodes.KEY_F9
        assert mods == frozenset({"ctrl", "alt"})

    def test_letter_maps_to_key(self):
        assert key_code("a") == KEY_A
        code, mods = parse_hotkey("a")
        assert code == KEY_A
        assert mods == frozenset()

    def test_space_maps_to_key(self):
        assert key_code("space") == ecodes.KEY_SPACE
        code, mods = parse_hotkey("space")
        assert code == ecodes.KEY_SPACE
        assert mods == frozenset()

    def test_digit_maps_to_key(self):
        assert key_code("0") == KEY_0
        code, mods = parse_hotkey("0")
        assert code == KEY_0
        assert mods == frozenset()

    def test_hotkey_case_and_whitespace_insensitive(self):
        code, mods = parse_hotkey("  CTRL + SPACE ")
        assert code == ecodes.KEY_SPACE
        assert mods == frozenset({"ctrl"})

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError):
            key_code("nonexistent")

    def test_unknown_modifier_raises(self):
        with pytest.raises(ValueError):
            parse_hotkey("hyper+f8")

    def test_modifier_only_target_raises(self):
        with pytest.raises(ValueError):
            parse_hotkey("ctrl+alt")

    def test_empty_hotkey_raises(self):
        with pytest.raises(ValueError):
            parse_hotkey("   ")


# ---------------------------------------------------------------------------
# Lecture d'événements simulés (push_to_talk)
# ---------------------------------------------------------------------------
class TestPushToTalk:
    def test_press_triggers_start_release_triggers_stop(self, monkeypatch):
        events = [
            ev(code=ecodes.KEY_F8, value=1),  # keydown
            ev(code=ecodes.KEY_F8, value=0),  # keyup
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 1
        assert stop.calls == 1

    def test_repeat_value2_ignored(self, monkeypatch):
        events = [
            ev(code=ecodes.KEY_F8, value=1),
            ev(code=ecodes.KEY_F8, value=2),  # repeat : ignoré
            ev(code=ecodes.KEY_F8, value=2),
            ev(code=ecodes.KEY_F8, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 1
        assert stop.calls == 1

    def test_non_key_events_ignored(self, monkeypatch):
        events = [
            ev(type_=0, code=0, value=0),        # EV_SYN
            ev(type_=2, code=0, value=1),        # EV_REL
            ev(code=ecodes.KEY_F8, value=1),
            ev(code=ecodes.KEY_F8, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 1
        assert stop.calls == 1

    def test_keyup_without_keydown_stops(self, monkeypatch):
        """Décision robuste : un keyup seul déclenche stop (le moteur ignore
        un stop hors enregistrement) — évite un enregistrement bloqué."""
        events = [ev(code=ecodes.KEY_F8, value=0)]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 0
        assert stop.calls == 1

    def test_other_keys_do_not_trigger(self, monkeypatch):
        events = [
            ev(code=KEY_A, value=1),
            ev(code=KEY_A, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 0
        assert stop.calls == 0

    def test_double_press_same_device_single_start(self, monkeypatch):
        # keydown deux fois sans release intermédiaire → un seul start
        events = [
            ev(code=ecodes.KEY_F8, value=1),
            ev(code=ecodes.KEY_F8, value=1),  # déjà enfoncée : ignoré
            ev(code=ecodes.KEY_F8, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 1
        assert stop.calls == 1


# ---------------------------------------------------------------------------
# Combinaisons avec modificateurs
# ---------------------------------------------------------------------------
class TestModifiers:
    def test_ctrl_held_then_space_triggers(self, monkeypatch):
        events = [
            ev(code=ecodes.KEY_LEFTCTRL, value=1),  # ctrl maintenu
            ev(code=ecodes.KEY_SPACE, value=1),     # press → start
            ev(code=ecodes.KEY_SPACE, value=0),     # release → stop
            ev(code=ecodes.KEY_LEFTCTRL, value=0),  # ctrl relâché
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("ctrl+space")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 1
        assert stop.calls == 1

    def test_target_pressed_without_required_modifier_does_not_start(
            self, monkeypatch):
        # hotkey = ctrl+space ; on presse space sans ctrl : pas de start
        events = [
            ev(code=ecodes.KEY_SPACE, value=1),
            ev(code=ecodes.KEY_SPACE, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("ctrl+space")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 0
        # keyup robuste : stop est appelé mais le moteur l'ignore
        assert stop.calls == 1

    def test_extra_modifier_does_not_trigger(self, monkeypatch):
        # hotkey = f8 ; on presse f8 avec ctrl : modificateur parasite → rien
        events = [
            ev(code=ecodes.KEY_LEFTCTRL, value=1),
            ev(code=ecodes.KEY_F8, value=1),
            ev(code=ecodes.KEY_F8, value=0),
            ev(code=ecodes.KEY_LEFTCTRL, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 0
        assert stop.calls == 1  # keyup robuste

    def test_modifier_release_before_target_release_still_stops(self, monkeypatch):
        # ctrl relâché AVANT space : le keyup de space stoppe quand même
        events = [
            ev(code=ecodes.KEY_LEFTCTRL, value=1),
            ev(code=ecodes.KEY_SPACE, value=1),     # start
            ev(code=ecodes.KEY_LEFTCTRL, value=0),  # ctrl relâché d'abord
            ev(code=ecodes.KEY_SPACE, value=0),     # stop quand même
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("ctrl+space")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 1
        assert stop.calls == 1

    def test_modifier_repeat_ignored(self, monkeypatch):
        events = [
            ev(code=ecodes.KEY_LEFTCTRL, value=1),
            ev(code=ecodes.KEY_LEFTCTRL, value=2),  # repeat ctrl : ignoré
            ev(code=ecodes.KEY_SPACE, value=1),
            ev(code=ecodes.KEY_SPACE, value=0),
            ev(code=ecodes.KEY_LEFTCTRL, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, start, stop = push_to_talk_manager("ctrl+space")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert start.calls == 1
        assert stop.calls == 1


# ---------------------------------------------------------------------------
# Mode toggle
# ---------------------------------------------------------------------------
class TestToggle:
    def _make(self, hotkey="f8"):
        calls = {"start": 0, "stop": 0}
        recording = {"v": False}

        def start():
            calls["start"] += 1
            recording["v"] = True

        def stop():
            calls["stop"] += 1
            recording["v"] = False

        manager = HotkeyManager(hotkey, "toggle", start, stop)
        manager.bind_recording_state(lambda: recording["v"])
        return manager, calls, recording

    def test_two_presses_toggle_start_then_stop(self, monkeypatch):
        events = [
            ev(code=ecodes.KEY_F8, value=1),  # press 1 → start
            ev(code=ecodes.KEY_F8, value=0),
            ev(code=ecodes.KEY_F8, value=1),  # press 2 → stop
            ev(code=ecodes.KEY_F8, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, calls, recording = self._make("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert calls["start"] == 1
        assert calls["stop"] == 1
        assert recording["v"] is False

    def test_repeat_does_not_retoggle(self, monkeypatch):
        events = [
            ev(code=ecodes.KEY_F8, value=1),  # press → start
            ev(code=ecodes.KEY_F8, value=2),  # repeat : rien
            ev(code=ecodes.KEY_F8, value=0),
            ev(code=ecodes.KEY_F8, value=1),  # press → stop
            ev(code=ecodes.KEY_F8, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, calls, recording = self._make("f8")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert calls["start"] == 1
        assert calls["stop"] == 1

    def test_toggle_requires_modifiers(self, monkeypatch):
        events = [
            ev(code=ecodes.KEY_SPACE, value=1),  # sans ctrl : pas de bascule
            ev(code=ecodes.KEY_SPACE, value=0),
            ev(code=ecodes.KEY_LEFTCTRL, value=1),
            ev(code=ecodes.KEY_SPACE, value=1),  # avec ctrl : bascule → start
            ev(code=ecodes.KEY_SPACE, value=0),
            ev(code=ecodes.KEY_LEFTCTRL, value=0),
        ]
        device = FakeDevice("/dev/input/event0", events)
        manager, calls, recording = self._make("ctrl+space")
        install_devices(monkeypatch, [device])
        run(manager, [device])
        assert calls["start"] == 1
        assert calls["stop"] == 0
        assert recording["v"] is True


# ---------------------------------------------------------------------------
# Multi-claviers & désinstallation
# ---------------------------------------------------------------------------
class TestMultiDeviceAndUninstall:
    def test_first_matching_device_triggers_others_deduplicated(self, monkeypatch):
        # Deux claviers signalent le même press F8 quasi simultanément :
        # un seul start (déduplication par code dans DEDUP_WINDOW).
        dev1 = FakeDevice("/dev/input/event0", [
            ev(code=ecodes.KEY_F8, value=1),
            ev(code=ecodes.KEY_F8, value=0),
        ])
        dev2 = FakeDevice("/dev/input/event1", [
            ev(code=ecodes.KEY_F8, value=1),  # dupliqué : ignoré
        ])
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [dev1, dev2])
        run(manager, [dev1, dev2])
        assert start.calls == 1
        assert stop.calls == 1

    def test_uninstall_closes_devices_and_stops_threads(self, monkeypatch):
        dev1 = BlockingDevice("/dev/input/event0", [])
        dev2 = BlockingDevice("/dev/input/event1", [])
        manager, start, stop = push_to_talk_manager("f8")
        install_devices(monkeypatch, [dev1, dev2])
        manager.install()
        threads = list(manager._threads)
        assert len(threads) == 2
        assert all(t.is_alive() for t in threads)
        manager.uninstall()
        assert dev1.closed is True
        assert dev2.closed is True
        assert all(not t.is_alive() for t in threads)

    def test_install_without_devices_raises_group_input(self, monkeypatch):
        monkeypatch.setattr(evdev, "list_devices", lambda: [])
        manager, start, stop = push_to_talk_manager("f8")
        with pytest.raises(HotkeyError, match="groupe input"):
            manager.install()

    def test_install_without_evkey_device_raises_group_input(self, monkeypatch):
        dev = FakeDevice("/dev/input/event0", [], capabilities={})
        install_devices(monkeypatch, [dev])
        manager, start, stop = push_to_talk_manager("f8")
        with pytest.raises(HotkeyError, match="groupe input"):
            manager.install()

    def test_install_unreadable_devices_skipped(self, monkeypatch):
        dev = FakeDevice("/dev/input/event0", [])
        install_devices(monkeypatch, [dev])
        manager, start, stop = push_to_talk_manager("f8")
        manager.install()
        manager.uninstall()
        assert start.calls == 0
        assert stop.calls == 0

    def test_invalid_hotkey_raises_value_error(self, monkeypatch):
        dev = FakeDevice("/dev/input/event0", [])
        install_devices(monkeypatch, [dev])
        manager, start, stop = push_to_talk_manager("unknown+key")
        with pytest.raises(ValueError):
            manager.install()
