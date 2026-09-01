# -*- coding: utf-8 -*-
"""
app/engine/whisperlive_client.py
================================
Client WebSocket temps réel vers whisper-live (WhisperLive de Collabora,
hwdsl2/whisper-live-server) — mode continu de Talky.

Le serveur expose, en plus du REST OpenAI-compatible (port 8000), un
WebSocket sur le port 9090 (path /) avec le protocole
WhisperLive :

  1. Handshake (1er message JSON) :
       {"uid": "<uuid4>", "model": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "task": "transcribe",
        "use_vad": true, "language": "fr", "same_output_threshold": 2.0}
  2. Puis chunks audio BINAIRES : PCM int16 mono 16 kHz (PAS de WAV).
  3. Réponses serveur JSON : {"uid":..., "message": "server_ready"} au début,
     puis {"uid":..., "message": "transcript",
           "segments": [{"start": t1, "end": t2, "text": "..."}]} au fil du
     flux (le VAD serveur découpe les phrases) ; éventuel message "error".
  4. Fin : envoyer {"uid":..., "eof": true} (ou fermer).

Choix de la lib : ``websockets`` (asyncio) — version officielle du paquet
Arch ``python-websockets``, la plus maintenue et compatible Python 3.14.
Le client encapsule une boucle asyncio dans un thread dédié (``websockets``
n'est pas thread-safe) et expose une façade SYNCHRONE thread-safe
(connect / send_audio / recv_event / send_eof / close), adaptée au moteur
dictation.py qui s'exécute dans des threads daemon.

Contrat d'erreur : AUCUNE méthode ne lève d'exception applicative — chaque
échec est traduit en message FR exposé via ``client.error`` (retour bool/None
selon la méthode), dans le style de transcriber_client.py.
"""

import asyncio
import json
import logging
import queue
import threading
import uuid
from typing import Optional

try:
    import websockets
    from websockets.exceptions import ConnectionClosed as _WSConnectionClosed
except Exception:  # pragma: no cover - lib absente (defensif : mock conftest)
    websockets = None
    _WSConnectionClosed = Exception

from app.core.constants import WS_DEFAULT_PORT
from app.engine.transcriber_client import _pcm16_from_float

log = logging.getLogger("talky")

# ---------------------------------------------------------------------------
# Constantes du protocole WhisperLive
# ---------------------------------------------------------------------------
WS_PATH = "/"
DEFAULT_MODEL = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
HANDSHAKE_TASK = "transcribe"
HANDSHAKE_USE_VAD = True
HANDSHAKE_SAME_OUTPUT_THRESHOLD = 2.0

# Timeouts (s) : connexion/handshake, envoi d'une frame, polling du receveur.
CONNECT_TIMEOUT = 8.0
READY_TIMEOUT = 8.0        # attente du server_ready après le handshake
SEND_TIMEOUT = 2.0         # envoi d'un chunk audio (fut.result)
RECV_POLL = 0.25           # polling du receveur asyncio (pas de busy-loop)
CLOSE_TIMEOUT = 2.0

# Événement interne poussé dans la file quand la connexion se ferme.
_EVENT_CLOSED = {"message": "__closed__"}

# ---------------------------------------------------------------------------
# Messages d'erreur FR (jamais d'exception levée)
# ---------------------------------------------------------------------------
_ERR_WS_LIB = ("Librairie WebSocket indisponible — installer python-websockets "
               "(paquet Arch python-websockets)")
_ERR_WS_CONNECT = "Connexion WebSocket impossible — vérifier server_url et ws_port"
_ERR_WS_TIMEOUT = "Le serveur WebSocket a mis trop de temps à répondre"
_ERR_WS_SERVER = "Erreur serveur WebSocket (transcription)"
_ERR_WS_SEND = "Échec de l'envoi audio WebSocket"


# ---------------------------------------------------------------------------
# Helpers purs (testables)
# ---------------------------------------------------------------------------
def _normalize(server_url: str) -> str:
    """Retire les slashes de fin d'une URL."""
    return str(server_url or "").strip().rstrip("/")


def build_ws_url(server_url: str, ws_port: Optional[int] = None,
                 path: str = WS_PATH) -> str:
    """Construit l'URL WebSocket depuis l'URL REST du serveur.

    http://192.168.1.50:8000 + ws_port 9090
        -> ws://192.168.1.50:9090/

    Schéma https -> wss (même hôte). Retourne une URL valide même si
    ``server_url`` est vide (hôte vide → ws://:9090/...).
    """
    from urllib.parse import urlsplit

    parts = urlsplit(_normalize(server_url))
    host = parts.hostname or ""
    port = ws_port if ws_port is not None else WS_DEFAULT_PORT
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{host}:{port}{path}"


def make_handshake(uid: str, model: str, language: Optional[str],
                   task: str = HANDSHAKE_TASK,
                   use_vad: bool = HANDSHAKE_USE_VAD,
                   same_output_threshold: float = HANDSHAKE_SAME_OUTPUT_THRESHOLD,
                   compute_type: str = "int8",
                   ) -> dict:
    """Construit le message JSON de handshake WhisperLive.

    La langue est omise quand elle est None / "auto" (auto-détection serveur).
    """
    handshake = {
        "uid": uid,
        "model": model or DEFAULT_MODEL,
        "task": task,
        "use_vad": use_vad,
        "same_output_threshold": same_output_threshold,
        "compute_type": compute_type,
    }
    if language not in (None, "", "auto"):
        handshake["language"] = str(language)
    return handshake


def float32_to_int16(audio) -> bytes:
    """Convertit un tableau float32 (16 kHz mono, valeurs ~[-1, 1]) en
    PCM16 int16 petit-boutiste — les chunks envoyés au serveur sont des
    frames BINAIRES brutes, SANS en-tête WAV.

    Clip [-1, 1] puis * 32767 (arrondi au plus proche), comme encode_wav().
    Vectorisé numpy (clip -> astype('<i2') -> tobytes()) quand les fonctions
    numpy réelles sont disponibles ; repli boucle Python (octets strictement
    identiques) si numpy est absent/mocké (conftest de test).
    """
    return _pcm16_from_float(audio)


def parse_event(raw) -> Optional[dict]:
    """Parse un message serveur JSON en dict (None si invalide)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def transcript_text(event: dict) -> str:
    """Concatène les textes des segments d'un message « transcript »."""
    segments = event.get("segments") or []
    parts = []
    for seg in segments:
        if isinstance(seg, dict):
            text = str(seg.get("text") or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts).strip()


class WhisperLiveClient:
    """Session WebSocket WhisperLive, façade synchrone sur une boucle asyncio
    exécutée dans un thread dédié (``websockets`` n'est pas thread-safe).

    Usage ::

        client = WhisperLiveClient(host, ws_port, model, language)
        if not client.connect():
            log(client.error)
            return
        client.send_audio(chunk)          # float32 -> int16 -> binaire
        while (event := client.recv_event(timeout=0.5)):
            ...  # {"message": "transcript", "segments": [...]}
        client.send_eof()
        client.close()
    """

    def __init__(self, host: str, ws_port: int,
                 model: str = DEFAULT_MODEL,
                 language: Optional[str] = None,
                 uid: Optional[str] = None,
                 compute_type: str = "int8",
                 server_api_key: str = "") -> None:
        self.host = str(host or "")
        self.ws_port = int(ws_port or WS_DEFAULT_PORT)
        self.model = model or DEFAULT_MODEL
        self.language = language
        self.compute_type = compute_type or "int8"
        self.uid = uid or str(uuid.uuid4())
        # Clé API du serveur talky (middleware TALKY_API_KEY) : si non vide,
        # envoyée en header `Authorization: Bearer <clé>` au handshake WS.
        self.server_api_key = str(server_api_key or "")
        self._error: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._ws = None
        self._connected = threading.Event()   # phase connect terminée (ok/échec)
        self._closed = threading.Event()      # close() demandée
        self._events: Optional[queue.Queue] = None

    # ------------------------------------------------------------------
    # Propriétés
    # ------------------------------------------------------------------
    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def url(self) -> str:
        """URL WebSocket par défaut (ws://host:ws_port/).
        Le schéma wss est géré par ``build_ws_url`` quand l'URL est dérivée
        d'une URL REST https ; ici (hôte nu) on reste en ``ws``."""
        return f"ws://{self.host}:{self.ws_port}{WS_PATH}"

    @property
    def is_connected(self) -> bool:
        return (self._connected.is_set() and self._error is None
                and not self._closed.is_set())

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    def connect(self, url: Optional[str] = None,
                timeout: float = CONNECT_TIMEOUT) -> bool:
        """Ouvre la session, envoie le handshake et attend « server_ready ».

        Bloque jusqu'à server_ready (ou échec/timeout). Retourne True si la
        session est prête à recevoir l'audio, False sinon (``self.error`` en
        français). Ne lève jamais.
        """
        if websockets is None:
            self._error = _ERR_WS_LIB
            return False

        self._error = None
        self._closed.clear()
        self._connected.clear()
        self._events = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="whisperlive-loop", daemon=True)
        self._loop_thread.start()

        target_url = url or self.url
        log.info(f"WhisperLive connect: tentative vers {target_url}")
        try:
            asyncio.run_coroutine_threadsafe(
                self._session(target_url), self._loop)
        except Exception:  # noqa: BLE001 — échec d'amorçage de la boucle
            self._error = _ERR_WS_CONNECT
            log.warning(f"WhisperLive connect: échec amorçage boucle asyncio")
            self.close()
            return False

        if not self._connected.wait(timeout=timeout):
            self._error = _ERR_WS_TIMEOUT
            log.warning(f"WhisperLive connect: timeout ({timeout}s) — pas de server_ready")
            self.close()
            return False
        if self._error is not None:
            log.warning(f"WhisperLive connect: échec — {self._error}")
            self.close()
            return False
        log.info("WhisperLive connect: session prête (server_ready reçu)")
        return True

    def close(self) -> None:
        """Ferme la session et arrête la boucle asyncio (thread dédié)."""
        self._closed.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._close_ws(), loop)
                fut.result(timeout=CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001 — fermeture best-effort
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        thread = self._loop_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=CLOSE_TIMEOUT)
        self._loop_thread = None
        self._loop = None
        self._ws = None

    # ------------------------------------------------------------------
    # Envoi audio / EOF
    # ------------------------------------------------------------------
    def send_audio(self, chunk, timeout: float = SEND_TIMEOUT) -> bool:
        """Envoie un chunk float32 (16 kHz mono) converti en PCM16 int16 brut.

        Retourne True si la frame a été envoyée, False sinon (erreur FR via
        ``self.error``).
        """
        payload = float32_to_int16(chunk)
        log.debug(f"WS send_audio: {len(payload)} bytes")  # debug pour ne pas spammer
        ok = self._submit(self._send(payload), timeout)
        if not ok and self._error is None:
            self._error = _ERR_WS_SEND
        return ok

    def send_eof(self, timeout: float = SEND_TIMEOUT) -> bool:
        """Envoie le message de fin de flux : {"uid":..., "eof": true}."""
        payload = json.dumps({"uid": self.uid, "eof": True})
        return self._submit(self._send(payload), timeout)

    def recv_event(self, timeout: float = RECV_POLL) -> Optional[dict]:
        """Retourne le prochain événement serveur (dict JSON), None si timeout.

        Événements possibles : {"message": "transcript", "segments": [...]},
        {"message": "error"}, ou le sentinel interne {"message": "__closed__"}.
        """
        events = self._events
        if events is None:
            return None
        try:
            return events.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Internes (boucle asyncio du thread dédié)
    # ------------------------------------------------------------------
    def _submit(self, coro, timeout: float) -> bool:
        if (self._loop is None or not self._loop.is_running()
                or self._closed.is_set()):
            coro.close()   # évite le RuntimeWarning « never awaited »
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            fut.result(timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001 — connexion fermée / timeout
            log.warning(f"WS _submit: échec ({type(exc).__name__}: {exc})")
            return False

    async def _send(self, payload) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(payload)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"WS _send: exception ({type(exc).__name__}: {exc})")
                raise

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass

    def _run_loop(self) -> None:
        loop = self._loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:  # noqa: BLE001 — nettoyage best-effort
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _session(self, url: str) -> None:
        """Coroutine principale : connecte, handshake, puis écoute les
        événements serveur jusqu'à la fermeture."""
        try:
            # Auth WS (middleware TALKY_API_KEY côté serveur) : le header
            # `Authorization: Bearer <clé>` n'est ajouté que si la clé est
            # non vide (clé vide = pas d'authentification, comportement
            # inchangé).
            connect_kwargs: dict = {}
            if self.server_api_key:
                connect_kwargs["extra_headers"] = {
                    "Authorization": f"Bearer {self.server_api_key}"}
            async with websockets.connect(url, **connect_kwargs) as ws:
                self._ws = ws
                await self._handshake_and_wait_ready(ws)
                if self._error is None:
                    await self._receive_loop(ws)
        except _WSConnectionClosed:
            log.warning(f"WhisperLive session: connexion fermée ({url})")
            self._events.put_nowait(dict(_EVENT_CLOSED))
        except Exception as exc:  # noqa: BLE001 — échec connexion/handshake
            if self._error is None:
                self._error = _ERR_WS_CONNECT
            log.warning(f"WhisperLive session: échec ({exc})")
            self._connected.set()
        finally:
            self._ws = None

    async def _handshake_and_wait_ready(self, ws) -> None:
        """Envoie le handshake puis attend « server_ready » (ou « error »)."""
        handshake = make_handshake(
            self.uid, self.model, self.language,
            compute_type=self.compute_type)
        await ws.send(json.dumps(handshake))
        log.info(f"WhisperLive handshake envoyé (uid={self.uid}, model={self.model}, "
                 f"language={self.language})")
        while True:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=READY_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                self._error = _ERR_WS_TIMEOUT
                log.warning(f"WhisperLive handshake: timeout server_ready ({READY_TIMEOUT}s)")
                self._connected.set()
                return
            event = parse_event(raw)
            if event is None:
                continue
            message = event.get("message")
            log.info(f"WhisperLive handshake: message reçu = {message}")
            if message and message.lower() == "server_ready":
                self._connected.set()
                return
            if message and message.lower() == "error":
                self._error = _ERR_WS_SERVER
                log.warning("WhisperLive handshake: erreur serveur reçue")
                self._connected.set()
                return

    async def _receive_loop(self, ws) -> None:
        """Écoute les messages serveur et les pousse dans la file thread-safe."""
        while not self._closed.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_POLL)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            except _WSConnectionClosed:
                self._events.put_nowait(dict(_EVENT_CLOSED))
                return
            event = parse_event(raw)
            if event is not None:
                self._events.put_nowait(event)
