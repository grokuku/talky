# -*- coding: utf-8 -*-
"""
tests/test_server_models.py
===========================
Fallback alias → repo IDs HuggingFace complets (côté serveur).

Talky utilise désormais les repo IDs HuggingFace complets partout
("mobiuslabsgmbh/faster-whisper-large-v3-turbo", "Systran/faster-whisper-*", …)
dans DEFAULT_MODEL, KNOWN_MODELS et REGISTRY_MODELS. Le serveur conserve
cependant ``resolve_repo()`` comme fallback SILENCIEUX : si un ancien
config.json contient encore un alias ("turbo", "large-v3", "medium", …),
il est traduit en repo ID complet pour ne pas casser l'existant.

Ce module importe server/server.py dans un environnement contrôlé (les
dépendances lourdes absentes — uvicorn, huggingface_hub, pydantic — sont
stubées et le cache HuggingFace est redirigé vers un répertoire temporaire)
puis vérifie :
  * la résolution des alias historiques en repo IDs complets (fallback) ;
  * le passage tel quel des repo IDs complets (y compris espaces) ;
  * l'absence d'alias dans les constantes exposées (DEFAULT_MODEL,
    KNOWN_MODELS, REGISTRY_MODELS).

Exécution : ``python3 -m pytest client/tests/test_server_models.py -q``
"""

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import contrôlé de server/server.py
# ---------------------------------------------------------------------------
# Le module exécute du code au niveau module (os.makedirs(CACHE_DIR, …),
# création de l'app FastAPI, enregistrement des routes) : on prépare donc
# l'environnement AVANT l'import pour qu'il soit inoffensif en test.
_CACHE = tempfile.mkdtemp(prefix="talky-hf-test-")
os.environ["HF_HOME"] = _CACHE
os.environ["HF_HUB_CACHE"] = _CACHE


def _ensure_module(name: str, attrs: dict | None = None) -> types.ModuleType:
    """Retourne le module réel s'il est importable, sinon un stub minimal
    (pattern « try import, sinon mock » identique à tests/conftest.py)."""
    try:
        __import__(name)
        return sys.modules[name]
    except Exception:  # noqa: BLE001 - ImportError et dérivés
        mod = types.ModuleType(name)
        for key, value in (attrs or {}).items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod


_ensure_module("uvicorn")
_ensure_module("huggingface_hub", {"snapshot_download": lambda *a, **k: ""})
_ensure_module("pydantic", {"BaseModel": object})

# Le mock fastapi de conftest (env sans fastapi installé) n'expose pas
# File/Form/HTTPException/UploadFile utilisés par server.py au niveau module
# (valeurs par défaut des paramètres de route) : on les complète si absents.
# File/Form sont APPELÉS à l'import (File(...), Form(DEFAULT_MODEL)) : ce
# doivent être des callables. HTTPException n'est utilisé qu'au runtime
# (raise), UploadFile qu'en annotation.
import fastapi as _fastapi  # noqa: E402  (conftest fournit le mock)

if not hasattr(_fastapi, "File"):
    _fastapi.File = lambda *a, **k: None
if not hasattr(_fastapi, "Form"):
    _fastapi.Form = lambda *a, **k: None
if not hasattr(_fastapi, "HTTPException"):
    _fastapi.HTTPException = Exception
if not hasattr(_fastapi, "UploadFile"):
    _fastapi.UploadFile = object

_SERVER_DIR = Path(__file__).resolve().parent.parent.parent / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import server  # noqa: E402  (import après préparation de l'environnement)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("alias,repo", [
    ("turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
    ("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
    ("large-v3", "Systran/faster-whisper-large-v3"),
    ("medium", "Systran/faster-whisper-medium"),
    ("small", "Systran/faster-whisper-small"),
    ("base", "Systran/faster-whisper-base"),
    ("tiny", "Systran/faster-whisper-tiny"),
])
def test_alias_fallback_resolves_to_full_repo(alias, repo):
    """Fallback compatibilité : un alias historique envoyé par un ancien
    config.json est résolu en repo ID HuggingFace complet."""
    assert server.resolve_repo(alias) == repo


def test_full_repo_ids_pass_through_unchanged():
    """Les repo IDs complets (déjà au nouveau format) passent tels quels —
    y compris avec des espaces de part et d'autre (strip)."""
    repo = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert server.resolve_repo(repo) == repo
    assert server.resolve_repo(f"  {repo}  ") == repo
    # Un repo ID d'une autre organisation n'est jamais réécrit.
    assert server.resolve_repo("openai/whisper-tiny") == "openai/whisper-tiny"


def test_unknown_alias_falls_back_to_systran_guess():
    """Un alias non listé (ex. "large-v2") est deviné via le préfixe
    Systran/faster-whisper-* (compatibilité large)."""
    assert server.resolve_repo("large-v2") == "Systran/faster-whisper-large-v2"


def test_constants_expose_full_repo_ids_only():
    """DEFAULT_MODEL, KNOWN_MODELS et REGISTRY_MODELS ne contiennent plus
    d'alias : uniquement des repo IDs complets (contenant '/').
    REGISTRY_MODELS.id == REGISTRY_MODELS.repo (même valeur)."""
    assert "/" in server.DEFAULT_MODEL
    assert all("/" in m for m in server.KNOWN_MODELS)
    for entry in server.REGISTRY_MODELS:
        assert "/" in entry["id"]
        assert entry["id"] == entry["repo"]
