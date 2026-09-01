# talky — Dictée vocale client/serveur (CachyOS)

## 1. Présentation

Talky transforme votre micro en clavier : appui sur une hotkey → vous parlez → le
serveur Whisper du LAN transcrit → le texte est injecté dans la fenêtre active.
Le client tourne en local (`127.0.0.1:8000`) et délègue toute l'inférence au
serveur distant — **aucun GPU n'est requis côté client**.

Le serveur (`hwdsl2/whisper-live-server`, WhisperLive de Collabora) expose **deux
interfaces** : REST OpenAI-compatible (port 8000, mode batch) et WebSocket temps
réel (port 9090, mode continu).

```
┌─────────────── CLIENT — CachyOS (KDE/Wayland) ───────────────┐   ┌───────────── SERVEUR whisper-live (LAN) ─────────────┐
│  hotkey (F8) → micro 16 kHz → HTTP multipart (batch, 8000) ──┼──▶│  Whisper large-v3-turbo (CUDA, FP16)                 │
│  hotkey (F8) → micro 16 kHz → WebSocket PCM16 (continu, 9090)─┼──▶│  REST 8000 (batch) + WS 9090 (temps réel)            │
│  texte ← wl-copy + Ctrl+V (ydotool → wtype → UInput)         │◀──┼──────────────────────────────────────────────────│
└───────────────────────────────────────────────────────────────┘   └──────────────────────────────────────────────────┘
```

Flux nominal : maintenir F8 → enregistrement → relâcher → envoi au serveur →
réponse → copie du texte (wl-copy) + Ctrl+V dans la fenêtre active → historique
affiché dans le panneau web (WebSocket). En **mode continu** (défaut) : F8
maintenu → l'audio est streamé sur le WebSocket 9090 et le texte apparaît **en
direct** (VAD serveur, événement `partial_transcript`) → relâcher → injection du
texte final (avec repli automatique sur le batch si la session WebSocket échoue).

## 2. Prérequis

- **CachyOS / Arch Linux** (Python 3.14 par défaut) ;
- **KDE Plasma / Wayland** recommandé (injection via wl-clipboard + ydotool/wtype) ;
- **Accès LAN** au serveur de transcription whisper-live (déjà déployé — voir
  [`../server/README.md`](../server/README.md) pour le déploiement et `TALKY_API_KEY`).

## 3. Installation

### 3.1 Paquets système (dépôts Arch)

```bash
sudo pacman -S portaudio pipewire-alsa python-fastapi uvicorn python-httpx python-websockets python-numpy python-evdev python-pyperclip wl-clipboard
```

### 3.2 AUR

```bash
paru -S python-sounddevice
# ou : yay -S python-sounddevice
```

### 3.3 Optionnel — injection Ctrl+V

```bash
sudo pacman -S ydotool          # et/ou : wtype
systemctl --user enable --now ydotool   # daemon ydotoold requis pour ydotool
```

L'injection du texte suit une chaîne de repli : **ydotool** → **wtype** → UInput
evdev intégré (aucun daemon). ydotool est le plus fiable sous KDE/Wayland ; sans
lui, le repli UInput pur Python fonctionne mais peut être moins robuste.

### 3.4 Groupe `input` (indispensable pour les hotkeys)

```bash
sudo usermod -aG input $USER
```

Puis **déconnexion / reconnexion** (ou reboot). Sans ce groupe, le client ne peut
pas lire `/dev/input/event*` : les hotkeys ne réagissent pas et le moteur passe en
erreur « ajouter l'utilisateur au groupe input ».

### 3.5 Dépendances Python

**Option A — paquets Arch (recommandé, sans venv)** : tout est déjà installé par
les commandes ci-dessus. Rien à faire de plus.

**Option B — venv** :

```bash
cd client
python -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> `--system-site-packages` est nécessaire : `python-evdev` n'a **pas de wheel
> PyPI** pour Python 3.14 (paquet Arch obligatoire) et `python-sounddevice` ne se
> trouve que dans l'AUR — le venv doit donc voir les paquets système.

## 4. Configuration

La configuration vit dans `client/config.json` (créé avec les défauts au premier
lancement) **ou** se modifie dans le panneau web (section Configuration) — les deux
écrivent le même fichier. La plupart des champs s'appliquent **à chaud** ; changer
`audio_device` redémarre le moteur.

```json
{
  "server_url": "http://192.168.1.50:8000",
  "server_api_key": "",
  "server_timeout": 30,
  "ws_port": 9090,
  "model": "large-v3-turbo",
  "language": "fr",
  "task": "transcribe",
  "vad_filter": true,
  "hotkey": "f8",
  "input_mode": "push_to_talk",
  "audio_device": null,
  "inject_text": true,
  "add_space": true,
  "keep_in_clipboard": false,
  "auto_start": false,
  "max_history": 50,
  "continuous_mode": true
}
```

| Clé | Défaut | Description |
|---|---|---|
| `server_url` | `http://192.168.1.50:8000` | URL du serveur whisper-live sur le LAN (**à adapter** à votre réseau) |
| `server_api_key` | `""` | Clé API Bearer (vide = pas d'auth ; sinon identique à `TALKY_API_KEY` du serveur) |
| `server_timeout` | `30` | Timeout de lecture httpx en secondes (≥ 5) |
| `ws_port` | `9090` | Port WebSocket whisper-live (mode continu temps réel, ≥ 1) |
| `model` | `large-v3-turbo` | Modèle Whisper (défaut serveur `TALKY_MODEL` ; le paramètre REST est honoré — alias faster-whisper ou repo ID complet) |
| `language` | `fr` | Langue attendue ; `auto` = auto-détection côté serveur |
| `task` | `transcribe` | `transcribe` ou `translate` (traduction en anglais) |
| `vad_filter` | `true` | Filtre VAD (détection de parole) |
| `hotkey` | `f8` | Raccourci clavier : `f8`, `ctrl+space`, `ctrl+alt+f9`… |
| `input_mode` | `push_to_talk` | `push_to_talk` (maintenir) ou `toggle` (appui simple) |
| `audio_device` | `null` | Index du micro (`null` = défaut ; liste via `/api/devices/audio` ou le panneau) |
| `inject_text` | `true` | Coller le texte via Ctrl+V dans la fenêtre active |
| `add_space` | `true` | Ajouter une espace après le texte |
| `keep_in_clipboard` | `false` | Conserver le texte dans le presse-papier (sinon restauration de l'ancien contenu) |
| `auto_start` | `false` | Démarrer le moteur dès le lancement de l'app |
| `max_history` | `50` | Nombre max d'entrées d'historique |
| `continuous_mode` | `true` | `true` = mode continu WebSocket WhisperLive (texte en direct, VAD serveur) ; `false` = batch complet à la relâche |

## 5. Lancement

```bash
cd client
python main.py
```

Puis ouvrir **http://127.0.0.1:8000** dans le navigateur. Le panneau affiche
l'état du moteur, l'historique en direct, la configuration et la section
« Serveur » (URL, clé API, timeout, bouton « Tester la connexion », badge
« Serveur connecté/déconnecté »).

> **Accès LAN au panneau** : le WebSocket `/ws` du client n'accepte que les
> origines `http://127.0.0.1:8000` et `http://localhost:8000`. Pour ouvrir le
> panneau depuis un autre appareil (ex. téléphone), lancer uvicorn avec
> `--host 0.0.0.0` **et** définir `TALKY_ALLOWED_ORIGINS` (liste CSV des
> origines autorisées, ex. `http://192.168.1.50:8000`).

### Démarrage au login (systemd user)

Exemple d'unité `~/.config/systemd/user/talky.service` :

```ini
[Unit]
Description=Talky — dictée vocale client/serveur
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/projects/talky/client
ExecStart=/usr/bin/python /projects/talky/client/main.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now talky
```

Avec `"auto_start": true` dans la config, le moteur démarre **automatiquement au
login** (hotkeys actives dès la session). Sans `auto_start`, cliquez sur
« Démarrer » dans le panneau.

## 6. Utilisation

- **Push-to-talk** : maintenez la hotkey (F8 par défaut) pour parler, **relâchez**
  pour transcrire et injecter le texte.
- **Mode continu** (défaut) : en maintenant F8, le texte apparaît **en direct**
  dans la zone « Transcription en direct » (WebSocket 9090, VAD serveur) ; à la
  relâche, le texte final est injecté. Repli automatique sur le batch si la
  session WebSocket échoue.
- **Toggle** : un appui démarre l'enregistrement, le second l'arrête et transcrit.
- Le texte est copié dans le presse-papier (wl-copy) puis collé (Ctrl+V) dans la
  **fenêtre active** — y compris les accents (é, è, à, ç…).
- Le panneau web montre en temps réel : l'état du moteur (ready / recording /
  transcribing / success / error), l'historique des transcriptions, la config et
  le **statut du serveur** (modèle, device `cuda`, compute_type `int8`).

## 7. Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| Les hotkeys ne réagissent pas | Utilisateur absent du groupe `input` ; session non redémarrée ; conflit avec un raccourci KDE | `sudo usermod -aG input $USER` + reconnexion ; choisir une touche libre (F8, `ctrl+space`) dans KDE |
| « Serveur déconnecté » dans le panneau | `server_url` incorrect ; serveur éteint ; clé API absente/erronée | Vérifier `server_url` ; sonder `curl http://<ip>:8000/health` (200 sans clé) ou `curl -H "Authorization: Bearer $TALKY_API_KEY" http://<ip>:8000/docs` (header si TALKY_API_KEY est défini) ; « Tester la connexion » ; aligner la clé API |
| Micro muet | `pipewire-alsa` manquant ; mauvais micro sélectionné | Installer `pipewire-alsa` ; sélectionner le micro dans le panneau (liste `GET /api/devices/audio`) |
| Texte non injecté | `inject_text` désactivé ; ydotool/wtype absents ; l'app cible refuse Ctrl+V | Activer `inject_text` ; installer `ydotool` (+ daemon) ou `wtype` ; si l'app ne colle pas, passer `inject_text: false` (copie seule) |
| `401 Unauthorized` | `server_api_key` ≠ `TALKY_API_KEY` du serveur | Aligner la clé (voir [`../server/README.md`](../server/README.md)) |
| « Le serveur a mis trop de temps à répondre » | Latence LAN ou premier chargement du modèle | Augmenter `server_timeout` ; attendre la fin du téléchargement initial (bind mount `hf-hub-cache` → `/var/lib/whisper-live`, au 1er client connecté) |

## 8. Tests

```bash
cd client
pytest tests/ -q
```

**205 tests** — aucun matériel ni serveur requis : tout est mocké (evdev,
sounddevice, pyperclip, httpx, subprocess, websocket). Couvre config (dont
`ws_port`), audio, hotkeys, injecteur, client de transcription (REST),
client WebSocket WhisperLive (handshake, PCM16, EOF), moteur (batch + continu)
et API locale.
