# -*- coding: utf-8 -*-
"""
app/engine/hotkeys.py
=====================
Raccourcis clavier globaux via python-evdev (P3, §5.2 / §6.1).

Remplace le module `keyboard` (incompatible Wayland) par une **lecture
passive** de /dev/input/event* : aucun `grab()` n'est posé, donc aucune
application n'est bloquée et aucun conflit avec KDE Plasma.

Deux modes de saisie (voir roadmap) :
  * push_to_talk : maintenir la touche pour enregistrer, relâcher pour
    transcrire (F8 par défaut) ;
  * toggle       : appuyer démarre, appuyer à nouveau arrête.

Permissions (R1 roadmap) : l'utilisateur doit appartenir au groupe `input`
pour lire /dev/input — sinon `install()` lève `HotkeyError` avec un message
actionnable (« sudo usermod -aG input $USER »).

Multi-claviers : un thread daemon par device ; le premier device qui signale
la combinaison déclenche, les événements dupliqués (même code dans une
fenêtre très courte) sont ignorés. Les évènements `value=2` (repeat) sont
ignorés.
"""

import glob
import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Set

# --- import conditionnel : le module s'importe même SANS evdev installé ---
# (evdev est un paquet Arch « python-evdev », pas de wheel PyPI ; les tests
#  mockent evdev via conftest.py).
try:
    import evdev
    from evdev import ecodes

    _EVIDEV_AVAILABLE = True
except Exception:  # noqa: BLE001 — ImportError / OSError en environnement nu
    evdev = None
    ecodes = None
    _EVIDEV_AVAILABLE = False

log = logging.getLogger("talky")


class HotkeyError(RuntimeError):
    """Erreur d'installation des hotkeys (permissions, aucun device, ...)."""


# ---------------------------------------------------------------------------
# Tables de correspondance (codes evdev standard linux/input-event-codes.h)
# ---------------------------------------------------------------------------
# Chaque valeur numérique sert de repli si la constante n'est pas exposée
# (mock minimaliste, variantes d'evdev) : en production, getattr() privilégie
# toujours la constante `ecodes.KEY_*` réelle (même valeur, plus robuste).
_KEY_CODES: Dict[str, int] = {
    # Lettres (codes clavier US)
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
    "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
    "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
    "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
    # Chiffres (rangée supérieure)
    "0": 11, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7,
    "7": 8, "8": 9, "9": 10,
    # Touches de fonction
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 69, "f12": 70,
    # Navigation / édition
    "enter": 28, "tab": 15, "esc": 1, "backspace": 14,
    "delete": 111, "insert": 110, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "caps_lock": 58, "space": 57,
    # Ponctuation
    "minus": 12, "equal": 13, "bracketleft": 26, "bracketright": 27,
    "backslash": 43, "semicolon": 39, "apostrophe": 40, "grave": 41,
    "comma": 51, "dot": 52, "slash": 53,
}

# Modificateurs reconnus dans la chaîne hotkey ("ctrl+alt+f9").
_MODIFIER_NAMES = ("ctrl", "alt", "shift", "super")

# Code evdev → nom canonique, pour suivre l'état gauche/droite fusionné.
_MODIFIER_CODES: Dict[int, str] = {
    getattr(ecodes, "KEY_LEFTCTRL", 29): "ctrl",
    getattr(ecodes, "KEY_RIGHTCTRL", 97): "ctrl",
    getattr(ecodes, "KEY_LEFTALT", 56): "alt",
    getattr(ecodes, "KEY_RIGHTALT", 100): "alt",
    getattr(ecodes, "KEY_LEFTSHIFT", 42): "shift",
    getattr(ecodes, "KEY_RIGHTSHIFT", 54): "shift",
    getattr(ecodes, "KEY_LEFTMETA", 125): "super",
    getattr(ecodes, "KEY_RIGHTMETA", 126): "super",
}

# Fenêtre (s) de déduplication multi-claviers : un même code signalé par un
# second device dans cet intervalle est ignoré (le premier a déclenché).
DEDUP_WINDOW = 0.15


# ---------------------------------------------------------------------------
# Résolution hotkey string → (code cible, modificateurs requis)
# ---------------------------------------------------------------------------
def key_code(name: str) -> int:
    """Retourne le code evdev d'une touche simple (« f8 », « space », « a »...).

    Privilégie la constante `ecodes.KEY_*` quand elle existe (production),
    sinon retombe sur le code numérique standard de la table.
    """
    key_name = str(name).lower().strip()
    if not key_name:
        raise ValueError("Nom de touche vide.")
    numeric = _KEY_CODES.get(key_name)
    if numeric is None:
        raise ValueError(
            f"Touche inconnue : « {name} » (clés supportées : lettres, "
            f"chiffres, f1-f12, space, enter, tab, esc, ...)."
        )
    constant = "KEY_" + key_name.upper()
    return getattr(ecodes, constant, numeric) if ecodes is not None else numeric


def parse_hotkey(hotkey: str) -> tuple:
    """Décompose une chaîne hotkey en (code_cible, modificateurs_requis).

    Exemples : "f8" → (KEY_F8, frozenset()) ;
               "ctrl+space" → (KEY_SPACE, frozenset({"ctrl"})) ;
               "ctrl+alt+f9" → (KEY_F9, frozenset({"ctrl", "alt"})).
    """
    parts = [p.strip().lower() for p in str(hotkey).split("+") if p.strip()]
    if not parts:
        raise ValueError("Hotkey vide.")
    modifiers: Set[str] = set()
    for part in parts[:-1]:
        if part not in _MODIFIER_NAMES:
            raise ValueError(
                f"Modificateur inconnu : « {part} » (ctrl, alt, shift, super)."
            )
        modifiers.add(part)
    target = parts[-1]
    if target in _MODIFIER_NAMES:
        raise ValueError(
            f"La touche cible « {target} » ne peut pas être un modificateur "
            f"seul — utilisez une combinaison comme « {target}+space »."
        )
    return key_code(target), frozenset(modifiers)


class HotkeyManager:
    """Installation / retrait des hooks clavier globaux (evdev, passif)."""

    def __init__(
        self,
        hotkey: str,
        mode: str,
        on_record_start: Callable[[], None],
        on_record_stop: Callable[[], None],
    ) -> None:
        self.hotkey = str(hotkey).lower().strip()
        self.mode = mode
        self._on_record_start = on_record_start
        self._on_record_stop = on_record_stop
        self._recording_predicate: Optional[Callable[[], bool]] = None

        # Résolution différée (install()) pour permettre un hotkey vide au
        # moment de la construction (la config est validée par ailleurs).
        self._target_code: Optional[int] = None
        self._required_mods: frozenset = frozenset()

        self._devices: List = []
        self._threads: List[threading.Thread] = []
        self._states: Dict[int, dict] = {}   # id(device) -> état par device
        self._stopping = False
        self._lock = threading.Lock()
        self._last_press: Dict[int, tuple] = {}  # code -> (device_id, horodatage)

    # ------------------------------------------------------------------
    # État d'enregistrement (délégué au moteur, comme l'original)
    # ------------------------------------------------------------------
    def bind_recording_state(self, predicate: Callable[[], bool]) -> None:
        """Le moteur expose un prédicat « est-on en train d'enregistrer ? »,
        utilisé par le mode toggle pour décider du sens de la bascule."""
        self._recording_predicate = predicate

    def _recording_in_progress(self) -> bool:
        return bool(self._recording_predicate and self._recording_predicate())

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------
    def install(self) -> None:
        """Ouvre les devices EV_KEY et démarre un thread de lecture par device.

        Lecture passive : aucun grab() — les autres applications continuent
        de recevoir les touches normalement.

        Lève `HotkeyError` si evdev est absent, si aucun /dev/input n'est
        lisible (groupe `input` manquant) ou si aucun device n'expose EV_KEY.
        """
        if not _EVIDEV_AVAILABLE:
            raise HotkeyError(
                "python-evdev n'est pas installé. Installez le paquet Arch "
                "« python-evdev » (pas de wheel PyPI)."
            )

        self._target_code, self._required_mods = parse_hotkey(self.hotkey)

        paths = self._enumerate_device_paths()
        if not paths:
            raise HotkeyError(
                "Aucun périphérique d'entrée trouvé dans /dev/input. "
                "Ajouter l'utilisateur au groupe input "
                "(sudo usermod -aG input $USER, puis déconnexion) "
                "et vérifier que /dev/input/event* existe."
            )

        devices: List = []
        for path in paths:
            try:
                device = evdev.InputDevice(path)
            except Exception as exc:  # noqa: BLE001 — OSError permissions, ...
                log.warning(f"Device {path} illisible ({exc}), ignoré.")
                continue
            try:
                caps = device.capabilities(verbose=False)
            except Exception:  # noqa: BLE001
                caps = {}
            if ecodes.EV_KEY not in caps:
                log.debug(f"Device {path} sans EV_KEY, ignoré.")
                device.close()
                continue
            devices.append(device)

        if not devices:
            raise HotkeyError(
                "Aucun clavier EV_KEY accessible dans /dev/input. "
                "Ajouter l'utilisateur au groupe input "
                "(sudo usermod -aG input $USER, puis déconnexion)."
            )

        self._devices = devices
        self._states = {}
        self._last_press = {}
        self._stopping = False

        for device in devices:
            state = {"mods": set(), "target_down": False}
            self._states[id(device)] = state
            thread = threading.Thread(
                target=self._read_loop,
                args=(device, state),
                name="talky-hotkeys",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

        log.info(
            f"Raccourci {self.mode} activé : « {self.hotkey} » "
            f"({len(devices)} périphérique(s) EV_KEY)."
        )

    @staticmethod
    def _enumerate_device_paths() -> List[str]:
        """Énumère /dev/input/event*.

        `evdev.list_devices()` est l'API canonique : on la respecte même si
        elle renvoie une liste vide (aucun device lisible -> erreur groupe
        input). Le glob n'est qu'un secours si l'API est indisponible.
        """
        if evdev is not None and hasattr(evdev, "list_devices"):
            try:
                return list(evdev.list_devices())
            except Exception:  # noqa: BLE001
                pass
        return sorted(glob.glob("/dev/input/event*"))

    # ------------------------------------------------------------------
    # Boucle de lecture (un thread daemon par device)
    # ------------------------------------------------------------------
    def _read_loop(self, device, state: dict) -> None:
        """Itère sur les événements du device jusqu'à l'arrêt / la fermeture."""
        try:
            for event in device.read_loop():
                if self._stopping:
                    break
                self._handle_event(device, state, event)
        except OSError as exc:
            # Device débranché ou fermé pendant uninstall() : silencieux.
            if not self._stopping:
                log.warning(f"Périphérique {device.path} fermé pendant la lecture : {exc}")
        except Exception as exc:  # noqa: BLE001
            if not self._stopping:
                log.error(f"Erreur de lecture evdev sur {device.path} : {exc}")

    # ------------------------------------------------------------------
    # Traitement d'un événement
    # ------------------------------------------------------------------
    def _handle_event(self, device, state: dict, event) -> None:
        """Filtre EV_KEY, suit les modificateurs, détecte la combinaison."""
        if event.type != ecodes.EV_KEY:
            return
        code = event.code
        value = event.value  # 0 = relâché, 1 = pressé, 2 = repeat

        if code in _MODIFIER_CODES:
            self._update_modifier(state, code, value)
            return

        if code != self._target_code:
            return

        if value == 2:
            return  # repeat : ignoré (maintien prolongé de la touche)

        if value == 0:  # keyup
            state["target_down"] = False
            if self.mode == "push_to_talk":
                # Robuste : on stoppe même sans keydown préalable sur ce
                # device (le moteur ignore un stop hors enregistrement) —
                # évite un enregistrement bloqué si un keydown a été manqué.
                self._safe_call(self._on_record_stop)
            return

        # value == 1 : keydown (edge)
        if state["target_down"]:
            return  # déjà enfoncée sur ce device, sans release
        if not self._modifiers_match(state):
            return  # combinaison de modificateurs non satisfaite
        if self._deduplicated(device, code):
            return  # un autre device a déjà déclenché ce code
        state["target_down"] = True
        if self.mode == "toggle":
            self._toggle()
        else:
            self._safe_call(self._on_record_start)

    def _update_modifier(self, state: dict, code: int, value: int) -> None:
        """Met à jour l'état des modificateurs (gauche/droite fusionnés)."""
        name = _MODIFIER_CODES[code]
        if value == 1:
            state["mods"].add(name)
        elif value == 0:
            state["mods"].discard(name)
        # value == 2 (repeat) : aucun changement.

    def _modifiers_match(self, state: dict) -> bool:
        """Vrai si les modificateurs maintenus correspondent EXACTEMENT à la
        combinaison configurée (aucun modificateur parasite)."""
        return self._required_mods == state["mods"]

    def _deduplicated(self, device, code: int) -> bool:
        """Déduplication multi-claviers : le premier device qui signale un
        code dans la fenêtre DEDUP_WINDOW déclenche ; le même code signalé
        par un AUTRE device dans cet intervalle est ignoré (« premier device
        qui matche »). Un même device peut re-déclencher immédiatement
        (deux appuis rapides légitimes ne sont pas avalés)."""
        now = time.monotonic()
        dev_id = id(device)
        with self._lock:
            last_dev, last_ts = self._last_press.get(code, (None, 0.0))
            if last_dev is not None and last_dev != dev_id and now - last_ts < DEDUP_WINDOW:
                return True
            self._last_press[code] = (dev_id, now)
            return False

    # ------------------------------------------------------------------
    # Bascule / appels
    # ------------------------------------------------------------------
    def _toggle(self) -> None:
        """Bascule enregistrement / transcription (mode Toggle)."""
        if self._recording_in_progress():
            self._safe_call(self._on_record_stop)
        else:
            self._safe_call(self._on_record_start)

    @staticmethod
    def _safe_call(callback: Optional[Callable[[], None]]) -> None:
        """Appelle un callback utilisateur en isolant les exceptions."""
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 — un callback ne doit jamais tuer
            log.exception("Erreur dans un callback hotkey.")

    # ------------------------------------------------------------------
    # Désinstallation
    # ------------------------------------------------------------------
    def uninstall(self) -> None:
        """Arrête les threads, ferme les devices (best-effort, try/except)."""
        self._stopping = True
        threads = list(self._threads)
        for device in self._devices:
            try:
                device.close()
            except Exception:  # noqa: BLE001
                pass
        for thread in threads:
            try:
                thread.join(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        self._threads = []
        self._devices = []
        self._states = {}
        self._last_press = {}
