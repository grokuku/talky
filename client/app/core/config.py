# -*- coding: utf-8 -*-
"""
app/core/config.py
==================
Chargement, sauvegarde et validation de la configuration (config.json).
"""

import json
import logging
from pathlib import Path

from app.engine.hotkeys import parse_hotkey

log = logging.getLogger("talky")

# ---------------------------------------------------------------------------
# Chemins du projet (racine = parent de 3 niveaux : app/core/config.py)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # client/
CONFIG_PATH = BASE_DIR / "config.json"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# Configuration par défaut (défini au §5.8 du roadmap.md)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "server_url": "http://192.168.1.50:8000",  # serveur whisper-live (LAN, REST 8000)
    "server_api_key": "",                      # vide = pas d'authentification
    "server_timeout": 30,                      # timeout lecture httpx (s), >= 5
    "ws_port": 9090,                           # port WebSocket whisper-live (temps réel)
    "model": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",  # repo ID HuggingFace complet (alias historiques obsolètes)
    "language": "fr",                          # fr | en | ... | auto (None = auto)
    "task": "transcribe",                      # transcribe | translate
    "vad_filter": True,                        # filtre VAD (détection de parole)
    "hotkey": "f8",                            # raccourci (f8, ctrl+space, ...)
    "input_mode": "push_to_talk",              # push_to_talk | toggle
    "audio_device": None,                      # index du micro (None = défaut)
    "inject_text": True,                       # coller le texte via Ctrl+V
    "add_space": True,                         # ajouter un espace après le texte
    "keep_in_clipboard": False,                # conserver le texte dans le presse-papier
    "auto_start": False,                       # démarrer le moteur au lancement
    "max_history": 50,                         # taille max de l'historique
    "continuous_mode": True,                   # transcription continue (WebSocket WhisperLive) vs batch
    "compute_type": "int8",                    # int8 | int8_float16 | float16 | float32
}


def load_config(cfg_path: Path | None = None) -> dict:
    """Charge la configuration en fusionnant avec les défauts (robuste).

    Les clés inconnues présentes dans config.json sont ignorées : seules
    les clés de DEFAULT_CONFIG sont conservées. Un fichier absent, illisible
    ou non-objet retombe sur les défauts.
    """
    cfg_path = cfg_path or CONFIG_PATH
    if not Path(cfg_path).exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"config.json illisible ({exc}), défauts utilisés.")
        return dict(DEFAULT_CONFIG)
    if not isinstance(user_cfg, dict):
        log.warning("config.json invalide (JSON non-objet), défauts utilisés.")
        return dict(DEFAULT_CONFIG)
    # Fusion défauts + clés connues uniquement.
    cfg = {key: user_cfg.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    return cfg


def save_config(cfg: dict, cfg_path: Path | None = None) -> None:
    """Écrit la configuration dans config.json (joli formatage)."""
    cfg_path = cfg_path or CONFIG_PATH
    try:
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.error(f"Impossible d'écrire {cfg_path} : {exc}")
        raise


def validate_config(cfg: dict) -> dict:
    """Validation + normalisation des champs critiques (ne mute pas cfg).

    Normalisations : language auto→None, entiers (server_timeout, ws_port,
    max_history, audio_device), bools (vad_filter, inject_text, add_space,
    keep_in_clipboard, auto_start). Erreurs : server_url non vide,
    server_timeout >= 5, ws_port >= 1.
    """
    cfg = dict(cfg)  # ne jamais muter l'appelant
    errors: dict[str, str] = {}

    # --- Collecte des erreurs ---
    server_url = str(cfg.get("server_url") or "").strip()
    if not server_url:
        errors["server_url"] = "server_url ne peut pas être vide."

    # model peut être vide (aucun modèle installé/sélectionné) : on l'accepte
    # tel quel pour permettre la sauvegarde (le frontend affiche un warning).
    # (M2) La hotkey est validée ICI via parse_hotkey (mêmes règles que
    # HotkeyManager.install), AVANT toute écriture : un raccourci malformé
    # ("ctrl+f25", "ctrl+ctrl", ...) est rejeté d'entrée — la config.json
    # n'est jamais polluée d'une hotkey qui rendrait le moteur sourd.
    if not cfg.get("hotkey"):
        errors["hotkey"] = "hotkey ne peut pas être vide."
    else:
        try:
            parse_hotkey(cfg["hotkey"])
        except ValueError as exc:
            errors["hotkey"] = str(exc)
    if cfg.get("task") not in ("transcribe", "translate"):
        errors["task"] = "task doit être 'transcribe' ou 'translate'."
    if cfg.get("compute_type") not in ("int8", "int8_float16", "float16", "float32"):
        errors["compute_type"] = ("compute_type doit être 'int8', 'int8_float16', "
                                 "'float16' ou 'float32'.")
    if cfg.get("input_mode") not in ("push_to_talk", "toggle"):
        errors["input_mode"] = "input_mode doit être 'push_to_talk' ou 'toggle'."

    try:
        server_timeout = int(cfg.get("server_timeout", 30))
    except (TypeError, ValueError):
        server_timeout = 0
    if server_timeout < 5:
        errors["server_timeout"] = "server_timeout doit être un entier >= 5."

    try:
        ws_port = int(cfg.get("ws_port", 9090))
    except (TypeError, ValueError):
        ws_port = 0
    if ws_port < 1:
        errors["ws_port"] = "ws_port doit être un entier >= 1."

    try:
        max_history = max(1, int(cfg.get("max_history", 50)))
    except (TypeError, ValueError):
        errors["max_history"] = "max_history doit être un entier >= 1."
        max_history = 1

    if errors:
        raise ValueError(json.dumps(errors))

    # --- Normalisations silencieuses ---
    cfg["server_url"] = server_url
    cfg["server_timeout"] = server_timeout
    cfg["ws_port"] = ws_port
    cfg["max_history"] = max_history
    cfg["model"] = str(cfg.get("model") or "")

    # Langue : auto / vide -> None (auto-détection côté serveur whisper-live).
    language = cfg.get("language")
    cfg["language"] = None if language in (None, "", "auto") else str(language)

    cfg["vad_filter"] = bool(cfg.get("vad_filter", True))
    cfg["inject_text"] = bool(cfg.get("inject_text", True))
    cfg["add_space"] = bool(cfg.get("add_space", True))
    cfg["keep_in_clipboard"] = bool(cfg.get("keep_in_clipboard", False))
    cfg["auto_start"] = bool(cfg.get("auto_start", False))
    cfg["continuous_mode"] = bool(cfg.get("continuous_mode", True))
    cfg["compute_type"] = str(cfg.get("compute_type") or "int8")

    # Périphérique audio : index entier (None = périphérique par défaut).
    # Une chaîne "1" serait interprétée comme un NOM de device par sounddevice.
    if cfg.get("audio_device"):
        cfg["audio_device"] = int(cfg["audio_device"])
    else:
        cfg["audio_device"] = None
    return cfg
