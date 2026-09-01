# -*- coding: utf-8 -*-
"""
tests/test_server_model_manager.py
====================================
Tests PURS de ``ModelManager`` (server/server.py) — sans GPU ni
faster-whisper. On importe server/server.py dans un environnement contrôlé
(uvicorn / huggingface_hub / pydantic stubés, cache HF redirigé, fastapi
complété comme dans test_server_models.py) puis on instancie un
``ModelManager`` isolé en injectant de FAUX objets modèle dans
``_models``/``_last_used``/``_in_use``.

``faster_whisper`` étant importé paresseusement dans ``get()``, on le stube
via sys.modules (le chargement réel n'a jamais lieu) : ``WhisperModel`` est
remplacé par une classe factice qui enregistre les repo/champs instanciés.

Couvre : ``_env_int`` (défauts, invalide, plancher), touch LRU, éviction LRU
(préserve le plus récent ET le modèle en cours de chargement), ModelCapacityError
si tout busy, compteur refcount ++/-- symétrique via hold/finally, et respect du
plafond de modèles chargés.

Exécution (harness stub — conftest mocke numpy/httpx/fastapi) ::
    python3 -m pytest client/tests/test_server_model_manager.py -q
"""

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import contrôlé de server/server.py (pattern identique à test_server_models.py)
# ---------------------------------------------------------------------------
_CACHE = tempfile.mkdtemp(prefix="talky-mm-test-")
os.environ["HF_HOME"] = _CACHE
os.environ["HF_HUB_CACHE"] = _CACHE


def _ensure_module(name: str, attrs: dict | None = None) -> types.ModuleType:
    """Module réel s'il est importable, sinon un stub minimal."""
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

# fastapi (mock conftest en env nu) : combler File/Form/HTTPException/UploadFile
# utilisés par server.py au niveau module.
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


@pytest.fixture()
def fake_whisper(monkeypatch):
    """Remplace faster_whisper.WhisperModel par une classe factice qui
    enregistre ses instanciations (repo + kwargs). Le chargement GPU n'a
    jamais lieu."""
    created = []

    class _FakeWhisper:
        def __init__(self, repo, **kwargs):
            created.append((repo, kwargs))

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _FakeWhisper
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
    return created


def _fresh(monkeypatch, max_models: int = 3) -> "server.ModelManager":
    """Un ModelManager isolé (jamais le global MODELS) au plafond donné."""
    mgr = server.ModelManager()
    mgr._max_models = max_models
    return mgr


# ---------------------------------------------------------------------------
# _env_int : défauts, invalide, plancher
# ---------------------------------------------------------------------------
def test_env_int_default(monkeypatch):
    monkeypatch.delenv("TALKY_TS_VAR", raising=False)
    assert server._env_int("TALKY_TS_VAR", 5, minimum=1) == 5


def test_env_int_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TALKY_TS_VAR", "pas_un_entier")
    assert server._env_int("TALKY_TS_VAR", 7, minimum=1) == 7


def test_env_int_clamped_to_minimum(monkeypatch):
    monkeypatch.setenv("TALKY_TS_VAR", "0")
    assert server._env_int("TALKY_TS_VAR", 5, minimum=1) == 1
    monkeypatch.setenv("TALKY_TS_VAR", "-3")
    assert server._env_int("TALKY_TS_VAR", 5, minimum=2) == 2


# ---------------------------------------------------------------------------
# get() : cache, touch LRU, hold (nop / incrément)
# ---------------------------------------------------------------------------
def test_get_returns_cached_and_touches_lru(monkeypatch):
    mgr = _fresh(monkeypatch)
    fake = object()
    key = mgr._key("modelA", "int8")
    mgr._models[key] = fake
    mgr._last_used[key] = 0.0
    obj = mgr.get("modelA", "int8")
    assert obj is fake
    assert mgr._last_used.get(key, -1) >= 0.0   # LRU touché (monotonic >= 0)


def test_get_nohold_does_not_increment_in_use(monkeypatch):
    mgr = _fresh(monkeypatch)
    key = mgr._key("modelA", "int8")
    mgr._models[key] = object()
    mgr._in_use[key] = 0
    before = dict(mgr._in_use)
    mgr.get("modelA", "int8")
    assert mgr._in_use == before                  # aucune prise sans hold


def test_get_hold_increments_in_use(monkeypatch):
    mgr = _fresh(monkeypatch)
    key = mgr._key("modelA", "int8")
    mgr._models[key] = object()
    mgr.get("modelA", "int8", hold=True)
    assert mgr._in_use.get(key, 0) == 1
    mgr.get("modelA", "int8", hold=True)
    assert mgr._in_use.get(key, 0) == 2           # refcount cumulable


# ---------------------------------------------------------------------------
# transcribe : refcount ++/-- symétrique via hold + finally
# ---------------------------------------------------------------------------
def test_transcribe_hold_refcount_symmetric(monkeypatch):
    mgr = _fresh(monkeypatch)

    class _Seg:
        def __init__(self, text):
            self.text = text

    class _Info:
        language = "fr"
        duration = 1.5

    observed = {}

    class _FakeModel:
        def transcribe(self, audio, **kw):
            observed["in_use_during"] = mgr._in_use.get(key, 0)
            return iter([_Seg("bonjour")]), _Info()

    key = mgr._key("modelA", "int8")
    mgr._models[key] = _FakeModel()

    text, info = mgr.transcribe("modelA", [0.0] * 80)
    assert text == "bonjour"
    assert info["language"] == "fr"
    assert info["duration"] == 1.5
    # Pendant l'inférence : 1 prise ; après le try/finally : compteur soldé.
    assert observed["in_use_during"] == 1
    assert key not in mgr._in_use


# ---------------------------------------------------------------------------
# Éviction LRU : préserve le plus récent + jamais le modèle en cours de chargement
# ---------------------------------------------------------------------------
def test_eviction_lru_preserves_most_recent_and_new(monkeypatch, fake_whisper):
    mgr = _fresh(monkeypatch, max_models=2)
    kA, kB = mgr._key("A", "int8"), mgr._key("B", "int8")
    mgr._models[kA] = object(); mgr._last_used[kA] = 1.0
    mgr._models[kB] = object(); mgr._last_used[kB] = 2.0   # plus récent

    kC = mgr._key("C", "int8")
    obj = mgr.get("C", "int8")

    assert kA not in mgr._models            # le moins récent (A) est évincé
    assert kB in mgr._models                # le plus récent (B) est préservé
    assert mgr._models[kC] is obj           # le nouveau modèle chargé survit
    assert set(mgr._models) == {kB, kC}
    assert len(fake_whisper) == 1           # un seul vrai « chargement »
    assert fake_whisper[0][0] == "Systran/faster-whisper-C"


def test_eviction_never_evicts_model_being_loaded(monkeypatch, fake_whisper):
    """Plafond plein : charger un nouveau modèle évince un autre, jamais le
    modèle en cours de chargement (new_key est exclu du choix LRU)."""
    mgr = _fresh(monkeypatch, max_models=1)
    kA = mgr._key("A", "int8")
    mgr._models[kA] = object(); mgr._last_used[kA] = 1.0

    kZ = mgr._key("Z", "int8")
    obj = mgr.get("Z", "int8")
    assert mgr._models[kZ] is obj           # le nouveau charge sans être évincé
    assert set(mgr._models) == {kZ}
    assert kA not in mgr._models


# ---------------------------------------------------------------------------
# Capacité : ModelCapacityError si tout est busy ; plafond jamais dépassé
# ---------------------------------------------------------------------------
def test_model_capacity_error_when_all_busy(monkeypatch):
    mgr = _fresh(monkeypatch, max_models=1)
    kA = mgr._key("A", "int8")
    mgr._models[kA] = object()
    mgr._last_used[kA] = 1.0
    mgr._in_use[kA] = 1                    # A est occupé (inférence en cours)
    with pytest.raises(server.ModelCapacityError):
        mgr.get("B", "int8")


def test_cap_respected_len_never_exceeds_max(monkeypatch, fake_whisper):
    mgr = _fresh(monkeypatch, max_models=3)
    for name in ("A", "B", "C", "D", "E"):
        mgr.get(name, "int8")
        assert len(mgr._models) <= 3        # plafond respecté à chaque étape
    # Cinq chargements demandés : toujours 3 modèles en mémoire (LRU), et
    # les derniers (les plus récents) sont présents.
    assert len(mgr._models) == 3
    for name in ("C", "D", "E"):
        assert mgr._key(name, "int8") in mgr._models
    assert mgr._key("A", "int8") not in mgr._models
    assert mgr._key("B", "int8") not in mgr._models
