# -*- coding: utf-8 -*-
"""
tests/test_injector.py
======================
Non-régression de l'injecteur Wayland (P4, §5.3 et §6.2 du roadmap.md) :
séquence wl-copy -> Ctrl+V (ydotool -> wtype -> evdev UInput) -> restauration.

Aucun outil système n'est exécuté : pyperclip est mocké (presse-papier
factice déterministe, même si le vrai pyperclip est installé) et subprocess
est mocké via la fixture `mock_subprocess` du conftest.
"""

import shutil
import subprocess

import pytest

import app.engine.injector as injector
from app.engine.injector import inject_text


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    """Supprime les délais réels (copie->Ctrl+V et Ctrl+V->restauration)."""
    monkeypatch.setattr(injector, "COPY_DELAY", 0)
    monkeypatch.setattr(injector, "PASTE_DELAY", 0)


@pytest.fixture(autouse=True)
def fake_clipboard(monkeypatch):
    """Presse-papier factice : déterministe même si le vrai pyperclip (qui
    appellerait wl-paste/wl-copy) est installé sur la machine de test."""
    import pyperclip

    state = {"value": "", "copies": []}

    def _copy(text):
        state["value"] = str(text)
        state["copies"].append(str(text))

    def _paste():
        return state["value"]

    monkeypatch.setattr(pyperclip, "copy", _copy)
    monkeypatch.setattr(pyperclip, "paste", _paste)
    return state


def _which_factory(available):
    """Construit un fake shutil.which : available = {nom: chemin | None}."""
    def _which(name):
        return available.get(name)
    return _which


def _find_run(calls, *argv):
    """Vrai si un appel subprocess.run(argv, ...) a été enregistré."""
    for kind, args, _kwargs in calls:
        if kind == "run" and args and list(args[0]) == list(argv):
            return True
    return False


YDTOOL_CTRL_V = ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]
WTYPE_CTRL_V = ["wtype", "-M", "ctrl", "v", "-m", "ctrl"]


# ---------------------------------------------------------------------------
# Séquence complète : wl-copy -> Ctrl+V -> restauration
# ---------------------------------------------------------------------------
class TestFullSequence:
    def test_copy_paste_restore_with_ydotool(self, fake_clipboard, mock_subprocess,
                                             monkeypatch):
        """Séquence nominale : ancien contenu sauvegardé, Ctrl+V via ydotool,
        ancien contenu restauré."""
        fake_clipboard["value"] = "ancien contenu"
        monkeypatch.setattr(shutil, "which",
                            _which_factory({"ydotool": "/usr/bin/ydotool"}))

        logs = []
        ok = inject_text(
            "Bonjour le monde",
            add_space=False,
            inject=True,
            keep_in_clipboard=False,
            log_callback=logs.append,
        )

        assert ok is True
        assert fake_clipboard["value"] == "ancien contenu"  # restauré
        # Copie initiale du texte, puis restauration.
        assert fake_clipboard["copies"] == ["Bonjour le monde", "ancien contenu"]
        # Ctrl+V envoyé via ydotool (pas wtype).
        assert _find_run(mock_subprocess, *YDTOOL_CTRL_V)
        assert not _find_run(mock_subprocess, *WTYPE_CTRL_V)
        assert any("restauré" in m for m in logs)

    def test_keep_in_clipboard_no_restore(self, fake_clipboard, mock_subprocess,
                                          monkeypatch):
        """keep_in_clipboard=True : le texte reste dans le presse-papier,
        l'ancien contenu n'est PAS restauré."""
        fake_clipboard["value"] = "ancien"
        monkeypatch.setattr(shutil, "which",
                            _which_factory({"ydotool": "/usr/bin/ydotool"}))

        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=True)

        assert ok is True
        assert fake_clipboard["value"] == "Bonjour"
        assert fake_clipboard["copies"] == ["Bonjour"]  # pas de restauration
        assert _find_run(mock_subprocess, *YDTOOL_CTRL_V)

    def test_add_space_suffix(self, fake_clipboard, mock_subprocess, monkeypatch):
        """add_space=True -> une espace est ajoutée au texte copié/collé."""
        monkeypatch.setattr(shutil, "which",
                            _which_factory({"ydotool": "/usr/bin/ydotool"}))

        ok = inject_text("Bonjour", add_space=True, inject=True,
                         keep_in_clipboard=True)

        assert ok is True
        assert fake_clipboard["value"] == "Bonjour "
        assert fake_clipboard["copies"] == ["Bonjour "]

    def test_inject_false_copy_only(self, fake_clipboard, mock_subprocess, monkeypatch):
        """inject=False + keep_in_clipboard=True : copie seule, aucun Ctrl+V."""
        fake_clipboard["value"] = "ancien"
        monkeypatch.setattr(shutil, "which", _which_factory({}))

        ok = inject_text("Copie seule", add_space=False, inject=False,
                         keep_in_clipboard=True)

        assert ok is True
        assert fake_clipboard["value"] == "Copie seule"
        assert fake_clipboard["copies"] == ["Copie seule"]
        assert mock_subprocess == []  # aucun sous-processus lancé

    def test_inject_false_no_keep_returns_false(self, fake_clipboard,
                                                mock_subprocess, monkeypatch):
        """inject=False + keep_in_clipboard=False : rien à faire -> False."""
        fake_clipboard["value"] = "ancien"
        monkeypatch.setattr(shutil, "which", _which_factory({}))

        ok = inject_text("Rien", add_space=False, inject=False,
                         keep_in_clipboard=False)

        assert ok is False
        assert fake_clipboard["value"] == "ancien"  # presse-papier intact
        assert fake_clipboard["copies"] == []
        assert mock_subprocess == []

    def test_empty_previous_restored(self, fake_clipboard, mock_subprocess,
                                     monkeypatch):
        """Ancien contenu vide : la restauration re-copie quand même ''."""
        fake_clipboard["value"] = ""  # presse-papier précédent vide
        monkeypatch.setattr(shutil, "which",
                            _which_factory({"ydotool": "/usr/bin/ydotool"}))

        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=False)

        assert ok is True
        assert fake_clipboard["copies"] == ["Bonjour", ""]  # restauration vide
        assert fake_clipboard["value"] == ""


# ---------------------------------------------------------------------------
# Chaîne de repli Ctrl+V
# ---------------------------------------------------------------------------
class TestFallback:
    def test_ydotool_absent_falls_back_to_wtype(self, fake_clipboard,
                                                mock_subprocess, monkeypatch):
        """ydotool absent -> wtype utilisé pour Ctrl+V."""
        monkeypatch.setattr(shutil, "which",
                            _which_factory({"wtype": "/usr/bin/wtype"}))

        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=False)

        assert ok is True
        assert _find_run(mock_subprocess, *WTYPE_CTRL_V)
        assert not _find_run(mock_subprocess, *YDTOOL_CTRL_V)

    def test_ydotool_failure_falls_back_to_wtype(self, fake_clipboard, monkeypatch):
        """ydotool présent mais en échec (code != 0) -> wtype utilisé."""
        monkeypatch.setattr(shutil, "which", _which_factory(
            {"ydotool": "/usr/bin/ydotool", "wtype": "/usr/bin/wtype"}))

        def _flaky_run(argv, **kwargs):
            if argv[0] == "ydotool":
                return subprocess.CompletedProcess(argv, returncode=1)
            return subprocess.CompletedProcess(argv, returncode=0)

        monkeypatch.setattr(subprocess, "run", _flaky_run)

        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=False)

        assert ok is True

    def test_wtype_absent_falls_back_to_uinput(self, fake_clipboard,
                                               mock_subprocess, monkeypatch):
        """ydotool et wtype absents -> evdev UInput (clavier virtuel)."""
        import evdev

        class FakeUInput:
            def __init__(self, events=None, **kwargs):
                self.events = events
                self.writes = []
                self.synced = False
                self.closed = False

            def write(self, etype, code, value):
                self.writes.append((etype, code, value))

            def syn(self):
                self.synced = True

            def close(self):
                self.closed = True

        fake_ui = FakeUInput()
        monkeypatch.setattr(evdev, "UInput", lambda *a, **kw: fake_ui,
                            raising=False)
        monkeypatch.setattr(shutil, "which", _which_factory({}))

        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=False)

        assert ok is True
        # KEY_LEFTCTRL down, KEY_V down, KEY_V up, KEY_LEFTCTRL up.
        assert fake_ui.writes == [(1, 29, 1), (1, 47, 1), (1, 47, 0), (1, 29, 0)]
        assert fake_ui.synced is True
        assert fake_ui.closed is True
        # Aucun sous-processus ydotool/wtype lancé.
        assert not _find_run(mock_subprocess, *YDTOOL_CTRL_V)
        assert not _find_run(mock_subprocess, *WTYPE_CTRL_V)

    def test_all_absent_returns_false_and_logs(self, fake_clipboard,
                                               mock_subprocess, monkeypatch):
        """ydotool, wtype et UInput indisponibles -> False + message clair."""
        monkeypatch.setattr(shutil, "which", _which_factory({}))
        monkeypatch.setattr(injector, "evdev", None)
        monkeypatch.setattr(injector, "ecodes", None)

        logs = []
        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=False, log_callback=logs.append)

        assert ok is False
        joined = " ".join(logs).lower()
        assert "indisponible" in joined or "impossible" in joined
        assert any("ydotool" in m.lower() for m in logs)
        assert any("wtype" in m.lower() for m in logs)

    def test_uinput_raises_returns_false(self, fake_clipboard, mock_subprocess,
                                         monkeypatch):
        """UInput présent mais en erreur -> False (dernier recours échoué)."""
        import evdev

        def _boom(*a, **kw):
            raise RuntimeError("permission refusée /dev/uinput")

        monkeypatch.setattr(evdev, "UInput", _boom, raising=False)
        monkeypatch.setattr(shutil, "which", _which_factory({}))

        logs = []
        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=False, log_callback=logs.append)

        assert ok is False
        assert any("uinput" in m.lower() for m in logs)


# ---------------------------------------------------------------------------
# Gestion de la chaîne vide (wl-copy refuse une sélection vide)
# ---------------------------------------------------------------------------
class TestEmptyClipboard:
    def test_wl_copy_failure_on_empty_falls_back_to_subprocess(self,
                                                               fake_clipboard,
                                                               mock_subprocess,
                                                               monkeypatch):
        """pyperclip.copy('') échoue (wl-copy / sélection vide) -> repli sur
        une exécution directe de wl-copy (stdin vide / --clear)."""
        import pyperclip

        fake_clipboard["value"] = ""
        monkeypatch.setattr(shutil, "which", _which_factory(
            {"ydotool": "/usr/bin/ydotool", "wl-copy": "/usr/bin/wl-copy"}))

        real_copy = pyperclip.copy

        def _copy_raising_on_empty(text):
            if str(text) == "":
                raise RuntimeError("wl-copy: empty selection")
            return real_copy(text)

        monkeypatch.setattr(pyperclip, "copy", _copy_raising_on_empty)

        ok = inject_text("Bonjour", add_space=False, inject=True,
                         keep_in_clipboard=False)

        assert ok is True
        assert _find_run(mock_subprocess, "wl-copy", "--clear") or \
               _find_run(mock_subprocess, "wl-copy")


# ---------------------------------------------------------------------------
# Robustesse / signature
# ---------------------------------------------------------------------------
class TestRobustness:
    def test_default_log_callback_is_safe(self, fake_clipboard, mock_subprocess,
                                          monkeypatch):
        """Appel sans log_callback : pas d'erreur."""
        monkeypatch.setattr(shutil, "which",
                            _which_factory({"ydotool": "/usr/bin/ydotool"}))
        ok = inject_text("Bonjour", add_space=False)
        assert ok is True

    def test_returns_bool(self, fake_clipboard, mock_subprocess, monkeypatch):
        """La fonction renvoie toujours un booléen."""
        monkeypatch.setattr(shutil, "which",
                            _which_factory({"ydotool": "/usr/bin/ydotool"}))
        result = inject_text("x", add_space=False, inject=True,
                             keep_in_clipboard=True)
        assert isinstance(result, bool)

    def test_module_importable_without_dependencies(self):
        """Le module expose les fonctions clés et gère pyperclip/evdev None."""
        assert callable(injector.inject_text)
        # En environnement réel, pyperclip/evdev peuvent être None ou mockés ;
        # l'important est que inject_text soit appelable dans tous les cas.
        assert injector.COPY_DELAY >= 0
        assert injector.PASTE_DELAY >= 0
