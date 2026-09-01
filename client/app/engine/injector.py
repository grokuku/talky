# -*- coding: utf-8 -*-
"""
app/engine/injector.py
======================
Injection du texte transcrit dans la fenêtre active sous Wayland (CachyOS,
KDE Plasma) VIA le presse-papier (pyperclip -> wl-copy/wl-paste) + Ctrl+V,
afin de conserver les accents français (é, è, à, ç...).

Remplace l'original Windows (ref/app/engine/injector.py) qui utilisait le
module `keyboard` : sous Wayland, l'injection se fait par copie du texte
puis envoi de Ctrl+V via une chaîne de repli (§5.3 et §6.2 du roadmap.md) :

    1. ydotool   (daemon ydotoold, `systemctl --user enable --now ydotool`)
    2. wtype     (outil autonome, `wtype -M ctrl v -m ctrl`)
    3. evdev UInput pur Python (dernier recours, aucun daemon requis)

Si `keep_in_clipboard` est faux, l'ancien contenu du presse-papier est
restauré après le collage (même s'il est vide : wl-copy refuse une sélection
vide, on passe alors par un stdin vide / `wl-copy --clear`).

Le module s'importe SANS pyperclip ni evdev installés (try/except) : dans ce
cas, les fonctions se dégradent (retour False + log), ce qui permet aux tests
de mocker proprement les dépendances.
"""

import logging
import shutil
import subprocess
import time
from typing import Callable, Optional

log = logging.getLogger("talky")

# ---------------------------------------------------------------------------
# Dépendances optionnelles : le module doit pouvoir s'importer sans elles.
# ---------------------------------------------------------------------------
try:
    import pyperclip
except Exception:  # pragma: no cover - pyperclip absent
    pyperclip = None

try:
    import evdev
    from evdev import ecodes
except Exception:  # pragma: no cover - python-evdev absent
    evdev = None
    ecodes = None

# ---------------------------------------------------------------------------
# Délais (secondes) — laisser Wayland rendre la sélection disponible avant
# Ctrl+V, et laisser le collage se terminer avant la restauration (§6.2).
# ---------------------------------------------------------------------------
COPY_DELAY = 0.05   # copie -> Ctrl+V (50-100 ms)
PASTE_DELAY = 0.1   # Ctrl+V -> restauration du presse-papier


def _notify(log_callback: Optional[Callable[[str], None]]) -> Callable[[str], None]:
    """Retourne un callable de log sûr (jamais None)."""
    return log_callback or (lambda _msg: None)


def _clipboard_paste(log_callback: Callable[[str], None]) -> Optional[str]:
    """Lit le presse-papier (wl-paste via pyperclip). None si indisponible."""
    if pyperclip is None:
        log_callback("Presse-papier indisponible (pyperclip non installé).")
        return None
    try:
        return pyperclip.paste()
    except Exception as exc:  # noqa: BLE001
        log_callback(f"pyperclip.paste a échoué : {exc}")
        return None


def _clear_clipboard_via_wl_copy(log_callback: Callable[[str], None]) -> bool:
    """Vide le presse-papier via wl-copy (repli pour la chaîne vide).

    wl-copy refuse une sélection vide passée en stdin : on tente d'abord
    `wl-copy --clear` (wl-clipboard 2.x), puis un stdin vide en secours.
    """
    for argv in (["wl-copy", "--clear"], ["wl-copy"]):
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.run(argv, input=b"", capture_output=True, timeout=5)
            return True
        except Exception as exc:  # noqa: BLE001
            log_callback(f"{' '.join(argv)} a échoué : {exc}")
    log_callback("wl-copy introuvable : impossible de vider le presse-papier.")
    return False


def _clipboard_copy(text, log_callback: Callable[[str], None]) -> bool:
    """Copie `text` dans le presse-papier via pyperclip.

    Cas particulier chaîne vide : wl-copy (via pyperclip) échoue sur une
    sélection vide ; on retombe alors sur une exécution directe de wl-copy
    (stdin vide / --clear) pour vider la sélection.
    """
    text = "" if text is None else str(text)
    if pyperclip is None:
        log_callback("Presse-papier indisponible (pyperclip non installé).")
        return False
    try:
        pyperclip.copy(text)
        return True
    except Exception as exc:  # noqa: BLE001
        log_callback(f"pyperclip.copy a échoué : {exc}")
    if text == "":
        return _clear_clipboard_via_wl_copy(log_callback)
    return False


def _restore_clipboard(previous, log_callback: Callable[[str], None]) -> bool:
    """Restaure l'ancien contenu du presse-papier (même s'il est vide)."""
    if previous is None:
        return False
    return _clipboard_copy(previous, log_callback)


def _send_ctrl_v_uinput(log_callback: Callable[[str], None]) -> bool:
    """Dernier recours : evdev UInput pur Python (aucun daemon requis).

    Crée un périphérique clavier virtuel et émet :
    KEY_LEFTCTRL down, KEY_V down, KEY_V up, KEY_LEFTCTRL up.
    """
    if evdev is None or ecodes is None:
        log_callback("evdev indisponible : pas de repli UInput possible.")
        return False
    ui_cls = getattr(evdev, "UInput", None)
    if ui_cls is None:
        log_callback("evdev.UInput indisponible : pas de repli UInput possible.")
        return False
    ev_key = getattr(ecodes, "EV_KEY", 0x01)
    key_leftctrl = getattr(ecodes, "KEY_LEFTCTRL", 29)
    key_v = getattr(ecodes, "KEY_V", 47)
    try:
        ui = ui_cls(events={ev_key: [key_leftctrl, key_v]})
        try:
            ui.write(ev_key, key_leftctrl, 1)  # Ctrl enfoncé
            ui.write(ev_key, key_v, 1)          # V enfoncé
            ui.write(ev_key, key_v, 0)          # V relâché
            ui.write(ev_key, key_leftctrl, 0)   # Ctrl relâché
            ui.syn()
        finally:
            ui.close()
        return True
    except Exception as exc:  # noqa: BLE001
        log_callback(f"UInput a échoué : {exc}")
        return False


def _send_ctrl_v(log_callback: Callable[[str], None]) -> bool:
    """Envoie Ctrl+V dans la fenêtre active.

    Chaîne de repli : ydotool -> wtype -> evdev UInput. Renvoie True dès
    qu'un canal a fonctionné, False si tous ont échoué.
    """
    # 1) ydotool (daemon ydotoold) — le plus fiable sous KDE/Wayland.
    if shutil.which("ydotool"):
        try:
            result = subprocess.run(
                ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return True
            log_callback(f"ydotool a échoué (code {result.returncode}).")
        except Exception as exc:  # noqa: BLE001
            log_callback(f"ydotool indisponible : {exc}")
    else:
        log_callback("ydotool introuvable (daemon ydotoold ?).")

    # 2) wtype — outil autonome Wayland.
    if shutil.which("wtype"):
        try:
            result = subprocess.run(
                ["wtype", "-M", "ctrl", "v", "-m", "ctrl"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return True
            log_callback(f"wtype a échoué (code {result.returncode}).")
        except Exception as exc:  # noqa: BLE001
            log_callback(f"wtype indisponible : {exc}")
    else:
        log_callback("wtype introuvable.")

    # 3) evdev UInput pur Python (aucun daemon).
    return _send_ctrl_v_uinput(log_callback)


def inject_text(
    text: str,
    *,
    add_space: bool = True,
    inject: bool = True,
    keep_in_clipboard: bool = False,
    log_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Copie (et éventuellement colle) le texte transcrit.

    :param text: texte transcrit par le moteur.
    :param add_space: ajouter une espace après le texte.
    :param inject: coller via Ctrl+V dans la fenêtre active.
    :param keep_in_clipboard: conserver le texte dans le presse-papier
                              (sinon restauration de l'ancien contenu).
    :param log_callback: reçoit des messages de log (optionnel).
    :return: True si le texte a été collé ou copié, False sinon.
    """
    notify = _notify(log_callback)
    suffix = " " if add_space else ""
    payload = str(text) + suffix

    # --- Pas d'injection : copie seule si demandé ---
    if not inject:
        if keep_in_clipboard:
            if _clipboard_copy(payload, notify):
                notify("Texte copié dans le presse-papier.")
                return True
            return False
        return False  # rien à faire

    # --- Sauvegarde de l'ancien contenu (wl-paste) ---
    previous = _clipboard_paste(notify)
    if previous is None:
        notify("Impossible de lire le presse-papier ; restauration ignorée.")
        previous = ""

    # --- Copie du texte + suffixe (wl-copy) ---
    if not _clipboard_copy(payload, notify):
        notify("Échec de la copie du texte : injection annulée.")
        return False

    # Laisse Wayland rendre la sélection disponible (50-100 ms).
    time.sleep(COPY_DELAY)

    # --- Ctrl+V dans la fenêtre active (ydotool -> wtype -> UInput) ---
    if not _send_ctrl_v(notify):
        notify("Injection Ctrl+V impossible : ydotool, wtype et UInput indisponibles.")
        return False

    # Attend la fin du collage avant de restaurer le presse-papier.
    time.sleep(PASTE_DELAY)

    # --- Restauration de l'ancien contenu ---
    if not keep_in_clipboard:
        if _restore_clipboard(previous, notify):
            notify("Presse-papier restauré (contenu précédent).")
        else:
            notify("Impossible de restaurer l'ancien contenu du presse-papier.")

    notify(f"Texte injecté dans la fenêtre active ({len(payload)} caractères).")
    return True
