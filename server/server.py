# -*- coding: utf-8 -*-
"""Talky — serveur de transcription maison (faster-whisper).

Remplace l'image hwdsl2/whisper-live-server (limites : compute_type non
configurable, tag rolling). Conteneur 100 % maîtrisé :

* REST OpenAI-compatible : POST /v1/audio/transcriptions (FastAPI)
* WebSocket temps réel     : ws://host:9090/ (protocole compatible avec le
                             client talky : handshake JSON, PCM16 int16,
                             messages {"message": "server_ready"|"transcript",
                             "segments": [...]}, fin par {"eof": true})
* Gestion de modèles       : GET /v1/models (installés), GET /v1/registry
                             (disponibles dans la registry HuggingFace),
                             POST /v1/models (téléchargement, body JSON)
* Précision                : INT8 natif (faster-whisper compute_type="int8")
* VAD                      : Silero VAD (segmentation de la parole en direct)

Un seul port interne (9090) sert REST + WS ; le docker-compose mappe les
deux ports hôtes (8000 REST / 9090 WS) vers ce port unique.
"""

import json
import logging
import os
import secrets
import threading
import wave
from typing import Optional

import anyio
import numpy as np
import uvicorn
from fastapi import (APIRouter, FastAPI, File, Form, HTTPException, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import JSONResponse
from huggingface_hub import snapshot_download
from pydantic import BaseModel

log = logging.getLogger("talky-server")
logging.basicConfig(
    level=os.environ.get("TALKY_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_MODEL = os.environ.get(
    "TALKY_MODEL", "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
LANGUAGE = os.environ.get("TALKY_LANGUAGE") or None      # None = auto
WS_PORT = int(os.environ.get("TALKY_WS_PORT", "9090"))
COMPUTE_TYPE = os.environ.get("TALKY_COMPUTE_TYPE", "int8")
CACHE_DIR = os.environ.get("HF_HOME", "/var/lib/whisper-live")
VAD_THRESHOLD = float(os.environ.get("TALKY_VAD_THRESHOLD", "0.5"))
VAD_SILENCE_MS = int(os.environ.get("TALKY_VAD_SILENCE_MS", "500"))
MAX_CLIENTS = int(os.environ.get("TALKY_MAX_CLIENTS", "4"))
# Clé API optionnelle : si non vide, exige `Authorization: Bearer <clé>` sur
# REST et dans le handshake WebSocket. Vide = aucune authentification (LAN).
API_KEY = os.environ.get("TALKY_API_KEY", "").strip()
# Chemins REST exemptés d'authentification (healthcheck Docker sans secret).
EXEMPT_PATHS = {"/health"}
SR = 16000

os.environ.setdefault("HF_HOME", CACHE_DIR)
os.environ.setdefault("HF_HUB_CACHE", CACHE_DIR)
os.makedirs(CACHE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Modèles : repo IDs HuggingFace complets (+ fallback alias historique)
# ---------------------------------------------------------------------------
# Les alias (turbo, large-v3, medium, etc.) sont un héritage de
# speaches/whisper-live.  Talky utilise désormais les repo IDs complets
# partout.  La table _ALIAS_TO_REPO est conservée UNIQUEMENT comme fallback
# silencieux : si un ancien config.json contient encore un alias, resolve_repo()
# le traduit en repo ID complet pour ne pas casser l'existant.
_ALIAS_TO_REPO = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# Liste des repo IDs complets connus (ordonnée du plus précis au plus léger).
KNOWN_MODELS = [
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "Systran/faster-whisper-large-v3",
    "Systran/faster-whisper-distil-large-v3",
    "Systran/faster-whisper-medium",
    "Systran/faster-whisper-medium.en",
    "Systran/faster-whisper-small",
    "Systran/faster-whisper-small.en",
    "Systran/faster-whisper-base",
    "Systran/faster-whisper-base.en",
    "Systran/faster-whisper-tiny",
    "Systran/faster-whisper-tiny.en",
]


def resolve_repo(model: str) -> str:
    """Résout un identifiant de modèle en repo ID HuggingFace complet.

    * Si l'identifiant contient un '/', c'est déjà un repo ID complet : on
      le retourne tel quel.
    * Si c'est un alias historique connu (ex. ``"turbo"``), on le traduit
      via ``_ALIAS_TO_REPO`` (fallback silencieux pour compat config.json).
    * Sinon, on tente ``Systran/faster-whisper-{model}`` (ce qui couvre les
      alias non listés comme ``"large-v2"``) ; si cela ne correspond à rien
      de connu, on retourne la valeur telle quelle (ça pourrait être un
      repo ID valide d'une autre organisation).
    """
    model = (model or "").strip()
    if "/" in model:
        return model
    if model in _ALIAS_TO_REPO:
        return _ALIAS_TO_REPO[model]
    guess = f"Systran/faster-whisper-{model}"
    return guess


# ---------------------------------------------------------------------------
# Registry : modèles faster-whisper connus (liste statique curatée)
# ---------------------------------------------------------------------------
# Au lieu d'interroger HuggingFace à chaque appel (peu fiable hors-ligne),
# on retourne une liste statique connue avec métadonnées (taille, VRAM).
# L'ID est désormais le repo ID HuggingFace complet (utilisé pour
# l'installation : POST /v1/models avec body JSON {"model": ...}).  Le
# champ "name" reste un nom d'affichage plus lisible.
REGISTRY_MODELS = [
    {"id": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
     "name": "Large v3 Turbo",
     "params": "809M", "vram_int8": "~0.8 Go",
     "repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"},
    {"id": "Systran/faster-whisper-large-v3", "name": "Large v3",
     "params": "1.5B", "vram_int8": "~1.5 Go",
     "repo": "Systran/faster-whisper-large-v3"},
    {"id": "Systran/faster-whisper-distil-large-v3", "name": "Distil Large v3",
     "params": "756M", "vram_int8": "~0.8 Go",
     "repo": "Systran/faster-whisper-distil-large-v3"},
    {"id": "Systran/faster-whisper-medium", "name": "Medium",
     "params": "769M", "vram_int8": "~0.8 Go",
     "repo": "Systran/faster-whisper-medium"},
    {"id": "Systran/faster-whisper-medium.en", "name": "Medium (English only)",
     "params": "769M", "vram_int8": "~0.8 Go",
     "repo": "Systran/faster-whisper-medium.en"},
    {"id": "Systran/faster-whisper-small", "name": "Small",
     "params": "244M", "vram_int8": "~0.3 Go",
     "repo": "Systran/faster-whisper-small"},
    {"id": "Systran/faster-whisper-small.en", "name": "Small (English only)",
     "params": "244M", "vram_int8": "~0.3 Go",
     "repo": "Systran/faster-whisper-small.en"},
    {"id": "Systran/faster-whisper-base", "name": "Base",
     "params": "74M", "vram_int8": "~0.1 Go",
     "repo": "Systran/faster-whisper-base"},
    {"id": "Systran/faster-whisper-base.en", "name": "Base (English only)",
     "params": "74M", "vram_int8": "~0.1 Go",
     "repo": "Systran/faster-whisper-base.en"},
    {"id": "Systran/faster-whisper-tiny", "name": "Tiny",
     "params": "39M", "vram_int8": "~0.05 Go",
     "repo": "Systran/faster-whisper-tiny"},
    {"id": "Systran/faster-whisper-tiny.en", "name": "Tiny (English only)",
     "params": "39M", "vram_int8": "~0.05 Go",
     "repo": "Systran/faster-whisper-tiny.en"},
]


# ---------------------------------------------------------------------------
# Gestionnaire de modèles (chargement paresseux, partagé, thread-safe)
# ---------------------------------------------------------------------------
class ModelManager:
    """Charge les modèles faster-whisper à la demande (INT8) et les garde en
    mémoire. Un verrou global sérialise les inférences (le modèle CTranslate2
    est partagé entre les sessions WebSocket et les requêtes batch)."""

    def __init__(self) -> None:
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()

    def get(self, model: str, compute_type: str = COMPUTE_TYPE) -> object:
        repo = resolve_repo(model)
        key = f"{repo}:{compute_type}"
        with self._lock:
            if key not in self._models:
                log.info("Chargement du modèle %s (repo %s, %s)…",
                         model, repo, compute_type)
                from faster_whisper import WhisperModel  # import paresseux
                self._models[key] = WhisperModel(
                    repo, device="cuda", compute_type=compute_type)
                log.info("Modèle %s chargé (compute_type=%s).", repo, compute_type)
            return self._models[key]

    def transcribe(self, model: str, audio: np.ndarray,
                   language: Optional[str] = None,
                   compute_type: str = COMPUTE_TYPE) -> tuple[str, dict]:
        """Transcrit `audio` (float32 16 kHz mono). Retourne (text, info)."""
        m = self.get(model, compute_type=compute_type)
        lang = language or LANGUAGE
        with self._infer_lock:
            segments, info = m.transcribe(
                audio, language=lang, beam_size=5,
                vad_filter=False,                 # VAD déjà appliqué en amont
                condition_on_previous_text=False)
            text = "".join(s.text for s in segments).strip()
        return text, {"language": info.language, "duration": info.duration}

    def installed(self) -> list[str]:
        """Liste les modèles présents dans le cache local (repo IDs complets)."""
        names = []
        if os.path.isdir(CACHE_DIR):
            for entry in sorted(os.listdir(CACHE_DIR)):
                if entry.startswith("models--"):
                    repo = entry[len("models--"):].replace("--", "/")
                    if "/" in repo:
                        names.append(repo)
        return names or list(KNOWN_MODELS[:3])


MODELS = ModelManager()

# ---------------------------------------------------------------------------
# VAD (Silero) — segmentation de la parole en direct
# ---------------------------------------------------------------------------
class StreamingVAD:
    """Segmente un flux audio en segments de parole (Silero VAD).

    ``feed()`` consomme l'audio par fenêtres de 512 échantillons (≈32 ms à
    16 kHz) et retourne les segments de parole finalisés (index [start, end)
    dans le flux global) quand un silence d'au moins ``silence_ms`` suit la
    parole. Utilise le modèle Silero VAD (probabilité de parole par fenêtre).

    Le seuil par défaut (0.5) correspond à la probabilité Silero. La variable
    d'environnement ``TALKY_VAD_THRESHOLD`` permet de l'ajuster.
    """

    def __init__(self, threshold: float = VAD_THRESHOLD,
                 silence_ms: int = VAD_SILENCE_MS) -> None:
        import torch
        from silero_vad import load_silero_vad
        self._torch = torch
        self._vad = load_silero_vad()
        self._threshold = threshold
        self._window = 512                          # ≈32 ms à 16 kHz (Silero)
        self._silence_frames = max(3, int(silence_ms * SR / 1000 / self._window))
        self._min_speech_frames = 3                 # ≈96 ms de parole
        self._pending = np.zeros(0, dtype=np.float32)
        self._global_idx = 0
        self._in_speech = False
        self._speech_start = 0
        self._silence_run = 0
        self._speech_frames_seen = 0

    def feed(self, chunk: np.ndarray) -> list[tuple[int, int]]:
        self._pending = np.concatenate([self._pending, chunk.astype(np.float32)])
        finalized: list[tuple[int, int]] = []
        n_win = len(self._pending) // self._window
        if n_win:
            block = self._pending[:n_win * self._window].reshape(
                n_win, self._window)
            self._pending = self._pending[n_win * self._window:]
            for i in range(n_win):
                self._global_idx += self._window
                prob = self._vad(
                    self._torch.from_numpy(block[i]), SR).item()
                if prob >= self._threshold:
                    self._silence_run = 0
                    if not self._in_speech:
                        self._speech_frames_seen += 1
                        if self._speech_frames_seen >= self._min_speech_frames:
                            self._in_speech = True
                            # Pre-roll : inclure ~160 ms d'audio avant la
                            # détection (5 fenêtres × 512 = 2560 samples) pour
                            # ne pas couper le début de la parole. Le VAD détecte
                            # le speech_start trop tard (après min_speech_frames
                            # de parole confirmée) ; ce padding remonte le
                            # point de départ pour capturer l'attaque.
                            pre_roll = self._window * 5
                            self._speech_start = max(
                                0, self._global_idx - self._window - pre_roll)
                    # en parole : rien à faire
                else:
                    self._speech_frames_seen = 0
                    if self._in_speech:
                        self._silence_run += 1
                        if self._silence_run >= self._silence_frames:
                            finalized.append(
                                (self._speech_start, self._global_idx))
                            self._in_speech = False
                            self._silence_run = 0
        return finalized

    def flush(self) -> Optional[tuple[int, int]]:
        """Segment de parole en cours (pour l'EOF), ou None."""
        if self._in_speech:
            seg = (self._speech_start, self._global_idx)
            self._in_speech = False
            self._silence_run = 0
            return seg
        return None


# ---------------------------------------------------------------------------
# Décodeur WAV (PCM16 16 kHz mono, comme envoyé par le client)
# ---------------------------------------------------------------------------
def decode_wav(data: bytes) -> np.ndarray:
    """Décode un WAV (PCM16 stéréo/mono, 8/16/24/32 bits) -> float32 16 kHz."""
    with wave.open(io_bytes(data), "rb") as wav:
        nch, sw, sr, nframes = (wav.getnchannels(), wav.getsampwidth(),
                                wav.getframerate(), wav.getnframes())
        raw = wav.readframes(nframes)
    if sr != SR:
        raise HTTPException(422, f"Fréquence non supportée : {sr} Hz (attendu {SR})")
    if sw == 3:  # 24-bit empaqueté en 3 octets : frombuffer ne marche pas
        samples = _decode_24bit(raw)
    else:
        dtype = {1: np.int8, 2: np.int16, 4: np.int32}[sw]
        scale = {1: 128.0, 2: 32768.0, 4: 2 ** 31}[sw]
        samples = np.frombuffer(raw, dtype=dtype).astype(np.float32) / scale
    if nch > 1:
        samples = samples[::nch]  # garde le premier canal
    return samples


def io_bytes(data: bytes):
    import io
    return io.BytesIO(data)


def _decode_24bit(raw: bytes) -> np.ndarray:
    n = len(raw) // 3
    out = np.empty(n, dtype=np.float32)
    for i in range(n):
        b = raw[i * 3:(i + 1) * 3]
        v = b[0] | (b[1] << 8) | (b[2] << 16)
        if v & 0x800000:
            v -= 0x1000000
        out[i] = v / 8388608.0
    return out


# ---------------------------------------------------------------------------
# Application FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="Talky — serveur de transcription", version="1.0.0")
router = APIRouter()

# Sessions WebSocket actives (pour TALKY_MAX_CLIENTS).
_active_ws: set[WebSocket] = set()


if API_KEY:
    @app.middleware("http")
    async def _require_api_key(request, call_next):
        """Exige `Authorization: Bearer <clé>` sur toutes les routes REST."""
        if request.url.path in EXEMPT_PATHS:   # healthcheck Docker sans secret
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {API_KEY}".encode("utf-8")
        if not secrets.compare_digest(auth.encode("utf-8"), expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


@router.get("/health")
async def health() -> dict:
    """Sonde de santé (ultra-légère : aucun accès modèle ni inférence)."""
    return {"status": "ok"}


@router.get("/v1/models")
async def list_installed() -> list[str]:
    """Modèles présents dans le cache local (utilisables)."""
    return MODELS.installed()


@router.get("/v1/registry")
async def registry(task: str = "automatic-speech-recognition") -> dict:
    """Modèles disponibles à l'installation.

    Interroge HuggingFace pour renvoyer tous les modèles faster-whisper /
    whisper CT2 disponibles, en plus de la liste curatée ``REGISTRY_MODELS``
    (qui contient des métadonnées détaillées). Si HuggingFace est injoignable,
    seul la liste curatée est retournée (fallback).

    Le paramètre ``task`` est conservé pour compatibilité (ignoré).
    """
    # 1. Liste curatée de base (avec détails : name, params, vram_int8)
    combined: dict[str, dict] = {}
    for entry in REGISTRY_MODELS:
        combined[entry["repo"]] = dict(entry)

    # 2. Recherche HuggingFace pour compléter avec plus de modèles
    try:
        from huggingface_hub import HfApi
        api = HfApi()

        seen_ids: set[str] = set(combined.keys())

        def hf_search(term: str, max_models: int) -> list:
            # list() matérialise la pagination réseau dans le thread appelant
            # (list_models retourne un générateur paresseux).
            return list(api.list_models(search=term, sort="downloads",
                                        limit=max_models))

        # Recherche "faster-whisper" (limit=100, tri par popularité)
        # (appel réseau HF offloadé en thread : ne doit pas bloquer l'event loop)
        for m in await anyio.to_thread.run_sync(hf_search,
                                                "faster-whisper", 100):
            repo_id = m.id
            if repo_id not in seen_ids:
                seen_ids.add(repo_id)
                combined[repo_id] = {
                    "id": repo_id, "name": repo_id,
                    "params": "", "vram_int8": "", "repo": repo_id}

        # Recherche "whisper" (limit=50) — filtrée pour ne garder que les
        # modèles qui semblent être des conversions CT2 / faster-whisper
        # (contiennent "faster-whisper" ou "whisper" dans l'ID).
        # (appel réseau HF offloadé en thread : ne doit pas bloquer l'event loop)
        for m in await anyio.to_thread.run_sync(hf_search, "whisper", 50):
            repo_id = m.id
            rid_lower = repo_id.lower()
            if repo_id in seen_ids:
                continue
            if "faster-whisper" in rid_lower or "whisper" in rid_lower:
                seen_ids.add(repo_id)
                combined[repo_id] = {
                    "id": repo_id, "name": repo_id,
                    "params": "", "vram_int8": "", "repo": repo_id}

    except Exception as exc:  # noqa: BLE001
        log.warning("HuggingFace injoignable, fallback liste curatée : %s",
                    exc)

    return {"task": task, "models": list(combined.values())}


class DownloadModelRequest(BaseModel):
    model: str


@router.post("/v1/models")
async def download_model(body: DownloadModelRequest) -> dict:
    """Télécharge (installe) un modèle dans le cache local.

    Le repo ID est passé dans le corps JSON (``{"model": "..."}``) plutôt
    que dans le chemin : les repo IDs HuggingFace contiennent des '/' qui,
    encodés en %2F dans l'URL, ne sont pas matché par FastAPI dans un
    paramètre de route (404). Peut prendre plusieurs minutes pour les gros
    modèles (le client utilise un timeout long). Retourne l'ID installé et
    le chemin du cache.
    """
    model_id = (body.model or "").strip()
    repo = resolve_repo(model_id)
    log.info("Téléchargement du modèle %s (repo %s)…", model_id, repo)
    try:
        path = await anyio.to_thread.run_sync(snapshot_download, repo_id=repo)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Échec téléchargement {repo} : {exc}") from exc
    log.info("Modèle %s installé.", model_id)
    return {"model": model_id, "repo": repo, "status": "downloaded",
            "cache": str(path)}


@router.post("/v1/audio/transcriptions")
async def transcribe_batch(
        file: UploadFile = File(...),
        model: str = Form(DEFAULT_MODEL),
        language: Optional[str] = Form(None),
        vad_filter: str = Form("true"),
        compute_type: str = Form(COMPUTE_TYPE),
        response_format: str = Form("json")) -> JSONResponse:
    """Transcription batch OpenAI-compatible (verbose_json supporté)."""
    try:
        data = await file.read()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "Fichier illisible") from exc
    audio = decode_wav(data)
    try:
        text, info = await anyio.to_thread.run_sync(
            MODELS.transcribe, model, audio, language, compute_type)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erreur transcription batch")
        raise HTTPException(500, f"Erreur GPU : {exc}") from exc

    if response_format == "verbose_json":
        return JSONResponse({
            "text": text, "language": info["language"],
            "duration": info["duration"]})
    return JSONResponse({"text": text})


@router.websocket("/")
async def realtime_endpoint(ws: WebSocket) -> None:
    """WebSocket temps réel (protocole compatible client talky).

    Handshake JSON : {"uid", "model", "task", "use_vad", "language",
    "same_output_threshold"} puis frames binaires PCM16 int16 16 kHz.
    Réponses : {"message": "server_ready"} puis {"message": "transcript",
    "segments": [{"start", "end", "text"}]}. Fin : {"eof": true} ou fermeture.
    """
    await ws.accept()
    # --- Authentification optionnelle (TALKY_API_KEY) -----------------------
    if API_KEY:
        auth = ws.headers.get("authorization", "")
        expected = f"Bearer {API_KEY}".encode("utf-8")
        if not secrets.compare_digest(auth.encode("utf-8"), expected):
            await ws.close(code=1008)
            return
    # --- Limite de sessions simultanées (TALKY_MAX_CLIENTS) -----------------
    if len(_active_ws) >= MAX_CLIENTS:
        await ws.send_text(json.dumps(
            {"message": "error", "reason": "busy"}))
        await ws.close()
        return
    _active_ws.add(ws)
    try:
        # --- Handshake ---------------------------------------------------------
        try:
            raw = await ws.receive_text()
            hb = json.loads(raw)
        except Exception:  # noqa: BLE001
            await ws.close(code=1008)
            return
        uid = str(hb.get("uid", "anon"))
        model = str(hb.get("model") or DEFAULT_MODEL)
        language = hb.get("language") or None
        use_vad = bool(hb.get("use_vad", True))
        compute_type = str(hb.get("compute_type") or COMPUTE_TYPE)

        try:
            await anyio.to_thread.run_sync(MODELS.get, model, compute_type)
        except Exception as exc:  # noqa: BLE001
            await ws.send_text(json.dumps(
                {"message": "error", "uid": uid, "reason": f"Failed to load model: {exc}"}))
            await ws.close()
            return

        await ws.send_text(json.dumps(
            {"message": "server_ready", "uid": uid, "language": language}))

        chunks: list[np.ndarray] = []   # accumulateur de chunks float32
        base = 0                          # index global du 1er échantillon de chunks[0]
        total = 0                         # nombre total d'échantillons reçus
        vad = StreamingVAD() if use_vad else None
        log.info("WS session ouverte (uid=%s, model=%s, vad=%s)", uid, model, use_vad)
        log.info("WS en attente de données audio (uid=%s)...", uid)

        try:
            while True:
                msg = await ws.receive()
                mtype = msg.get("type")
                log.debug("WS receive: type=%s, keys=%s", mtype,
                          list(msg.keys()) if isinstance(msg, dict) else type(msg))
                if mtype == "websocket.disconnect":
                    log.info("WS déconnexion reçue (uid=%s, code=%s, reason=%s)",
                             uid, msg.get("code"), msg.get("reason"))
                    break
                if mtype != "websocket.receive":
                    continue
                if msg.get("bytes") is not None:
                    chunk = np.frombuffer(msg["bytes"], dtype=np.int16)
                    if chunk.size == 0:
                        continue
                    f = chunk.astype(np.float32) / 32768.0
                    chunks.append(f)
                    total += f.size
                    if vad is None:
                        continue
                    for start, end in vad.feed(f):
                        await _send_segment(ws, uid, model, language,
                                            chunks, base, start, end,
                                            compute_type=compute_type)
                        base = _prune_chunks(chunks, base, end)
                elif msg.get("text"):
                    try:
                        j = json.loads(msg["text"])
                    except (TypeError, ValueError):
                        continue
                    if j.get("eof"):
                        break
        except WebSocketDisconnect as exc:
            log.info("WS WebSocketDisconnect (uid=%s, code=%s)", uid, getattr(exc, 'code', '?'))
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("WS session erreur (uid=%s): %s", uid, exc)

        # --- Finalisation (EOF ou déconnexion) ---------------------------------
        if vad is not None:
            seg = vad.flush()
            if seg is not None:
                start, end = seg
                if start < total:
                    await _send_segment(ws, uid, model, language,
                                        chunks, base, start, min(end, total),
                                        compute_type=compute_type)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("WS session fermée (uid=%s)", uid)
    finally:
        _active_ws.discard(ws)


async def _send_segment(ws: WebSocket, uid: str, model: str,
                        language: Optional[str], chunks: list[np.ndarray],
                        base: int, start: int, end: int,
                        compute_type: str = COMPUTE_TYPE) -> None:
    """Transcrit le segment [start:end) du flux et l'envoie au client."""
    seg = _materialize(chunks, base, start, end)
    if seg.size < SR // 20:                    # < 50 ms : segment vide
        return
    try:
        text, _info = await anyio.to_thread.run_sync(
            MODELS.transcribe, model, seg, language, compute_type)
    except Exception as exc:  # noqa: BLE001
        log.warning("Échec transcription segment (uid=%s): %s", uid, exc)
        return
    if not text:
        return
    await ws.send_text(json.dumps({
        "message": "transcript", "uid": uid,
        "segments": [{
            "start": round(start / SR, 3),
            "end": round(end / SR, 3),
            "text": text,
        }],
    }))
    log.info("WS segment transcrit (uid=%s, %d→%d) : %r",
             uid, start, end, text[:60])


def _materialize(chunks: list[np.ndarray], base: int,
                 start: int, end: int) -> np.ndarray:
    """Concatène les chunks couvrant [start:end) en un tableau float32."""
    parts: list[np.ndarray] = []
    offset = base
    for c in chunks:
        c_end = offset + c.size
        if c_end > start and offset < end:
            lo = max(start, offset) - offset
            hi = min(end, c_end) - offset
            if hi > lo:
                parts.append(c[lo:hi])
        offset = c_end
        if offset >= end:
            break
    if not parts:
        return np.zeros(0, dtype=np.float32)
    if len(parts) == 1:
        return parts[0]
    return np.concatenate(parts)


def _prune_chunks(chunks: list[np.ndarray], base: int,
                  before: int) -> int:
    """Retire les chunks entièrement avant `before` ; retourne le nouveau base."""
    offset = base
    keep_from = 0
    for i, c in enumerate(chunks):
        c_end = offset + c.size
        if c_end <= before:
            keep_from = i + 1
        offset = c_end
    if keep_from:
        new_base = base + sum(c.size for c in chunks[:keep_from])
        del chunks[:keep_from]
        return new_base
    return base


app.include_router(router)

# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Talky serveur : REST+WS sur 0.0.0.0:%d (compute_type=%s, "
             "modèle=%s, cache=%s)", WS_PORT, COMPUTE_TYPE, DEFAULT_MODEL,
             CACHE_DIR)
    uvicorn.run(app, host="0.0.0.0", port=WS_PORT)
