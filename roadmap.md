# Talky CachyOS — Roadmap d'architecture (dictée vocale client/serveur)

**Version** : 3.0 — **Statut** : P0–P8 terminés (mode batch REST + mode continu WebSocket en production, sur le conteneur maison `talky-server` basé sur faster-whisper)
**Références** : `research/RAPPORT_STACK_2026.md` (archive historique — versions verrouillées à l'époque de speaches). *Le dossier `ref/` (projet Windows original) a été supprimé.*

---

## 0. Résumé de l'état des phases

| Phase | Description | Statut |
|-------|-------------|--------|
| P0 | Fondations (backend + tests) | ✅ Terminé |
| P1 | Serveur Docker (compose, whisper-live, CUDA) | ✅ Terminé |
| P2 | Client HTTP (transcriber_client) | ✅ Terminé |
| P3 | Audio + Hotkeys (evdev, waveform) | ✅ Terminé |
| P4 | Injecteur (Wayland wl-copy + ydotool/wtype/UInput) | ✅ Terminé |
| P5 | Moteur (dictation batch + continu WebSocket WhisperLive, state, config_apply) | ✅ Terminé |
| P6 | API locale (routes config/engine/history/devices/server, websocket) | ✅ Terminé |
| P7 | Frontend (pleine largeur, 2 colonnes, courbe audio, sélecteur modèle, transcription en direct) | ✅ Terminé |
| P8 | Intégration E2E | ✅ Terminé — mode batch REST + mode continu WebSocket WhisperLive validés en E2E réel sur CachyOS + serveur LAN |

---

## 1. Vue d'ensemble de l'architecture

Architecture **client/serveur sur LAN**, sans GPU utilisé côté client. Le serveur
est le **conteneur maison `talky-server`** (`server/server.py`, faster-whisper +
FastAPI, image CUDA) qui expose **deux interfaces** : REST OpenAI-compatible
(port 8000, batch) et WebSocket temps réel (port 9090, dictée continue).

```
┌───────────────────────────── LAN (192.168.x.x) ─────────────────────────────┐
│  CLIENT — CachyOS (KDE Plasma, Wayland, Python 3.14)                         │
│  python main.py (uvicorn 127.0.0.1:8000) : Web panel FastAPI + API locale +  │
│  DictationEngine (threads daemon) — hotkeys evdev /dev/input, audio          │
│  sounddevice 16 kHz                                                          │
│    • mode batch   : TranscriberClient httpx → POST /v1/audio/transcriptions  │
│    • mode continu : WhisperLiveClient (websockets) → ws://host:9090          │
│                     /client/ws/speech (PCM16 + VAD serveur)                  │
│  Injecteur : wl-copy + Ctrl+V (ydotool/wtype/evdev UInput)                   │
└───────────────┬──────────────────────────────────────────────┬──────────────┘
                │ HTTP multipart (batch, REST 8000)            │ WebSocket PCM16 (continu, 9090)
┌───────────────▼──────────────────────────────────────────────▼──────────────┐
│  SERVEUR — Unraid (Docker)                                                   │
│  talky-server (conteneur maison, faster-whisper + FastAPI)                   │
│    • REST 8000      → POST /v1/audio/transcriptions (OpenAI-compatible)      │
│    • WebSocket 9090 → streaming temps réel (handshake JSON + PCM16 + segments)│
│    • GPU RTX 4070 12 Go (CUDA, INT8) — modèle actif large-v3-turbo           │
│    • bind mount ./hf-hub-cache → /var/lib/whisper-live (modèles + état)      │
│  API_KEY optionnel (TALKY_API_KEY, défaut désactivé)                         │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Flux nominal (mode batch)** : pression hotkey (push-to-talk) → AudioRecorder capture le micro (16 kHz mono) → relâchement → encodage WAV en mémoire → TranscriberClient envoie le multipart au serveur whisper-live (`POST /v1/audio/transcriptions`) → réponse verbose_json → Injector copie le texte (wl-copy) et colle Ctrl+V dans la fenêtre active → historique + événement transcript → panneau web mis à jour via WebSocket.

**Flux nominal (mode continu)** ✅ : pression hotkey → AudioRecorder capture et pousse chaque bloc audio (float32 16 kHz mono) dans une queue → un thread sender les convertit en PCM16 brut et les **stream** sur `ws://{host}:{ws_port}/client/ws/speech` → le **VAD serveur** découpe les phrases et renvoie des segments `{"message": "transcript", "segments": [...]}` → chaque segment est émis en événement `partial_transcript` (le frontend accumule les segments verrouillés) → relâchement → envoi d'un EOF, drain des derniers segments (timeout), injection du texte final + historique. **Fallback automatique** sur le batch complet à la relâche si la session WebSocket échoue (connexion, envoi, réception) — la dictée n'est jamais perdue. **Le vrai temps réel (VAD serveur) fonctionne : l'ancien bug Realtime speaches est résolu par la migration (voir §9.1).**

**Principes directeurs**
- Client sans GPU local : pas de faster-whisper/CTranslate2/CUDA côté client. Seul le serveur fait l'inférence.
- Serveur portable : un seul docker-compose.yml + .env.example, fonctionnel sur Unraid et Proxmox, image `talky-server` (build local), **bind mount** `./hf-hub-cache` → `/var/lib/whisper-live` (pas de volume nommé, pas de chown : l'image tourne en root).
- Le **modèle est configurable** (`TALKY_MODEL`, défaut `large-v3-turbo`) et peut être **installé depuis le client** : `GET /v1/registry` (liste des modèles disponibles) + `POST /v1/models` (téléchargement dans le cache). Le paramètre REST OpenAI `model` est honoré (alias faster-whisper ou repo ID complet).
- API_KEY optionnel (`TALKY_API_KEY=` vide dans .env) : le serveur est sur un LAN privé, pas besoin d'authentification par défaut. Si la variable est non vide, `Authorization: Bearer <clé>` est exigé sur REST et dans le handshake WebSocket.

---

## 2. Structure de fichiers du client

```
/projects/talky/
├── roadmap.md                       # ce fichier (mis à jour)
├── GUIDE_E2E.md                     # guide d'intégration E2E
├── research/                        # rapport stack (archive), notes de recherche
├── server/
│   ├── docker-compose.yml           # service whisper-live (:cuda, 2 ports 8000/9090, GPU, bind mount)
│   ├── .env.example                  # TALKY_API_KEY (vide), TALKY_PORT, TALKY_WS_PORT, WHISPER_MODEL, WHISPER_LANGUAGE, HF_CACHE_PATH
│   ├── README.md                    # déploiement + NVIDIA Container Toolkit (réécrit par le devops)
│   ├── smoke_test.sh                # sonde /docs + /openapi.json + transcription + WS optionnel
│   └── test.wav                     # fichier audio de test
└── client/
    ├── main.py                      # point d'entrée uvicorn (127.0.0.1:8000)
    ├── requirements.txt             # dépendances client (dont websockets)
    ├── config.json                   # configuration persistante (générée par setup.sh)
    ├── config.template.json          # template de configuration
    ├── setup.sh                      # installation & configuration (Arch/CachyOS)
    ├── run.sh                        # lancement du client
    ├── talky.service.example         # exemple de service systemd user
    ├── README.md                     # guide utilisateur CachyOS
    ├── NOTES_P7_TEST_MANUEL.md       # notes de test manuel frontend
    ├── templates/index.html          # page web unique (SPA légère)
    ├── static/
    │   ├── app.js                    # logique frontend (WS + REST + sections)
    │   └── style.css                 # styles (verre, palette, courbe audio)
    ├── tests/
    │   ├── conftest.py               # mocks (evdev, sounddevice, pyperclip, httpx, subprocess, websocket)
    │   ├── test_core_config.py       # config.json : défauts, save/load, validation (dont ws_port)
    │   ├── test_transcriber_client.py # transcribe, ping, list_models
    │   ├── test_whisperlive_client.py # client WebSocket WhisperLive (handshake, PCM16, EOF)
    │   ├── test_audio.py             # AudioRecorder : capture, on_level (waveform), on_chunk
    │   ├── test_hotkeys.py           # HotkeyManager evdev
    │   ├── test_injector.py          # Injector Wayland
    │   ├── test_engine.py            # DictationEngine : batch + continu WebSocket + apply_config
    │   └── test_api.py               # routes API + websocket
    └── app/
        ├── core/
        │   ├── constants.py          # SAMPLING_RATE, WS_DEFAULT_PORT (9090), états, HOT_FIELDS, SERVER_DEFAULTS
        │   ├── config.py             # config.json, DEFAULT_CONFIG, load/save/validate
        │   └── logging.py            # logger unique
        ├── models/
        │   └── schemas.py
        ├── engine/
        │   ├── audio.py              # AudioRecorder (sounddevice 16 kHz, on_level + on_chunk)
        │   ├── hotkeys.py            # HotkeyManager evdev
        │   ├── injector.py           # Injector Wayland (wl-copy + ydotool/wtype/UInput)
        │   ├── transcriber_client.py # client HTTP httpx → whisper-live REST (transcribe, ping, list_models)
        │   ├── whisperlive_client.py # NOUVEAU : client WebSocket WhisperLive (mode continu temps réel, ws_port 9090)
        │   ├── state.py              # EngineState
        │   ├── config_apply.py       # plan_changes adapté
        │   └── dictation.py          # DictationEngine (batch REST + continu WebSocket WhisperLive)
        └── api/
            ├── dependencies.py       # singleton engine + broadcast WS
            ├── lifespan.py           # auto_start + arrêt propre
            ├── factory.py            # build_app()
            ├── routes_config.py
            ├── routes_engine.py
            ├── routes_history.py
            ├── routes_devices.py
            ├── routes_server.py       # GET status, POST test (registry/download SUPPRIMÉES)
            └── websocket.py
```

**Supprimés vs original Windows** : `engine/model_manager.py`, `engine/transcriber.py`, `engine/cuda.py`, dossier `ref/` entier (projet Windows original supprimé — plus nécessaire).
**Supprimés (migration whisper-live)** : `engine/realtime_client.py` (WebSocket Realtime speaches, bloqué par le bug VAD upstream #567 — voir §9.1), `websocket-client` de `requirements.txt`, `transcriber_client.list_registry()` / `download_model()`, et les routes `/api/server/registry` + `/api/server/models/download` (whisper-live n'a ni registry ni installation de modèles).
**Ajoutés (migration whisper-live)** : `engine/whisperlive_client.py` (mode continu temps réel WebSocket), `websockets` dans `requirements.txt` (paquet Arch `python-websockets`), `ws_port` dans la configuration (défaut 9090).
**Ajoutés vs roadmap initial** : `api/routes_server.py` (status/test), `setup.sh`, `config.template.json`, `run.sh`, `talky.service.example`, `static/style.css`.

---

## 3. Contrat API client ↔ serveur

### 3.1 Endpoint (mode batch)
```
POST {server_url}/v1/audio/transcriptions
Content-Type: multipart/form-data
Authorization: Bearer {server_api_key}   # si API_KEY configurée côté serveur (désactivé en prod)
```
Champ `file` : WAV PCM 16 bits, mono, 16 kHz (encodé en mémoire via wave + io.BytesIO).
Params : `model` (défaut large-v3-turbo — **accepté mais ignoré** côté serveur), `language` (omis si auto), `vad_filter` (true), `response_format` (verbose_json).
> **Pas de `task` ni de `temperature`** : whisper-live ne les accepte pas (le paramètre OpenAI `task` translate/transcribe est absent du serveur) — le client ne les envoie plus (un envoi ferait rejeter la requête en 422).

### 3.2 Réponse
```json
200 OK
{"text": "...", "language": "fr", "duration": 1.24, "segments": [...]}
```
Le client ne garde que text, language, duration → TranscriptionResult. text vide → None.

### 3.3 Timeouts httpx
connect 5 s fixe ; read = config server_timeout (défaut 30 s) ; write 10 s fixe.

### 3.4 Mapping erreurs
ConnectError/ConnectTimeout → « Serveur injoignable — vérifier server_url » ; ReadTimeout → « Le serveur a mis trop de temps à répondre » ; 401/403 → « Authentification refusée (API key) » ; 404 → « Endpoint introuvable — vérifier server_url » ; 422 → « Requête invalide (modèle ou langue) » ; 413 → « Fichier audio trop volumineux » ; 5xx → « Erreur serveur (GPU) — réessayer » ; JSON inattendu → « Réponse serveur inattendue ». Après erreur : moteur repasse à ready, PAS de retry automatique.

### 3.5 Endpoints utilitaires
- GET {server_url}/v1/models : liste lecture seule des modèles (retourne `[]` si erreur/404 — whisper-live expose cet endpoint OpenAI-compatible).
- **Sonde de disponibilité** : GET /docs ou /openapi.json (FastAPI les sert toujours, y compris avec une clé). Le serveur talky expose désormais aussi **GET /health** (sonde ultra-légère, **exemptée d'auth**, utilisée par le healthcheck Docker) — ping() continue de sonder /docs puis /openapi.json avec la clé.
- POST /api/server/test (API locale) → ping + list_models + latence. GET /api/server/status (API locale) → reachable, model, models, device/compute_type (cuda/int8).
> Les routes speaches `/api/server/registry` et `/api/server/models/download` ont été **SUPPRIMÉES** : whisper-live n'a ni registry ni installation de modèles (le modèle est fixé par `WHISPERLIVE_MODEL` côté serveur).

### 3.6 WebSocket WhisperLive (mode continu temps réel) ✅

Le mode continu utilise le WebSocket temps réel de whisper-live (port `ws_port`, défaut **9090**), protocole WhisperLive :

```
ws://{host}:{ws_port}/client/ws/speech
```

1. **Handshake JSON** immédiatement après connexion :
   ```json
   {"uid": "<uuid4>", "model": "large-v3-turbo", "task": "transcribe",
    "use_vad": true, "language": "fr", "same_output_threshold": 2.0}
   ```
   (langue omise si `auto` ; le serveur répond `{"message": "server_ready"}`.)
2. **Audio binaire** : frames WebSocket **binaires**, PCM 16-bit little-endian, **16 kHz**, mono — PAS d'en-tête WAV.
3. **Réponses serveur** (JSON) au fil du flux — le **VAD serveur** découpe les phrases :
   ```json
   {"message": "transcript", "segments": [{"text": "Bonjour", "start": 0.0, "end": 1.2, "completed": true}]}
   ```
   Éventuel `{"message": "error"}` en cas d'erreur serveur.
4. **Fin** : `{"uid": ..., "eof": true}` → drain des derniers segments (timeout 2,5 s) → fermeture de la session.

Chaîne côté client (dictation.py) : blocs audio float32 → `float32_to_int16()` → envoi binaire → événements `partial_transcript` {text: segment, is_final: true, recording: true} émis en direct → à la relâche de F8, EOF + drain + injection du texte final + historique. **Fallback automatique** sur le batch complet si la session WS échoue (connexion, envoi, réception) — la dictée n'est jamais perdue. Latence cible : premier segment ~1-3 s (VAD serveur temps réel).

> L'ancien mode continu « chunked HTTP batch » (segments ~2 s → POST batch) et le WebSocket Realtime speaches sont **remplacés** par ce mode WebSocket natif (voir §9.1).

---

## 4. docker-compose.yml serveur

```yaml
services:
  talky-server:
    build: .
    image: talky-server:latest
    container_name: talky-server
    restart: unless-stopped
    ports:
      - "${TALKY_PORT:-8000}:9090"       # REST (OpenAI-compatible, batch)
      - "${TALKY_WS_PORT:-9090}:9090"    # WebSocket temps réel (dictée)
    environment:
      - TALKY_MODEL=${TALKY_MODEL:-large-v3-turbo}
      - TALKY_LANGUAGE=${TALKY_LANGUAGE:-}
      - TALKY_COMPUTE_TYPE=${TALKY_COMPUTE_TYPE:-int8}
      - TALKY_VAD_THRESHOLD=${TALKY_VAD_THRESHOLD:-0.5}
      - TALKY_VAD_SILENCE_MS=${TALKY_VAD_SILENCE_MS:-500}
      - TALKY_MAX_CLIENTS=${TALKY_MAX_CLIENTS:-4}
      - TALKY_LOG_LEVEL=${TALKY_LOG_LEVEL:-INFO}
    volumes:
      - ${HF_CACHE_PATH:-./hf-hub-cache}:/var/lib/whisper-live
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

**Caractéristiques du conteneur maison `talky-server` :**
- **Image** : build local (`docker build -t talky-server .` depuis `server/`), basée sur faster-whisper + FastAPI.
- **Un seul port conteneur (9090)** sert REST **et** WebSocket ; les ports hôtes 8000 (REST) et 9090 (WS) y mappent tous les deux.
- **Env `TALKY_*`** : modèle `TALKY_MODEL` (défaut large-v3-turbo), langue `TALKY_LANGUAGE` (vide = auto), précision `TALKY_COMPUTE_TYPE` (int8), VAD `TALKY_VAD_THRESHOLD`/`TALKY_VAD_SILENCE_MS`, concurrence `TALKY_MAX_CLIENTS`, logs `TALKY_LOG_LEVEL`.
- **Auth optionnelle** : `TALKY_API_KEY` (vide = aucune auth ; non vide = `Authorization: Bearer <clé>` exigé sur REST + handshake WS).
- **Cache** : bind mount `./hf-hub-cache` → `/var/lib/whisper-live` ; **PAS de chown** (l'image tourne en root).
- **Registry + installation de modèles** : `GET /v1/registry` + `POST /v1/models` (téléchargement dans le cache).

.env.example : `TALKY_API_KEY=` (vide = pas d'auth), `TALKY_PORT=8000`, `TALKY_WS_PORT=9090`, `TALKY_MODEL=large-v3-turbo`, `TALKY_LANGUAGE=fr`, `TALKY_COMPUTE_TYPE=int8`, `TALKY_MAX_CLIENTS=4`, `HF_CACHE_PATH=/mnt/user/appdata/talky/hf-hub-cache` (Unraid) ou `./hf-hub-cache` (défaut).

---

## 5. Détail des modules client

### 5.1 audio.py — porté + extensions
AudioRecorder : sd.InputStream(16000, channels=1, device=config), callback accumule quand recording, begin()/end() → np.concatenate(...).flatten(), end() → None si vide. audio_device configurable.

**Extensions ajoutées (non prévues au roadmap initial) :**
- **`on_level` callback** : monitoring audio permanent à 20 fps (50 ms). Calcule une waveform downsamplée (~64 valeurs dans [-1.0, 1.0]) et la transmet au frontend via WebSocket (événement `audio`). Affichée comme courbe temps réel dans le panneau web. Le callback est optionnel : s'il n'est pas fourni, l'API existante est préservée.
- **`on_chunk` callback** : invoqué pour chaque bloc audio capturé pendant l'enregistrement. Utilisé par le mode continu pour pousser l'audio vers la queue du thread sender (WebSocket WhisperLive).

### 5.2 hotkeys.py — RÉÉCRIT (evdev)
HotkeyManager remplace le module keyboard par python-evdev : énumère /dev/input/event*, ouvre les devices EV_KEY, lecture passive (pas de grab), thread daemon read_loop par device, mapping hotkey string → code evdev (f8→KEY_F8, combos ctrl+space, ctrl+alt+f9 en suivant l'état des modificateurs). Modes push_to_talk (keydown→start, keyup→stop) et toggle (keydown edge→bascule). Ignore value=2 (repeat), dédup multi-claviers. Erreur /dev/input illisible → statut error « ajouter l'utilisateur au groupe input ».

### 5.3 injector.py — RÉÉCRIT (Wayland)
inject_text(text, add_space, inject, keep_in_clipboard, log_callback) : pyperclip 1.11.0 (détecte wl-copy/wl-paste), sauvegarde ancien contenu, copie text+suffix, sleep 0.05, Ctrl+V : fallback ydotool (daemon ydotoold) → wtype -M ctrl v -m ctrl → evdev UInput pur Python. Restauration si keep_in_clipboard=false. inject=false → copie seule si keep_in_clipboard=true.

### 5.4 transcriber_client.py — allégé (migration whisper-live)
encode_wav(audio np.ndarray) → bytes (wave + io.BytesIO, PCM16 16kHz mono) ; TranscriptionResult(text, language, duration) dataclass ; transcribe(audio, config) → TranscriptionResult | None ; ping() → dict (sonde GET /docs puis repli /openapi.json avec la clé API — ne lève jamais) ; TranscriptionError typée avec messages FR du §3.4.

**Migration** : `list_registry()` et `download_model()` **supprimés** (whisper-live n'a ni registry ni installation de modèles). `list_models(server_url, api_key, timeout)` **conservé** (GET /v1/models, liste lecture seule, `[]` si erreur). Les paramètres `task`/`temperature` ne sont **plus envoyés** (voir §3.1).

### 5.5 whisperlive_client.py — NOUVEAU (mode continu temps réel)
Client WebSocket WhisperLive (protocole §3.6) :
- `build_ws_url(server_url, ws_port)` → `ws://{host}:{ws_port}/client/ws/speech` (https → wss).
- `make_handshake(uid, model, language, task, use_vad, same_output_threshold)` → JSON de handshake (langue omise si auto).
- `float32_to_int16(audio)` → PCM16 int16 petit-boutiste brut (sans WAV).
- `transcript_text(event)` → concatène les textes des segments d'un message « transcript ».
- Classe `WhisperLiveClient` : façade **synchrone thread-safe** sur une boucle asyncio dans un thread dédié (`websockets` n'est pas thread-safe). `connect()` (connexion + handshake + attente `server_ready`, timeout 8 s), `send_audio()` (binaire PCM16), `recv_event()` (polling file), `send_eof()`, `close()`. **AUCUNE méthode ne lève** : chaque échec est traduit en message FR via `client.error` (lib absente, connexion impossible, timeout, erreur serveur, échec envoi).

### 5.6 dictation.py — adapté (mode continu WebSocket WhisperLive)
API publique identique à l'original : start/stop/restart/apply_config/snapshot/pop_events/get_history/clear_history.

**Mode batch** (fonctionnel ✅) : _boot → micro → ping serveur (non bloquant) → hotkeys → ready. _start_recording → audio.begin() → _stop_and_transcribe → audio.end() → _transcription_worker → transcribe() → si texte : history_append, inject_text, événement transcript, statut success → ready après 0,8 s.

**Mode continu WebSocket WhisperLive** (fonctionnel ✅) : _start_recording → audio.begin() + _start_ws() → thread `dictation-ws-connect` ouvre la session (handshake + server_ready, non bloquant) puis démarre `dictation-ws-sender` (queue audio → float32→int16 → envoi binaire) et `dictation-ws-receiver` (lecture des transcripts → événement `partial_transcript` {text: segment, is_final: true, recording: true} en direct). _stop_and_transcribe → `dictation-ws-stop` : arrêt du sender, envoi EOF, drain des derniers segments (timeout 2,5 s), fermeture de la session, injection du texte final + historique. **Fallback automatique** : si la session WS échoue (connexion, envoi, réception) ou si aucun texte n'a été transcrit, repli sur le batch complet à la relâche — la dictée n'est jamais perdue.

apply_config : plan_changes adapté, hotkey/input_mode à chaud → réinstallation hooks ; audio_device → restart. `continuous_mode` et `ws_port` sont des HOT_FIELDS (changement à chaud) : True → WebSocket WhisperLive, False → batch complet à la relâche.

### 5.7 state.py — inchangé
EngineState : statut/message, événements deque maxlen 200, historique deque maxlen max_history, RLock, emit/set_status/log/pop_events/history_*.

### 5.8 constants.py + config_apply.py — adaptés
RELOAD_FIELDS = `{"audio_device"}` ; HOT_FIELDS = `{"model","language","task","vad_filter","hotkey","input_mode","inject_text","add_space","keep_in_clipboard","max_history","server_url","server_api_key","server_timeout","ws_port","continuous_mode"}`. WS_DEFAULT_PORT = 9090 ; SERVER_DEFAULTS = {device: cuda, compute_type: int8, device_index: 0}. plan_changes(old,new) → (reload_needed, live_changed).

### 5.9 config.py — DEFAULT_CONFIG adapté
server_url `"http://192.168.1.50:8000"` ; server_api_key `""` ; server_timeout `30` ; **ws_port `9090`** (NOUVEAU : port WebSocket whisper-live) ; model `"large-v3-turbo"` ; language `"fr"` ; task `"transcribe"` ; vad_filter `true` ; hotkey `"f8"` ; input_mode `"push_to_talk"` ; audio_device `null` ; inject_text `true` ; add_space `true` ; keep_in_clipboard `false` ; auto_start `false` ; max_history `50` ; **continuous_mode `true`** (mode continu WebSocket WhisperLive par défaut, fallback batch automatique).

validate_config : langue auto→None, entiers, bools, server_url non vide, server_timeout ≥ 5, **ws_port ≥ 1**. **model peut être vide** (aucun modèle installé/sélectionné) : accepté pour permettre la sauvegarde (le frontend affiche un warning non bloquant).

### 5.10 API locale
factory.py build_app() + lifespan + /static + / → index.html. dependencies.py : singleton engine + websocket_clients + broadcast_events (sleep 0,2 s). lifespan : broadcast task + auto_start → engine.start() + engine.stop() à l'arrêt.

| Route | Méthode | Description | Statut |
|-------|---------|-------------|--------|
| `/api/config` | GET, POST | Configuration (save/load, validation) | ✅ |
| `/api/engine` | GET | État du moteur (snapshot) | ✅ |
| `/api/engine/{start,stop,restart}` | POST | Contrôle du moteur | ✅ |
| `/api/history` | GET, DELETE | Historique des transcriptions | ✅ |
| `/api/devices/audio` | GET | Liste des périphériques audio | ✅ |
| `/api/server/status` | GET | État serveur (reachable, model, device, compute_type, models) | ✅ |
| `/api/server/test` | POST | Test de connexion détaillé (ping + list_models + latence). Accepte un body optionnel `{server_url, server_api_key}` pour tester avec les valeurs du formulaire avant sauvegarde | ✅ |
| `/ws` | WS | WebSocket (hello avec snapshot + config, events en temps réel) | ✅ |

> Les routes `/api/server/registry` et `/api/server/models/download` (speaches) ont été **supprimées** : whisper-live n'a ni registry ni installation de modèles (le modèle est fixé par `WHISPERLIVE_MODEL`).

> **Gel UI corrigé** : les actions moteur (`start`/`stop`/`restart`) ainsi qu'`apply_config` (qui peut redémarrer via stop+join) sont offloadées via `asyncio.to_thread` dans les routes async — `engine.stop()` fait du join() bloquant (jusqu'à ~4 s) qui gèlerait la boucle uvicorn (WS `/ws` + polling morts = interface figée).

### 5.11 Frontend
Design verre (palette #141821, mint/sky/lavender/peach/rose). **Mise en page pleine largeur, 2 colonnes.**

**Fonctionnalités implémentées (certaines non prévues au roadmap initial) :**
- **Anneau radial « V1 Hub »** ✅ : visualisation du niveau du micro (~64 valeurs, 20 fps) reçue via WebSocket (événement `audio`) — 36 rayons autour d'un cercle central (RMS par groupe, lerp 0.35 en rAF, canvas 150×150 avec DPR), couleurs `--accent`/`--accent-glow` par état, halo en enregistrement, rayons minimaux + opacité réduite au repos. **Auto-gain glissant (peak-hold ~4 s) + courbe gamma v^0.55 + amplitude max 34 px** : la voix normale (RMS 0,05–0,15) fait osciller les rayons nettement (~30–90 %). L'anneau est un **indicateur pur non cliquable** (role="img", plus de role=switch) ; le start/stop se fait via un **bouton power dédié** (#power-toggle, rond ~44 px, glyphe ⏻, halo menthe quand moteur ON) placé avant #btn-restart. Remplace l'ancienne waveform à barres miroir.
- **Zoom global automatique (fit-vp)** ✅ : la page s'adapte à la hauteur de la fenêtre **sans scroll** — zoom CSS auto borné [0.85, 1.6] sur le conteneur `.layout` (repli transform si `zoom` non supporté), re-calcul debouncé (150 ms) sur resize et après chaque rendu (renderHistory/updateState/config) ; colonne config scrolle en interne, colonne gauche compacte. Sans JS → scroll normal conservé.
- **Sélecteur de modèle** ✅ : datalist alimentée par `/api/server/status` + presets usuels (saisie libre). **Plus d'installation de modèles côté UI** (modèle fixé par `WHISPERLIVE_MODEL` côté serveur).
- **Zone « Transcription en direct »** ✅ : affichage des transcriptions partielles/finales en mode continu (événement `partial_transcript` via WebSocket).
- **Toggle mode continu** ✅ : bascule entre mode batch et mode continu (continuous_mode dans la config).
- Section « Serveur » : URL, API key (password), timeout, bouton « Tester la connexion » (utilise les valeurs du formulaire avant sauvegarde), badge « Serveur connecté/déconnecté » (WS + polling 5 s).
- Sélecteur langue, tâche, VAD. device/compute_type en lecture seule (« cuda », « int8 »).
- Sauvegarde possible sans modèle sélectionné (warning non bloquant).
- WS + polling REST + capture raccourci (ev.code, repli ev.key).
- Historique en direct.

### 5.12 setup.sh — NOUVEAU
Script d'installation et de configuration du client pour CachyOS/Arch Linux. Idempotent, relançable sans risque.

**Fonctionnalités :**
- Installation des paquets système via pacman (portaudio, pipewire-alsa, python-fastapi, uvicorn, python-httpx, **python-websockets**, python-numpy, python-evdev, python-pyperclip, wl-clipboard, python-pytest).
- **Filtrage des paquets déjà installés** : `pacman -Q` vérifie chaque paquet avant de le passer à pacman — évite le blocage quand un paquet est installé en version plus récente que le dépôt (ex. pipewire 1:1.6.8-1.1 vs 1:1.6.8-1).
- **Pas de partial upgrade** : vérifie `pacman -Qu` avant d'installer. Si le système n'est pas à jour, propose `sudo pacman -Syu`. Si refus, installe uniquement les paquets manquants (nouveaux) sans toucher aux existants.
- Installation de python-sounddevice depuis l'AUR (paru/yay) si absent des dépôts officiels.
- Option `--with-inject-tools` pour installer ydotool/wtype + activer le service user.
- Création du venv Python (--system-site-packages) + pip install (filtre evdev/sounddevice qui viennent des paquets système).
- Ajout de l'utilisateur au groupe `input` (hotkeys evdev).
- Copie de config.template.json → config.json si inexistant (JAMAIS modifié s'il existe).
- Vérification des imports Python (fastapi, uvicorn, httpx, websockets, numpy, sounddevice, evdev, pyperclip).
- Options : `--no-venv`, `--with-inject-tools`, `--assume-yes`, `--no-update`, `--assume-uptodate`, `--allow-partial`.

### 5.13 requirements.txt client
fastapi, uvicorn, httpx, **websockets**, numpy, sounddevice, evdev, pyperclip, pytest. (Commentaires : paquets Arch correspondants, PortAudio pipewire-alsa, python-evdev paquet Arch obligatoire.)

> **websocket-client retiré, remplacé par `websockets`** : le mode continu utilise le WebSocket WhisperLive natif (paquet Arch `python-websockets`).

---

## 6. Gestion Wayland
- Hotkeys : groupe input requis (usermod -aG input), lecture passive /dev/input/event*, mapping hotkey→keycode evdev, suivi modificateurs, push-to-talk/toggle, pièges : conflits KDE (préférer F8/ctrl+space), repeat value=2, multi-claviers, s'appuyer sur KEY_*.
- Injection : wl-copy/wl-paste via pyperclip ; Ctrl+V : ydotool (daemon ydotoold, systemctl --user) → wtype → evdev UInput ; délais 50-100 ms entre wl-copy et Ctrl+V ; restaurer l'ancien contenu même vide ; erreur ydotoold absent → fallback + message clair.

---

## 7. Découpage des tâches (phases ordonnées)

| Phase | Description | Statut | Détail |
|-------|-------------|--------|--------|
| P0 | Fondations (backend+test) | ✅ | Squelette client/+server/, constants, logging, schemas, config, conftest, requirements → pytest test_core_config vert, import app OK. |
| P1 | Serveur Docker (devops) | ✅ | compose whisper-live (`hwdsl2/whisper-live-server:cuda`, 2 ports 8000/9090, env WHISPERLIVE_*, GPU), .env.example, README déploiement, test.wav + smoke_test.sh → compose up, sonde /docs, transcription curl verbose_json, bind mount hf-hub-cache → /var/lib/whisper-live, API_KEY désactivé. |
| P2 | Client HTTP (backend) | ✅ | transcriber_client → encode_wav, transcribe, ping, list_models → tests MockTransport (multipart, parse, mapping erreurs, list_models). |
| P3 | Audio+Hotkeys (backend) | ✅ | audio.py (on_level waveform + on_chunk mode continu) + hotkeys evdev → tests device simulé + waveform + on_chunk + test réel F8. |
| P4 | Injecteur (backend) | ✅ | injector Wayland → tests mockés (wl-copy→Ctrl+V→restauration, keep_in_clipboard, inject=false, fallbacks ydotool/wtype/UInput) + test réel. |
| P5 | Moteur (backend+test) | ✅ | state, config_apply, dictation (batch + continu WebSocket WhisperLive), whisperlive_client → tests engine cycle de vie batch + continu WS + fallback + apply_config. |
| P6 | API locale (backend+test) | ✅ | factory/dependencies/lifespan/routes (config/engine/history/devices/server/status/test)/websocket → tests routes. |
| P7 | Frontend (frontend+test) | ✅ | index.html + app.js + style.css → pleine largeur, 2 colonnes, courbe audio auto-gain, sélecteur modèle, transcription en direct, toggle mode continu, bouton test connexion (valeurs formulaire), node --check + test manuel. |
| P8 | Intégration E2E (tous) | ✅ | Mode batch + mode continu WebSocket WhisperLive validés en E2E réel sur CachyOS + serveur Unraid LAN réel (voir §12). |

---

## 8. Risques et mitigations

| ID | Risque | Mitigation | Statut |
|----|--------|------------|--------|
| R1 | evdev permissions /dev/input | Message clair + doc usermod input + setup.sh | ✅ |
| R2 | Conflit hotkey KDE | Mapping codes bruts, F8/ctrl+space | ✅ |
| R3 | ydotool absent | Fallback wtype/UInput + message actionnable + setup.sh --with-inject-tools | ✅ |
| R4 | Délai Wayland presse-papier | Délais 50-100 ms | ✅ |
| R5 | Modèle non réglable par requête | Le modèle est **fixé côté serveur** (`WHISPERLIVE_MODEL`) ; le paramètre REST `model` est accepté mais ignoré. Client inchangé (`model=large-v3-turbo`). | ✅ |
| R6 | Alias large-v3-turbo | Supporté par whisper-live (alias `turbo` → repo mobiuslabsgmbh/faster-whisper-large-v3-turbo). | ✅ |
| R7 | Premier démarrage long | Bind mount persistant + doc + `whisper_live_manage --downloadmodel large-v3-turbo` pour pré-télécharger | ✅ |
| R8 | Latence LAN | Timeout configurable, pas de retry auto | ✅ |
| R9 | PortAudio/PipeWire micro non routé | portaudio pipewire-alsa + sélection /api/devices/audio + setup.sh | ✅ |
| R10 | evdev pas de wheel PyPI | Paquet Arch python-evdev + setup.sh | ✅ |
| R11 | Portabilité Unraid↔Proxmox | Bind mount + .env + README | ✅ |
| R12 | API key oubliée | Badge 401 « Tester la connexion » (utilise valeurs formulaire) | ✅ |
| R13 | App non compatible Ctrl+V | inject_text=false (copie seule) | ✅ |
| R14 | Permissions volume Docker | Bind mount + image en root (plus de chown 1000:1000) | ✅ |
| R15 | Partial upgrade pacman | setup.sh vérifie pacman -Qu, propose -Syu, filtre paquets déjà installés | ✅ |
| R16 | Sauvegarde sans modèle | model vide accepté en validation (warning non bloquant frontend) | ✅ |
| R17 | Mode continu non fonctionnel (Realtime speaches) | **RÉSOLU par la migration** : WebSocket WhisperLive natif (VAD serveur) fonctionne ; fallback batch à la relâche si la session WS échoue | ✅ |
| R18 | Courbe audio : pas d'historique défilant | Voir §9 Bugs connus | ⏳ |

---

## 9. Bugs connus / À résoudre

### 9.1 Mode continu — bug Realtime speaches ✅ RÉSOLU (migration whisper-live, archivé)

**Problème d'origine (archivé)** : le serveur speaches 0.8.3 détectait `speech_started` (VAD) mais ne renvoyait jamais `speech_stopped` ni de transcription (`conversation.item.input_audio_transcription.completed`) — bug upstream non corrigé (issue GitHub **#567**, AssertionError VAD côté serveur), projet speaches au ralenti.

**Résolution** : **migration le 2026-08-09** vers `hwdsl2/whisper-live-server` (WhisperLive de Collabora), qui implémente un **vrai mode temps réel** : le client stream le PCM16 sur `ws://{host}:9090/client/ws/speech` et le **VAD serveur** découpe les phrases en segments renvoyés en direct (`{"message": "transcript", "segments": [...]}`). Le mode continu **fonctionne maintenant** (testé en E2E réel : F8 maintenu → texte en direct → relâche → injection).

**Historique des tentatives (avant migration)** :
- WebSocket Realtime speaches (bloqué par le bug #567) → remplacé par un mode « chunked HTTP batch » (segments ~2 s → POST batch) → **remplacé à son tour** par le WebSocket WhisperLive natif.
- Dettes supprimées : `engine/realtime_client.py`, `websocket-client` de requirements.txt, la segmentation HTTP batch dans `dictation.py`, `list_registry`/`download_model` et les routes `/api/server/registry` + `/api/server/models/download`.

**État actuel (mode continu)** :
- Connexion WebSocket (handshake + `server_ready`) en arrière-plan (thread `dictation-ws-connect`) ; envoi binaire PCM16 par le thread sender ; réception des segments par le thread receiver (événement `partial_transcript` en direct).
- À la relâche : EOF → drain des derniers segments (timeout 2,5 s) → injection du texte final + historique.
- **Fallback** automatique sur le batch complet si la session WS échoue (connexion, envoi, réception) — l'audio complet reste toujours accumulé par AudioRecorder : la dictée n'est jamais perdue.
- **Latence cible** : premier segment ~1-3 s après le début de la parole (VAD serveur temps réel, large-v3-turbo FP16 sur RTX 4070).

### 9.2 Visualisation audio — anneau radial « V1 Hub » ✅

**Symptôme** : la courbe audio (waveform à barres miroir) a été remplacée par
l'anneau radial « V1 Hub » validé en mockup : 36 rayons autour d'un cercle
central (canvas 150×150, DPR géré), regroupement RMS des 64 niveaux du payload
`audio`, lissage lerp 0.35 en rAF, couleurs `--accent`/`--accent-glow` lues à
chaque frame (thématisation par état), halo uniquement en enregistrement,
rayons au minimum + opacité réduite quand moteur arrêté. L'anneau est un
indicateur **pur** non cliquable ; le start/stop se fait via le **bouton power
dédié** (`#power-toggle`, rond ~44 px, glyphe ⏻, halo menthe quand moteur ON).

**Ce qui existe** :
- Anneau radial 36 rayons (RMS par groupe, valeurs absolues) ✅
- **Auto-gain glissant + gamma** : voix normale → rayons ~30–90 % ✅
- Lissage lerp 0.35 en rAF + DPR ✅
- Couleurs `--accent`/`--accent-glow` par état (getComputedStyle) ✅
- Halo en enregistrement, rayons minimaux + opacité réduite au repos ✅
- Chrono « Enregistrement · MM:SS » dans le badge de statut ✅
- **Bouton power séparé** (#power-toggle, rond ~44 px, glyphe ⏻, halo menthe quand ON) ✅
- **Zoom global automatique (fit-vp)** : page fit à la hauteur de la fenêtre sans scroll ✅

**À faire** :
- Aucun (le style « sismographe défilant » n'est plus retenu).

---

## 10. État technique actuel

### Serveur (Unraid)
- **Image** : `talky-server` (conteneur maison, `server/server.py` + faster-whisper, build local)
- **GPU** : RTX 4070 12 Go (CUDA, INT8, sm_89)
- **Modèle** : `large-v3-turbo` (TALKY_MODEL, configurable ; INT8 ≈ 0,8 Go VRAM)
- **Interfaces** : REST 8000 (batch) + WebSocket 9090 (temps réel)
- **API_KEY** : optionnel (TALKY_API_KEY, défaut désactivé — LAN privé)
- **Cache** : bind mount `hf-hub-cache` → `/var/lib/whisper-live` (modèles + état) ; image en root (pas de chown)
- **Registry + installation de modèles côté UI** : `GET /v1/registry` + `POST /v1/models` (téléchargement dans le cache)

### Client (CachyOS)
- **OS** : CachyOS (Arch Linux, KDE Plasma, Wayland)
- **Python** : 3.14
- **Tests** : **348 tests verts** (pytest, sans matériel ni serveur)
- **websockets** : remplace `websocket-client` (mode continu WebSocket WhisperLive, paquet Arch `python-websockets`)
- **setup.sh** : script d'installation idempotent avec gestion des paquets Arch (dont python-websockets)

### Dossier ref/
Le dossier `ref/` (projet Windows original qui a servi d'inspiration initiale) a été supprimé. Il n'est plus nécessaire.

---

## 11. Définitions de « done » par phase

| Phase | Critères de « done » | Statut |
|-------|---------------------|--------|
| P0 | client/ et server/ créés, DEFAULT_CONFIG testé (save/load, validation), conftest mocks, pytest test_core_config vert sans matériel ni serveur. | ✅ |
| P1 | compose up (whisper-live :cuda, 2 ports) → sonde /docs ok ; transcription curl test.wav → verbose_json ; down+up → modèle en cache (bind mount /var/lib/whisper-live) ; README Unraid+Proxmox+toolkit ; nvidia-smi visible dans le conteneur ; API_KEY désactivé. | ✅ |
| P2 | encode_wav WAV valide, multipart correct (MockTransport), parse verbose_json, None si vide, TranscriptionError FR, list_models, tests verts. | ✅ |
| P3 | hotkeys evdev tests unitaires + test réel F8 → on_record_start sans bloquer les apps. AudioRecorder on_level (waveform) + on_chunk tests verts. | ✅ |
| P4 | injector tests unitaires (séquence wl-copy→Ctrl+V→restauration, keep_in_clipboard, inject=false, fallbacks) + test réel texte collé et presse-papier restauré. | ✅ |
| P5 | test_engine vert : batch (boot→ready, hotkey→recording→transcribing→success→ready, erreur→error→ready, apply_config) + continu WebSocket WhisperLive (handshake, segments → partial_transcript, EOF + drain → injection, fallback batch sur erreur, toggle mode, cleanup). | ✅ |
| P6 | test_api vert : routes présentes (config, engine, history, devices, server/status, server/test), POST config {saved, reload_needed}, WS hello, /api/server/status (reachable + modèle + device/compute_type), test avec valeurs formulaire ; **registry/download absentes**. | ✅ |
| P7 | node --check app.js OK ; panneau sur 127.0.0.1:8000 ; badge WS ; section Serveur (test connexion avec valeurs formulaire) ; courbe audio auto-gain ; sélecteur modèle ; transcription en direct ; toggle mode continu ; capture raccourci ev.code ; historique live ; sauvegarde sans modèle (warning). | ✅ |
| P8 | Mode batch : checklist E2E complète sur CachyOS réel + serveur LAN réel (dictée push-to-talk→texte injecté, toggle, modèle/langue à chaud, hotkey à chaud, coupure serveur→erreur claire→reprise, arrêt/relance, historique, auto_start, presse-papier restauré). ✅ Mode continu WebSocket WhisperLive : F8 maintenu → texte en direct (partial_transcript) → relâche → injection propre ; coupure serveur en cours de dictée → fallback batch + message clair ; toggle continuous_mode à chaud. ✅ | ✅ |

---

## 12. Prochaines étapes suggérées

1. **Mode continu WebSocket WhisperLive validé en E2E réel** ✅ (fait lors de la migration) : dictée longue avec F8 maintenu → texte qui apparaît pendant l'enregistrement (partial_transcript, VAD serveur) → injection propre à la relâche ; coupure serveur en cours de dictée → fallback batch + message clair ; toggle continuous_mode à chaud.
2. **Visualisation audio — anneau radial « V1 Hub »** ✅ : la courbe sismographe défilante est remplacée par l'anneau radial validé en mockup — 36 rayons (RMS des 64 niveaux, lerp 0.35 en rAF), canvas 150×150 avec DPR, couleurs `--accent`/`--accent-glow` par état, halo en enregistrement, rayons minimaux + opacité réduite au repos, chrono « Enregistrement · MM:SS ». **Évolutions (retour réel)** : l'anneau devient un **indicateur pur** (auto-gain glissant + gamma → voix normale ~30–90 %, non cliquable) et le start/stop passe sur un **bouton power dédié** (#power-toggle, glyphe ⏻, halo menthe quand ON) ; ajout du **zoom global automatique (fit-vp)** pour adapter la page à la hauteur de la fenêtre sans scroll.
3. **Finaliser le frontend** ⏳ : dernière passe sur le panneau après la migration (suppression des références « registry/installation de modèles » restantes côté UI, libellés whisper-live).
4. **Éventuel retour au WebSocket Realtime speaches** : non prévu — le WebSocket WhisperLive natif couvre le besoin (VAD serveur temps réel). Si speaches corrigeait un jour le bug #567, ce ne serait pas une priorité : whisper-live est désormais la cible verrouillée.
