# -*- coding: utf-8 -*-
"""
app/engine/dictation.py
=======================
DictationEngine — orchestration du moteur de dictée vocale (P5).

Adapté de l'original (ref/app/engine/dictation.py) au modèle client/serveur
de Talky :
  * flux micro 16 kHz mono (sounddevice -> AudioRecorder) ;
  * hotkeys globales evdev (HotkeyManager) ;
  * transcription HTTP batch vers le serveur whisper-live (TranscriberClient) ;
  * mode continu TEMPS RÉEL via le WebSocket WhisperLive (WhisperLiveClient) ;
  * injection du texte par presse-papier (pyperclip + Ctrl+V -> injector).

API publique conservée (compatibilité) :
  start() / stop() / restart()
  apply_config(new_config) -> (reload_needed, live_changed)
  snapshot() / pop_events() / get_history() / clear_history()

Plus de modèle local ni de GPU : les threads daemon sont `dictation-boot`
(démarrage), `dictation-transcribe` (batch), `dictation-ws-connect`
(connexion WebSocket), `dictation-ws-sender` (envoi audio), `dictation-ws-receiver`
(réception transcripts) et `dictation-ws-stop` (finalisation continue).

Mode continu « WebSocket WhisperLive » (remplace l'ancien chunked HTTP batch,
lui-même hérité du WebSocket Realtime speaches) :
pendant l'enregistrement, le AudioRecorder pousse chaque bloc audio (float32
16 kHz mono) dans une queue ; un thread sender les convertit en PCM16 int16
brut et les stream sur ws://host:9090/. Le VAD serveur
découpe les phrases et renvoie des segments {"message": "transcript",
"segments": [...]} : chaque segment est émis en événement partial_transcript
{text, is_final: true, recording: true} (le frontend accumule lui-même les
segments verrouillés). À la relâche de F8, EOF est envoyé, les derniers
segments sont drainés (timeout), puis le texte final est injecté (via
l'injecteur existant) et ajouté à l'historique. Si la session WS échoue
(connexion, envoi, réception), repli automatique sur le batch complet à la
relâche — l'audio complet reste toujours disponible (AudioRecorder.end()).
"""

import logging
import queue
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger("talky")

from app.core.constants import (
    STATE_BOOTING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_READY,
    STATE_RECORDING,
    STATE_STOPPING,
    STATE_SUCCESS,
    STATE_TRANSCRIBING,
    WS_DEFAULT_PORT,
)
from app.engine import transcriber_client
from app.engine.audio import AudioRecorder, AudioRecorderError
from app.engine.config_apply import plan_changes
from app.engine.hotkeys import HotkeyManager, HotkeyError
from app.engine.injector import inject_text
from app.engine.state import EngineState
from app.engine.transcriber_client import TranscriptionError
from app.engine.whisperlive_client import (
    WhisperLiveClient,
    build_ws_url,
    transcript_text,
)
from app.models.schemas import make_transcript_event

# Durée (s) pendant laquelle le statut « success » / « error » reste affiché
# avant de revenir à « ready » (roadmap §5.5). Rendu configurable (tests).
READY_DELAY = 0.8

# Timeout court du ping serveur au boot : on avertit mais on ne bloque pas.
PING_TIMEOUT = 3.0

# ---------------------------------------------------------------------------
# Mode continu « WebSocket WhisperLive »
# ---------------------------------------------------------------------------
# Timeout (s) de la connexion + handshake (server_ready) du WebSocket.
WS_CONNECT_TIMEOUT = 8.0
# Intervalle (s) de polling de la queue audio par le thread sender.
WS_SEND_POLL = 0.05
# Intervalle (s) de polling des événements par le thread receiver.
WS_RECV_POLL = 0.1
# Timeouts (s) des join à la relâche / à l'arrêt.
WS_CONNECT_JOIN_TIMEOUT = 3.0
WS_SENDER_JOIN_TIMEOUT = 2.0
WS_RECEIVER_JOIN_TIMEOUT = 2.0
# Fenêtre (s) de drain des derniers segments après l'envoi de l'EOF.
WS_EOF_DRAIN_TIMEOUT = 2.5
# Période de silence (s) après l'EOF au-delà de laquelle on conclut le drain
# (le serveur ferme normalement la session ; ce seuil évite une attente pleine
# de WS_EOF_DRAIN_TIMEOUT quand plus aucun segment n'arrive).
WS_EOF_QUIET = 0.5


def _language_none(language: object):
    """None / \"\" / \"auto\" -> None (auto-détection côté serveur)."""
    if language in (None, "", "auto"):
        return None
    return str(language)


def _server_host(server_url: str) -> str:
    """Extrait l'hôte d'une URL REST : http://192.168.1.50:8000 -> 192.168.1.50."""
    return urlsplit(str(server_url or "").strip().rstrip("/")).hostname or "localhost"


class DictationEngine:
    """Moteur de dictée exécuté dans ses propres threads daemon."""

    def __init__(self, config: dict) -> None:
        self.config = dict(config)
        self._lock = threading.RLock()      # protège tout le moteur
        self._state = EngineState(self.config, self._lock)

        # Sous-systèmes
        # Le AudioRecorder pousse la waveform (20 fps) via ce callback ;
        # on relaie en événement « audio » vers le frontend (WebSocket).
        # Le callback on_chunk (mode continu) pousse chaque bloc audio vers
        # la queue du thread sender (WebSocket WhisperLive).
        self._audio = AudioRecorder(
            on_level=self._on_audio_level,
            on_chunk=self._on_audio_chunk,
        )
        self._hotkeys: HotkeyManager | None = None
        self._boot_thread: threading.Thread | None = None

        # État du mode continu « WebSocket WhisperLive »
        self._ws_client: WhisperLiveClient | None = None
        self._ws_send_queue: queue.Queue | None = None
        self._ws_stop_event: threading.Event | None = None
        self._ws_connect_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None      # sender
        self._ws_recv_thread: threading.Thread | None = None  # receiver
        self._ws_text: str = ""          # texte cumulé (segments finaux)
        self._ws_failed = False          # échec WS → fallback batch à la relâche

    def _on_audio_level(self, levels: list, recording: bool) -> None:
        """Callback du AudioRecorder : pousse un événement « audio » vers le WS.

        Invoqué depuis le thread audio PortAudio : ne lève jamais pour ne pas
        risquer de bloquer le boot ou la transcription.
        """
        try:
            self._state.emit(
                "audio", {"levels": levels, "recording": recording})
        except Exception:  # noqa: BLE001 — best-effort
            pass

    def _on_audio_chunk(self, chunk) -> None:
        """Callback du AudioRecorder : pousse un bloc audio vers la queue du
        thread sender (mode continu WebSocket WhisperLive).

        Invoqué depuis le thread audio PortAudio : ne lève jamais.
        """
        if self._ws_send_queue is None:
            return
        try:
            self._ws_send_queue.put_nowait(chunk)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    # ------------------------------------------------------------------
    # Délégation d'état (API publique préservée)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return self._state.snapshot(self.config.get("model", ""))

    def pop_events(self) -> list:
        return self._state.pop_events()

    def get_history(self) -> list:
        return self._state.history_get()

    def clear_history(self) -> None:
        self._state.history_clear()

    def _is_recording(self) -> bool:
        return bool(self._audio and self._audio.is_recording())

    # ------------------------------------------------------------------
    # Démarrage / Arrêt / Redémarrage
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Démarre le moteur en tâche de fond (micro + hotkeys)."""
        with self._lock:
            if self._state.status not in (STATE_IDLE, STATE_ERROR):
                return
            self._state.set_status(
                STATE_BOOTING, "Démarrage du moteur de dictée...")
            self._boot_thread = threading.Thread(
                target=self._boot, name="dictation-boot", daemon=True)
        self._boot_thread.start()

    def stop(self) -> None:
        """Arrête proprement le moteur (hooks clavier, flux audio, mode continu)."""
        with self._lock:
            if self._state.status == STATE_IDLE:
                return
            self._state.set_status(STATE_STOPPING, "Arrêt du moteur...")
            self._uninstall_hotkeys()
            # Signaler au thread sender (mode continu WS) de s'arrêter.
            if self._ws_stop_event is not None:
                self._ws_stop_event.set()
            if self._ws_send_queue is not None:
                try:
                    self._ws_send_queue.put_nowait(None)
                except Exception:  # noqa: BLE001
                    pass
            self._audio.stop()
            boot_thread = self._boot_thread
            self._boot_thread = None
            # Collecter les références pour join hors verrou (évite deadlock).
            ws_client = self._ws_client
            ws_connect = self._ws_connect_thread
            ws_sender = self._ws_thread
            ws_receiver = self._ws_recv_thread
            self._ws_client = None
            self._ws_send_queue = None
            self._ws_stop_event = None
            self._ws_connect_thread = None
            self._ws_thread = None
            self._ws_recv_thread = None
            self._ws_text = ""
            self._ws_failed = False
        # Fermeture + join des threads hors verrou.
        if ws_client is not None:
            try:
                ws_client.close()
            except Exception:  # noqa: BLE001 — fermeture best-effort
                pass
        for thread in (ws_sender, ws_receiver, ws_connect, boot_thread):
            # is_alive() : un thread jamais démarré (course stop/connect)
            # ne doit pas être joint (RuntimeError sinon).
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        with self._lock:
            self._state.set_status(STATE_IDLE, "Moteur arrêté.")

    def restart(self) -> None:
        """Redémarre le moteur (équivalent stop + start)."""
        self.stop()
        self.start()

    def _boot(self) -> None:
        """Thread de démarrage : micro, ping serveur (non bloquant), hotkeys."""
        try:
            # 1) Flux micro (erreur claire si le périphérique est invalide).
            with self._lock:
                if self._state.status != STATE_BOOTING:
                    return
                device = self.config.get("audio_device") or None
                self._audio.open(device)
            self._state.log(
                "INFO", f"Flux audio ouvert (device={device or 'défaut'}).")
            if self._boot_aborted():
                self._audio.stop()
                return

            # 2) Ping serveur NON bloquant : avertissement seulement.
            self._ping_server_warning()
            if self._boot_aborted():
                self._audio.stop()
                return

            # 3) Hotkeys + passage à ready (sous verrou : pas de course avec
            #    stop() qui passe par STATE_STOPPING).
            with self._lock:
                if self._state.status != STATE_BOOTING:
                    self._audio.stop()
                    return
                self._install_hotkeys()
                if self._state.status != STATE_BOOTING:
                    self._uninstall_hotkeys()
                    self._audio.stop()
                    return
                self._state.set_status(
                    STATE_READY, "Prêt : maintenez la touche pour dicter.")
            self._state.log(
                "INFO",
                f"Moteur prêt (modèle: {self.config.get('model', '')}, "
                f"serveur: {self.config.get('server_url', '')}).",
            )
        except AudioRecorderError as exc:
            self._state.set_status(STATE_ERROR, str(exc))
        except HotkeyError as exc:
            self._audio.stop()
            self._state.set_status(STATE_ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001 — tout autre échec de démarrage
            self._audio.stop()
            self._state.set_status(STATE_ERROR, f"Erreur de démarrage : {exc}")

    def _boot_aborted(self) -> bool:
        """Vrai si stop()/restart() a interrompu le démarrage en cours."""
        return self._state.status != STATE_BOOTING

    def _ping_server_warning(self) -> None:
        """Ping court (3 s) — ne lève jamais : log avertissement si down."""
        try:
            reachable = transcriber_client.ping(
                self.config.get("server_url", ""),
                api_key=self.config.get("server_api_key", ""),
                timeout=PING_TIMEOUT,
            ).get("reachable", False)
        except Exception:  # noqa: BLE001 — ping contractuellement sans exception
            reachable = False
        if not reachable:
            self._state.log(
                "WARN",
                "Serveur injoignable, la dictée échouera tant que le serveur "
                "est down.",
            )

    # ------------------------------------------------------------------
    # Raccourcis clavier
    # ------------------------------------------------------------------
    def _install_hotkeys(self) -> None:
        """Installe les hotkeys de la configuration courante (utilisé au boot
        et au rollback d'apply_config)."""
        manager = self._make_hotkey_manager(
            hotkey=self.config.get("hotkey", "f8"),
            mode=self.config.get("input_mode", "push_to_talk"),
        )
        manager.install()
        self._hotkeys = manager

    def _uninstall_hotkeys(self) -> None:
        if self._hotkeys is not None:
            self._hotkeys.uninstall()
            self._hotkeys = None

    # ------------------------------------------------------------------
    # Enregistrement / Transcription / Injection
    # ------------------------------------------------------------------
    def _start_recording(self) -> None:
        """Déclenche la capture audio (push-to-talk / toggle).

        En mode continu (``continuous_mode=True``), démarre la session
        WebSocket WhisperLive : connexion + handshake en arrière-plan
        (thread), puis les chunks audio sont streamés au fur et à mesure.
        En mode batch, l'audio est simplement accumulé pour une transcription
        complète à la relâche.
        """
        with self._lock:
            if self._state.status != STATE_READY or self._audio.is_recording():
                return
            self._audio.begin()
            continuous = self.config.get("continuous_mode", True)

        self._state.set_status(
            STATE_RECORDING, "Enregistrement en cours... Relâchez la touche.")
        self._state.log(
            "INFO", "Enregistrement... (relâchez la touche pour transcrire)")
        log.info("Enregistrement... (relâchez la touche pour transcrire) "
                 f"continuous_mode={continuous}")

        if continuous:
            self._start_ws()

    def _stop_and_transcribe(self) -> None:
        """Arrête la capture et lance la transcription.

        En mode continu : démarre le worker de finalisation (EOF, drain des
        derniers segments, fermeture de la session, injection du texte cumulé,
        fallback batch si une erreur WS a eu lieu).
        En mode batch : lance la transcription HTTP serveur dans un thread
        dédié (réseau : ne jamais bloquer les hooks clavier / le web).
        """
        with self._lock:
            if not self._audio.is_recording():
                return
            audio = self._audio.end()
            ws_active = (self._ws_connect_thread is not None
                         or self._ws_thread is not None)

        if ws_active:
            self._state.set_status(
                STATE_TRANSCRIBING, "Finalisation transcription continue…")
            self._state.log("INFO", "Finalisation transcription continue…")
            threading.Thread(
                target=self._ws_stop_worker,
                args=(audio,),
                name="dictation-ws-stop",
                daemon=True,
            ).start()
        elif audio is not None:
            self._state.set_status(STATE_TRANSCRIBING, "Transcription serveur…")
            self._state.log("INFO", "Transcription serveur…")
            threading.Thread(
                target=self._transcription_worker,
                args=(audio,),
                name="dictation-transcribe",
                daemon=True,
            ).start()
        else:
            self._state.set_status(STATE_READY, "Prêt.")

    def _transcription_worker(self, audio) -> None:
        """Transcrit sur le serveur whisper-live puis injecte le texte."""
        try:
            result = transcriber_client.transcribe(audio, self.config)

            if result is None:
                self._state.set_status(STATE_READY, "Aucun texte détecté.")
                return

            ts = time.time()
            self._state.history_append(
                result.text, result.duration, result.language)

            # Statut succès, événement transcript, puis injection.
            self._state.set_status(STATE_SUCCESS, "Succès — texte injecté.")
            self._state.log(
                "INFO",
                f"Transcription : « {result.text[:80]}"
                f"{'…' if len(result.text) > 80 else ''} »",
            )
            self._state.emit(
                "transcript",
                make_transcript_event(
                    result.text, result.language, result.duration, ts)["data"],
            )
            inject_text(
                result.text,
                add_space=self.config.get("add_space", True),
                inject=self.config.get("inject_text", True),
                keep_in_clipboard=self.config.get("keep_in_clipboard", False),
                log_callback=lambda msg: self._state.log("INFO", msg),
            )

            # Retour au statut « ready » après un court instant de succès.
            self._recover_after(STATE_SUCCESS)
        except TranscriptionError as exc:
            self._state.set_status(STATE_ERROR, str(exc))
            self._state.log("ERROR", f"Erreur de transcription : {exc}")
            self._recover_after(STATE_ERROR)
        except Exception as exc:  # noqa: BLE001 — imprévu
            self._state.set_status(STATE_ERROR, f"Erreur de transcription : {exc}")
            self._state.log("ERROR", f"Erreur de transcription : {exc}")
            self._recover_after(STATE_ERROR)

    def _recover_after(self, transient: str) -> None:
        """Après un succès / une erreur transitoire, revient à ready."""
        time.sleep(READY_DELAY)
        with self._lock:
            if self._state.status == transient:
                self._state.set_status(STATE_READY, "Prêt.")

    # ------------------------------------------------------------------
    # Mode continu (WebSocket WhisperLive)
    # ------------------------------------------------------------------
    def _start_ws(self) -> None:
        """Démarre le mode continu WebSocket : un thread de connexion ouvre
        la session (handshake + server_ready), puis les threads sender
        (audio float32 -> int16) et receiver (transcripts) tournent pendant
        l'enregistrement.

        Ne bloque pas le handler hotkey : la connexion réseau se fait dans
        ``dictation-ws-connect``. En cas d'échec, self._ws_failed passe à
        True → repli sur le batch complet à la relâche.
        """
        with self._lock:
            if self._state.status not in (STATE_READY, STATE_RECORDING):
                return
            self._ws_text = ""
            self._ws_failed = False
            self._ws_client = None
            self._ws_send_queue = queue.Queue()
            self._ws_stop_event = threading.Event()
            self._ws_thread = None
            self._ws_recv_thread = None
            self._ws_connect_thread = threading.Thread(
                target=self._ws_connect_worker,
                name="dictation-ws-connect", daemon=True)
            self._ws_connect_thread.start()
        self._state.log(
            "INFO", "Mode continu WhisperLive : connexion WebSocket…")
        log.info("Mode continu WhisperLive : connexion WebSocket…")

    def _ws_connect_worker(self) -> None:
        """Thread de connexion : construit le client WhisperLive, connecte
        (handshake + server_ready), puis démarre les threads sender et
        receiver. Un échec marque self._ws_failed (fallback batch)."""
        try:
            server_url = self.config.get("server_url", "")
            ws_port = int(self.config.get("ws_port") or WS_DEFAULT_PORT)
            url = build_ws_url(server_url, ws_port)
            log.info(f"WS connexion vers {url}")
            client = WhisperLiveClient(
                host=_server_host(server_url),
                ws_port=ws_port,
                model=self.config.get("model")
                or transcriber_client.DEFAULT_MODEL,
                language=_language_none(self.config.get("language")),
                server_api_key=self.config.get("server_api_key", ""),
                compute_type=self.config.get("compute_type", "int8"),
            )
            ok = client.connect(url=url, timeout=WS_CONNECT_TIMEOUT)
            if not ok:
                with self._lock:
                    self._ws_failed = True
                self._state.log(
                    "WARN",
                    f"Mode continu: connexion WebSocket impossible "
                    f"({client.error}) — fallback batch à la relâche.")
                log.warning(f"WS échec connexion ({client.error})")
                return
            with self._lock:
                # Le moteur a pu être arrêté pendant la connexion.
                if (self._ws_stop_event is not None
                        and self._ws_stop_event.is_set()):
                    client.close()
                    return
                self._ws_client = client
                # Démarrage sous verrou : stop() ne voit jamais de threads
                # non démarrés (join sûr).
                self._ws_thread = threading.Thread(
                    target=self._ws_sender_loop,
                    name="dictation-ws-sender", daemon=True)
                self._ws_recv_thread = threading.Thread(
                    target=self._ws_receiver_loop,
                    name="dictation-ws-receiver", daemon=True)
                self._ws_thread.start()
                self._ws_recv_thread.start()
            self._state.log(
                "INFO", "Mode continu WhisperLive actif (streaming PCM16).")
            log.info("Mode continu WhisperLive actif (streaming PCM16).")
        except Exception as exc:  # noqa: BLE001 — imprévu
            with self._lock:
                self._ws_failed = True
            self._state.log(
                "WARN", f"Mode continu: erreur connexion WebSocket ({exc}).")
            log.warning(f"WS erreur ({exc})")
        finally:
            with self._lock:
                self._ws_connect_thread = None

    def _ws_sender_loop(self) -> None:
        """Thread sender : vide la queue audio et envoie chaque chunk
        (float32 → int16 PCM brut) sur la session WebSocket.

        En cas d'échec d'envoi, self._ws_failed passe à True → repli sur le
        batch complet à la relâche (l'audio complet reste disponible).
        """
        log.info("WS sender démarré")
        sent = 0
        _ws_sender_last_log = time.monotonic()
        while True:
            if self._ws_stop_event is not None and self._ws_stop_event.is_set():
                break
            queue_ref = self._ws_send_queue
            if queue_ref is None:
                break
            try:
                chunk = queue_ref.get(timeout=WS_SEND_POLL)
            except queue.Empty:
                if sent and time.monotonic() - _ws_sender_last_log > 2.0:
                    log.info(f"WS sender: {sent} chunks envoyés")
                    _ws_sender_last_log = time.monotonic()
                continue
            if chunk is None:  # sentinel : drain terminé
                log.info(f"WS sender: {sent} chunks envoyés (fin)")
                break
            client = self._ws_client
            if client is None:
                continue
            try:
                if not client.send_audio(chunk):
                    with self._lock:
                        self._ws_failed = True
                    self._state.log(
                        "WARN",
                        f"Mode continu: envoi audio WS échoué ({client.error}) "
                        f"— fallback batch à la relâche.")
                    log.warning(f"WS sender: échec envoi ({client.error})")
                    break
                sent += 1
            except Exception as exc:  # noqa: BLE001 — imprévu
                with self._lock:
                    self._ws_failed = True
                self._state.log(
                    "WARN", f"Mode continu: erreur envoi audio WS ({exc}).")
                log.warning(f"WS sender: exception ({exc})")
                break

    def _ws_receiver_loop(self) -> None:
        """Thread receiver : lit les événements serveur (transcripts) et émet
        partial_transcript pour chaque segment reçu. Un « error » serveur ou
        une fermeture inattendue marque self._ws_failed (fallback batch)."""
        client = self._ws_client
        if client is None:
            return
        log.info("WS receiver démarré")
        idle_since = time.monotonic()
        while True:
            if self._ws_stop_event is not None and self._ws_stop_event.is_set():
                break
            try:
                event = client.recv_event(timeout=WS_RECV_POLL)
            except Exception:  # noqa: BLE001 — session anormale
                with self._lock:
                    self._ws_failed = True
                self._state.log(
                    "WARN", "Mode continu: session WebSocket interrompue.")
                log.warning("WS receiver: session interrompue")
                break
            if event is None:
                if time.monotonic() - idle_since > 3.0:
                    log.info("WS receiver: en attente...")
                    idle_since = time.monotonic()
                continue
            idle_since = time.monotonic()
            message = event.get("message")
            log.info(f"WS recv: type={message}")
            if message == "transcript":
                text = transcript_text(event)
                if text:
                    self._append_ws_text(text)
            elif message == "error":
                with self._lock:
                    self._ws_failed = True
                self._state.log(
                    "WARN", "Mode continu: erreur serveur WebSocket.")
                log.warning("WS receiver: erreur serveur")
                break
            elif message == "__closed__":
                log.info("WS receiver: session fermée")
                break

    def _append_ws_text(self, segment_text: str) -> None:
        """Accumule un segment final reçu du serveur et émet un événement
        partial_transcript {text: <segment>, is_final: True, recording: True}.

        Le frontend accumule lui-même les segments verrouillés
        (app.js → handlePartialTranscript : is_final=true → live.finalText) ;
        ``self._ws_text`` est la copie cumulée du moteur pour l'injection
        finale.
        """
        with self._lock:
            self._ws_text = (self._ws_text + " " + segment_text).strip()
        self._state.emit(
            "partial_transcript",
            {"text": segment_text, "is_final": True, "recording": True})

    def _ws_stop_worker(self, audio) -> None:
        """Worker de finalisation du mode continu WebSocket : arrête le
        thread sender, envoie l'EOF, draine les derniers segments (timeout),
        ferme la session, puis injecte le texte final (via l'injecteur
        existant) et alimente l'historique.

        Filet de sécurité : si la session WS a échoué (connexion, envoi,
        réception) ou si aucun texte n'a été transcrit, on retombe sur le
        batch complet (l'audio complet est toujours accumulé par le
        AudioRecorder) — la dictée n'est jamais perdue.
        """
        try:
            # 1. Signaler l'arrêt immédiatement : le sender et le receveur
            #    s'arrêtent, et une connexion qui aboutirait en retard sera
            #    fermée par le connect worker (aucun thread orphelin).
            with self._lock:
                if self._ws_stop_event is not None:
                    self._ws_stop_event.set()

            # 2. Attendre la fin éventuelle de la connexion en cours.
            connect_thread = self._ws_connect_thread
            if connect_thread is not None:
                connect_thread.join(timeout=WS_CONNECT_JOIN_TIMEOUT)

            # 3. Drainer la queue du sender (sentinel) et joindre le sender.
            if self._ws_send_queue is not None:
                try:
                    self._ws_send_queue.put_nowait(None)
                except Exception:  # noqa: BLE001
                    pass
            sender = self._ws_thread
            if sender is not None:
                sender.join(timeout=WS_SENDER_JOIN_TIMEOUT)

            # 4. EOF puis drain des derniers segments (le VAD serveur peut
            #    encore renvoyer des phrases après l'EOF). Le stop worker est
            #    ici le SEUL lecteur : le receveur s'est arrêté au step 1.
            client = self._ws_client
            if client is not None and not self._ws_failed:
                client.send_eof()
                log.info("WS stop: EOF envoyé")
                log.info("WS stop: drain...")
                deadline = time.monotonic() + WS_EOF_DRAIN_TIMEOUT
                quiet_deadline = None
                drain_count = 0
                while True:
                    if time.monotonic() >= deadline:
                        break
                    event = client.recv_event(timeout=0.1)
                    if event is None:
                        # Rien dans la file : on laisse un court silence pour
                        # laisser le serveur finaliser, puis on conclut.
                        if quiet_deadline is None:
                            quiet_deadline = (
                                time.monotonic() + WS_EOF_QUIET)
                        elif time.monotonic() >= quiet_deadline:
                            break
                        continue
                    quiet_deadline = None
                    message = event.get("message")
                    if message == "transcript":
                        text = transcript_text(event)
                        if text:
                            drain_count += 1
                            self._append_ws_text(text)
                    elif message == "error":
                        with self._lock:
                            self._ws_failed = True
                        self._state.log(
                            "WARN", "Mode continu: erreur serveur WebSocket "
                            "à la finalisation.")
                        log.warning("WS stop: erreur serveur à la finalisation")
                        break
                    elif message == "__closed__":
                        break
                log.info(f"WS stop: {drain_count} segments reçus pendant le drain")

            # 5. Joindre le receveur et fermer la session.
            receiver = self._ws_recv_thread
            if receiver is not None:
                receiver.join(timeout=WS_RECEIVER_JOIN_TIMEOUT)
            if client is not None:
                client.close()

            # 6. Fallback batch si la session WS a échoué.
            if self._ws_failed:
                self._state.log(
                    "WARN", "Mode continu: échec WebSocket — fallback batch.")
                log.warning("WS stop: fallback batch")
                if audio is not None and len(audio) > 0:
                    self._transcription_worker(audio)
                else:
                    self._state.set_status(
                        STATE_READY, "Aucun texte détecté.")
                return

            # 7. Injection du texte cumulé (segments finaux du serveur).
            text = self._ws_text.strip()
            if not text:
                # Rien transcrit sur le flux : repli sur le batch complet si
                # l'audio contient quelque chose (dictée courte, VAD strict).
                if audio is not None and len(audio) > 0:
                    self._transcription_worker(audio)
                    return
                self._state.set_status(STATE_READY, "Aucun texte détecté.")
                return

            ts = time.time()
            language = self.config.get("language") or "fr"
            self._state.history_append(text, 0.0, language)
            self._state.set_status(STATE_SUCCESS, "Succès — texte injecté.")
            self._state.log(
                "INFO",
                f"Transcription : « {text[:80]}"
                f"{'…' if len(text) > 80 else ''} »",
            )
            log.info(f"WS stop: injection texte « {text[:80]}… »")
            self._state.emit(
                "partial_transcript",
                {"text": text, "is_final": True, "recording": False})
            self._state.emit(
                "transcript",
                make_transcript_event(text, language, 0.0, ts)["data"],
            )
            inject_text(
                text,
                add_space=self.config.get("add_space", True),
                inject=self.config.get("inject_text", True),
                keep_in_clipboard=self.config.get("keep_in_clipboard", False),
                log_callback=lambda msg: self._state.log("INFO", msg),
            )
            self._recover_after(STATE_SUCCESS)
        except Exception as exc:  # noqa: BLE001 — imprévu
            self._state.set_status(
                STATE_ERROR, f"Erreur transcription continue : {exc}")
            self._state.log("ERROR", f"Erreur transcription continue : {exc}")
            self._recover_after(STATE_ERROR)
        finally:
            with self._lock:
                self._ws_client = None
                self._ws_send_queue = None
                self._ws_stop_event = None
                self._ws_connect_thread = None
                self._ws_thread = None
                self._ws_recv_thread = None
                self._ws_text = ""
                self._ws_failed = False

    # ------------------------------------------------------------------
    # Application dynamique d'une configuration (sans redémarrer à la main)
    # ------------------------------------------------------------------
    def apply_config(self, new_config: dict) -> tuple[bool, list[str]]:
        """
        Applique une nouvelle configuration dictée par l'API.

        Retourne (reload_needed, live_changed) :
          * reload_needed : un redémarrage complet est requis (audio_device) ;
          * live_changed   : liste des champs HOT_FIELDS modifiés.
        Les changements de hotkey/input_mode sont réinstallés à chaud ;
        un changement d'audio_device redémarre le moteur.

        Transactionnel (M2) : la NOUVELLE hotkey est résolue (parse_hotkey)
        et installée AVANT toute mutation de ``self.config`` — en cas
        d'échec (ValueError touche inconnue, HotkeyError /dev/input), la
        configuration et l'installation courante restent inchangées et
        l'exception est propagée à l'appelant (la route ne persiste donc
        rien). Le swap (nouveaux hooks) n'a lieu qu'après installation
        réussie ; l'ancien manager est retiré ensuite, avec rollback si un
        problème survenait malgré tout.
        """
        with self._lock:
            old = dict(self.config)
            reload_needed, live_changed = plan_changes(old, new_config)
            active = self._state.status not in (
                STATE_IDLE, STATE_ERROR, STATE_STOPPING)
            hotkey_changed = (
                not reload_needed
                and any(k in live_changed for k in ("hotkey", "input_mode")))

            # 1) Construire + installer le nouveau manager SANS rien toucher
            #    d'autre : si parse_hotkey/install échoue, l'installation
            #    courante reste opérationnelle.
            new_manager = None
            old_manager = self._hotkeys
            if active and hotkey_changed:
                new_manager = self._make_hotkey_manager(
                    hotkey=new_config.get("hotkey", "f8"),
                    mode=new_config.get("input_mode", "push_to_talk"),
                )
                new_manager.bind_recording_state(self._is_recording)
                try:
                    new_manager.install()   # ValueError / HotkeyError possibles
                except Exception:
                    # Aucune mutation n'a encore eu lieu ; restaurer quand
                    # même l'invariant ``self._hotkeys == manager actif``.
                    self._hotkeys = old_manager
                    raise

            # 2) Installation réussie : on peut appliquer la config + swap.
            self.config.update(new_config)
            self._state.history_resize(
                max(int(new_config.get("max_history", 50)), 1))
            if new_manager is not None:
                self._hotkeys = new_manager

        # 3) Retrait de l'ANCIENNE installation HORS verrou (uninstall joint
        #    les threads evdev). En cas d'échec (improbable), rollback :
        #    l'ancienne installation est déjà remplacée par la nouvelle
        #    (opérationnelle) — on ne fait que consigner l'erreur.
        if new_manager is not None and old_manager is not None:
            try:
                old_manager.uninstall()
            except Exception:  # noqa: BLE001 — best-effort
                log.exception("Échec du retrait des anciennes hotkeys.")

        # Application à chaud (touche / mode / autres champs) si le moteur
        # tourne.
        if not reload_needed and live_changed:
            self._state.log("INFO", "Configuration appliquée à chaud.")

        if reload_needed and active:
            self.restart()

        return reload_needed, live_changed

    def _make_hotkey_manager(self, hotkey: str, mode: str) -> HotkeyManager:
        """Construit un HotkeyManager prêt à install() pour (hotkey, mode)."""
        manager = HotkeyManager(
            hotkey=hotkey,
            mode=mode,
            on_record_start=self._start_recording,
            on_record_stop=self._stop_and_transcribe,
        )
        manager.bind_recording_state(self._is_recording)
        return manager
