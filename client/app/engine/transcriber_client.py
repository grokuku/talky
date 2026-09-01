# -*- coding: utf-8 -*-
"""
app/engine/transcriber_client.py
================================
Client HTTP vers le serveur de transcription whisper-live (hwdsl2/whisper-live-
server, API OpenAI-compatible, POST /v1/audio/transcriptions — §3 roadmap.md).

L'audio (numpy float32, 16 kHz mono) est encodé en WAV PCM16 en mémoire
(wave + io.BytesIO, aucun fichier temporaire) puis envoyé en multipart.
La réponse verbose_json est parsée en TranscriptionResult ; chaque erreur
réseau / statut HTTP / JSON inattendu est traduite en TranscriptionError
avec un message en français (§3.4).

Exemple ::

    result = transcribe(audio, config)          # -> TranscriptionResult | None
    if result is None:
        print("Aucun texte détecté")
"""

import io
import struct
import wave
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np

from app.core.constants import SAMPLING_RATE

# Conversion vectorisée disponible ? numpy réel oui ; le mock du conftest de
# test n'expose ni asarray, ni clip, ni rint -> repli sur la boucle Python
# (octets strictement identiques, cf. tests de non-régression).
_VECTOR_PCM16 = (hasattr(np, "asarray") and hasattr(np, "clip")
                 and hasattr(np, "rint"))


def _pcm16_from_float(audio) -> bytes:
    """float ~[-1, 1] -> PCM16 int16 petit-boutiste (brut, sans en-tête).

    Chemin vectorisé numpy : clip [-1, 1], *32767 (précision float64 —
    identique à l'ancien float(sample) * 32767), arrondi au plus proche
    (np.rint, même arrondi bancaire que round()), astype('<i2') -> tobytes().
    Le mode boucle Python (numpy absent/mocké) reproduit l'ancienne
    implémentation octet pour octet.
    """
    if _VECTOR_PCM16:
        samples = np.asarray(audio, dtype=np.float64).reshape(-1)
        # Emulation de l'ancienne boucle : min(1.0, nan) == 1.0 (toute
        # comparaison avec NaN est False), donc un NaN était clipé à 1.0.
        samples = np.where(np.isnan(samples), 1.0, samples)
        np.clip(samples, -1.0, 1.0, out=samples)
        samples *= 32767.0
        return np.rint(samples).astype("<i2").tobytes()
    # Repli (numpy mocké, env de test) : ancienne implémentation référence.
    flat = audio.flatten() if hasattr(audio, "flatten") else audio
    out = bytearray()
    for sample in flat:
        value = max(-1.0, min(1.0, float(sample)))
        out += struct.pack("<h", int(round(value * 32767)))
    return bytes(out)


# ---------------------------------------------------------------------------
# Encodage WAV en mémoire
# ---------------------------------------------------------------------------
def encode_wav(audio: np.ndarray) -> bytes:
    """Encode un tableau float32 (16 kHz mono, valeurs ~[-1, 1]) en WAV PCM16.

    Écriture en mémoire via `wave` + `io.BytesIO` (aucun fichier temporaire).
    Conversion float32 -> int16 (vectorisée, cf. _pcm16_from_float) : clip
    [-1, 1] puis * 32767 (arrondi au plus proche, compatibilité avec le
    comportement de np.ndarray.astype(np.int16)).
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)                       # PCM16 = 2 octets
        wav.setframerate(SAMPLING_RATE)
        wav.writeframes(_pcm16_from_float(audio))
    return buf.getvalue()

DEFAULT_MODEL = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"

# Modèles connus supportés par whisper-live (faster-whisper). whisper-live
# n'expose AUCUN endpoint de liste de modèles (ni /v1/models ni /health) :
# cette liste LOCALE sert de référence côté client (routes /api/server/* et
# dropdown du frontend). Ordre : du plus précis au plus léger.
# Liste en repo IDs HuggingFace complets (pas d'alias).
WHISPERLIVE_MODELS = [
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "Systran/faster-whisper-large-v3",
    "Systran/faster-whisper-medium",
    "Systran/faster-whisper-small",
    "Systran/faster-whisper-base",
    "Systran/faster-whisper-tiny",
]

# ---------------------------------------------------------------------------
# Types & messages d'erreur (§3.4 du roadmap.md)
# ---------------------------------------------------------------------------
_ERR_CONNECT = "Serveur injoignable — vérifier server_url"
_ERR_READ_TIMEOUT = "Le serveur a mis trop de temps à répondre"
_ERR_AUTH = "Authentification refusée (API key)"
_ERR_NOT_FOUND = "Endpoint introuvable — vérifier server_url"
_ERR_INVALID = "Requête invalide (modèle ou langue)"
_ERR_TOO_LARGE = "Fichier audio trop volumineux"
_ERR_BUSY = ("Serveur occupé — tous les modèles sont en cours d'utilisation, "
            "réessaie dans un instant")
_ERR_SERVER = "Erreur serveur (GPU) — réessayer"
_ERR_UNEXPECTED = "Réponse serveur inattendue"


class TranscriptionError(Exception):
    """Erreur de transcription typée (message utilisateur en français)."""


@dataclass
class TranscriptionResult:
    """Résultat d'une transcription réussie (verbose_json)."""
    text: str
    language: Optional[str]
    duration: Optional[float]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_server_url(server_url: str) -> str:
    """Retire le ou les slash(s) de fin : http://h:8000/ -> http://h:8000."""
    return str(server_url or "").strip().rstrip("/")


def _language_param(language: object) -> Optional[str]:
    """None / \"\" / \"auto\" -> None (auto-détection côté serveur)."""
    if language in (None, "", "auto"):
        return None
    return str(language)


def _auth_headers(api_key: str) -> dict:
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _detail_from_response(response, maxlen: int = 200) -> str:
    """Extrait le champ ``detail`` d'une réponse d'erreur FastAPI/starlette.

    Le serveur talky (FastAPI) renvoie ses erreurs en JSON
    ``{"detail": "<message>"}`` ; une erreur de validation pydantic est
    renvoyée en liste ``[{"detail": {...}, "loc": [...]}, ...]``. Cette
    aide récupère le message réel (tronqué à ``maxlen`` caractères) pour le
    rendre visible à l'utilisateur au lieu de cacher la cause derrière un
    message générique. Retourne ``""`` si la réponse n'apporte rien
    d'exploitable (body non-JSON, détail absent ou vide).
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 — body non-JSON / inaccessible
        return ""

    def _fmt(detail) -> str:
        if isinstance(detail, str):
            return detail.strip()
        if isinstance(detail, dict):
            # Erreur de validation pydantic : message = premier champ texte
            # exploitable ("msg", "message", ...).
            for key in ("msg", "message", "error", "detail", "type"):
                val = detail.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""

    if isinstance(payload, dict):
        return _fmt(payload.get("detail"))[:maxlen]
    if isinstance(payload, list):
        # Validation pydantic : liste d'erreurs {"loc", "msg", "type"}. On
        # prend le premier message texte exploitable (via _fmt).
        for item in payload:
            txt = _fmt(item)
            if txt:
                return txt[:maxlen]
    return ""


def _detail_suffix(response) -> str:
    """Suffixe à coller à un message d'erreur FR : ``" : <détail serveur>"``
    quand le serveur fournit un ``detail`` JSON, sinon ``""``."""
    detail = _detail_from_response(response)
    return f" : {detail}" if detail else ""


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def transcribe(audio: np.ndarray, config: dict,
               transport: object = None) -> Optional[TranscriptionResult]:
    """Transcrit l'audio sur le serveur whisper-live (§3.1/§3.2).

    `transport` : seam de test (httpx.MockTransport). None en production ->
    requête réseau réelle via httpx.post. Retourne None si aucun texte n'a
    été détecté ; lève TranscriptionError (message FR) sinon (§3.4).
    """
    server_url = _normalize_server_url(config.get("server_url", ""))
    if not server_url:
        raise TranscriptionError(_ERR_CONNECT)

    api_key = str(config.get("server_api_key") or "")
    server_timeout = float(config.get("server_timeout", 30))
    model = config.get("model") or DEFAULT_MODEL
    language = _language_param(config.get("language"))
    vad_filter = config.get("vad_filter", True)
    compute_type = config.get("compute_type", "int8")

    # Multipart OpenAI-compatible (whisper-live) : model, language,
    # vad_filter, response_format=verbose_json. Les paramètres `task` et
    # `temperature` ne sont PAS OpenAI-compatible côté whisper-live : on ne
    # les envoie plus (le serveur rejetterait la requête).
    files = {"file": ("audio.wav", encode_wav(audio), "audio/wav")}
    data = {
        "model": model,
        "vad_filter": "true" if vad_filter else "false",
        "response_format": "verbose_json",
        "compute_type": compute_type,
    }
    if language is not None:
        data["language"] = language

    url = f"{server_url}/v1/audio/transcriptions"
    timeout = httpx.Timeout(connect=5, read=server_timeout, write=10, pool=5)

    post_kwargs: dict = {}
    if transport is not None:
        post_kwargs["transport"] = transport

    try:
        response = httpx.post(url, files=files, data=data,
                              headers=_auth_headers(api_key),
                              timeout=timeout, **post_kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise TranscriptionError(_ERR_CONNECT) from exc
    except httpx.ReadTimeout as exc:
        raise TranscriptionError(_ERR_READ_TIMEOUT) from exc
    except Exception as exc:  # noqa: BLE001 — toute autre erreur réseau
        raise TranscriptionError(_ERR_CONNECT) from exc

    # --- Mapping statut HTTP (§3.4) ---
    if response.status_code in (401, 403):
        raise TranscriptionError(_ERR_AUTH)
    if response.status_code == 404:
        raise TranscriptionError(_ERR_NOT_FOUND)
    if response.status_code == 422:
        raise TranscriptionError(_ERR_INVALID)
    if response.status_code == 413:
        raise TranscriptionError(_ERR_TOO_LARGE)
    if response.status_code == 503:
        raise TranscriptionError(_ERR_BUSY)
    if response.status_code >= 500:
        # 5xx générique : on y accole le ``detail`` du serveur (ex. cause
        # d'échec de snapshot_download) pour la rendre visible à l'utilisateur.
        raise TranscriptionError(_ERR_SERVER + _detail_suffix(response))
    if response.status_code != 200:
        raise TranscriptionError(_ERR_UNEXPECTED)

    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — JSON invalide / inattendu
        raise TranscriptionError(_ERR_UNEXPECTED) from exc

    if not isinstance(payload, dict):
        raise TranscriptionError(_ERR_UNEXPECTED)

    text = (payload.get("text") or "").strip()
    if not text:
        return None
    return TranscriptionResult(
        text=text,
        language=payload.get("language"),
        duration=payload.get("duration"),
    )


# ---------------------------------------------------------------------------
# Endpoints utilitaires (§3.5)
# ---------------------------------------------------------------------------
def ping(server_url: str, api_key: str = "", timeout: float = 5.0,
         transport: object = None) -> dict:
    """Sonde le serveur talky — ne lève JAMAIS d'exception.

    Le serveur talky (conteneur maison, FastAPI) expose désormais GET
    {server_url}/health (sonde sans secret, exemptée d'auth), mais ping()
    continue de sonder GET {server_url}/docs (Swagger UI FastAPI) puis — si
    /docs répond 404/405 — GET {server_url}/openapi.json, dans les deux cas
    avec la clé API (header Authorization). Si les deux échouent, le serveur
    est considéré injoignable/incompatible.

    Retourne {"reachable": True, "status": ...} ou
    {"reachable": False, "error": ...}.
    """
    server_url = _normalize_server_url(server_url)
    get_kwargs: dict = {}
    if transport is not None:
        get_kwargs["transport"] = transport

    for path in ("/docs", "/openapi.json"):
        try:
            response = httpx.get(f"{server_url}{path}",
                                 headers=_auth_headers(api_key),
                                 timeout=timeout, **get_kwargs)
            if response.status_code < 400:
                return {"reachable": True, "status": response.status_code}
            # 404/405 sur /docs : UI Swagger absente → repli /openapi.json.
            if path == "/docs" and response.status_code in (404, 405):
                continue
            return {"reachable": False, "error": f"HTTP {response.status_code}"}
        except Exception as exc:  # noqa: BLE001 — contrat §3.5 : jamais d'exception
            return {"reachable": False, "error": str(exc)}
    return {"reachable": False, "error": "Serveur injoignable"}


def list_models(server_url: str = "", api_key: str = "", timeout: float = 5.0,
                transport: object = None) -> list:
    """Liste les modèles du serveur talky (GET /v1/models).

    Interroge l'endpoint /v1/models du serveur (modèles installés dans le
    cache local). En cas d'échec (serveur injoignable, endpoint absent,
    liste vide), repli sur la liste LOCALE WHISPERLIVE_MODELS (toujours
    disponible). Les paramètres réseau sont conservés pour compatibilité.
    """
    server_url = _normalize_server_url(server_url)
    get_kwargs: dict = {}
    if transport is not None:
        get_kwargs["transport"] = transport
    if server_url:
        try:
            response = httpx.get(f"{server_url}/v1/models",
                                 headers=_auth_headers(api_key),
                                 timeout=timeout, **get_kwargs)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list) and data:
                    return [str(x) for x in data]
        except Exception:  # noqa: BLE001 — repli local silencieux
            pass
    return list(WHISPERLIVE_MODELS)


# ---------------------------------------------------------------------------
# Gestion de modèles (installation depuis le client — serveur talky)
# ---------------------------------------------------------------------------
def list_registry(server_url: str = "", api_key: str = "",
                  timeout: float = 10.0, transport: object = None,
                  task: str = "automatic-speech-recognition") -> list:
    """Liste les modèles disponibles à l'installation (GET /v1/registry).

    Le serveur talky renvoie une liste de dicts (id, name, params,
    vram_int8, repo) ou, pour compatibilité, une liste de strings.
    La liste est retournée telle quelle (le frontend gère le format dict).
    En cas d'échec, retourne [] (jamais d'exception).
    """
    server_url = _normalize_server_url(server_url)
    if not server_url:
        return []
    get_kwargs: dict = {}
    if transport is not None:
        get_kwargs["transport"] = transport
    try:
        response = httpx.get(f"{server_url}/v1/registry",
                             params={"task": task},
                             headers=_auth_headers(api_key),
                             timeout=timeout, **get_kwargs)
        if response.status_code != 200:
            return []
        data = response.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("models"), list):
            return data["models"]
        return []
    except Exception:  # noqa: BLE001 — jamais d'exception
        return []


def download_model(server_url: str = "", api_key: str = "", model: str = "",
                   timeout: float = 600.0, transport: object = None) -> dict:
    """Installe un modèle sur le serveur (POST /v1/models, body JSON).

    Le repo ID (ex. "Systran/faster-whisper-medium") est envoyé dans le
    corps JSON plutôt que dans l'URL : les '/' des repo IDs HuggingFace
    seraient encodés en %2F dans le chemin, ce que FastAPI ne matche pas
    dans un paramètre de route (404). Peut prendre plusieurs minutes
    (téléchargement HuggingFace). Lève TranscriptionError (message FR) en
    cas d'échec ; retourne le dict de confirmation du serveur sinon.
    """
    server_url = _normalize_server_url(server_url)
    model = (model or "").strip()
    if not server_url:
        raise TranscriptionError(_ERR_CONNECT)
    if not model:
        raise TranscriptionError(_ERR_INVALID)
    url = f"{server_url}/v1/models"
    post_kwargs: dict = {}
    if transport is not None:
        post_kwargs["transport"] = transport
    try:
        response = httpx.post(url, json={"model": model},
                              headers=_auth_headers(api_key),
                              timeout=timeout, **post_kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise TranscriptionError(_ERR_CONNECT) from exc
    except httpx.ReadTimeout as exc:
        raise TranscriptionError(_ERR_READ_TIMEOUT) from exc
    except Exception as exc:  # noqa: BLE001
        raise TranscriptionError(_ERR_CONNECT) from exc
    if response.status_code in (401, 403):
        raise TranscriptionError(_ERR_AUTH + _detail_suffix(response))
    if response.status_code == 404:
        raise TranscriptionError(_ERR_NOT_FOUND + _detail_suffix(response))
    if response.status_code >= 500:
        # 5xx : accole le ``detail`` du serveur (cause réelle de l'échec de
        # snapshot_download, ex. "Repository not found", "GatedRepoError",
        # "[Errno 28] No space left on device", quota HF 4xx...) pour que
        # l'utilisateur voie la VRAIE raison au lieu du message générique.
        raise TranscriptionError(_ERR_SERVER + _detail_suffix(response))
    if response.status_code != 200:
        raise TranscriptionError(_ERR_UNEXPECTED + _detail_suffix(response))
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — JSON invalide
        raise TranscriptionError(_ERR_UNEXPECTED) from None
