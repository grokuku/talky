# Rapport — Stack 100 % à jour et sans conflits — App de dictée vocale client/serveur

> ⚠️ **ARCHIVE** — remplacé par **hwdsl2/whisper-live-server** le **2026-08-09**
> (raison : Realtime API speaches cassée, issue GitHub **#567**, projet au ralenti).
> Ce rapport documente les versions verrouillées à l'époque de speaches ; il est conservé
> tel quel à titre historique. Voir `roadmap.md` / `server/README.md` / `client/README.md`
> pour l'état actuel (whisper-live, modèle fixé côté serveur `WHISPERLIVE_MODEL`, pas de
> registry ni d'installation de modèles).

**Date de recherche** : ~août 2026 (env. de test)
**Méthode** : API officielles — PyPI JSON, GitHub Releases/API, GitHub Container Registry (GHCR), Hugging Face API, archlinux.org (dépôts + AUR RPC), docs NVIDIA (CUDA, cuDNN, NVIDIA Container Toolkit). Pages officielles archivées dans `research/docs/` (voir §6).

> Note outillage : les outils `librarian_search` / `librarian_archive` n'étant **pas disponibles** dans cet environnement, la recherche a été faite via requêtes HTTP directes et les pages utiles ont été **archivées localement** dans `research/docs/` (équivalent fonctionnel de librarian_archive).

---

## 1. Tableau des dernières versions stables (avec sources)

### 1.1 Serveur (Docker + GPU RTX 4070 12 Go, Ada sm_89)

| # | Composant | Dernière version stable | Notes / source |
|---|-----------|--------------------------|----------------|
| 1 | **Docker Engine (CE)** | **v29.7.2** (2026-08-06) | https://github.com/moby/moby/releases/tag/docker-v29.7.2 — Arch : `docker 29.7.2-1` (extra) |
| 2 | **NVIDIA Container Toolkit** | **v1.19.1** (2026-05-21) | https://github.com/NVIDIA/nvidia-container-toolkit/releases/tag/v1.19.1 — Arch : `nvidia-container-toolkit 1.19.1-1` en **extra** (⚠ pas AUR) |
| 3 | **faster-whisper** (PyPI) | **1.2.1** (2025-10-31) | https://pypi.org/project/faster-whisper/ — `requires-python >=3.9` — contrainte **`ctranslate2>=4.0,<5`** |
| 4 | **CTranslate2** (PyPI) | **4.8.1** (2026-07-03) | https://pypi.org/project/ctranslate2/ — wheels **cp39 → cp314**, manylinux_2_27/2_28, win, macOS. **CUDA 12.x requis** au runtime + cuDNN. |
| 5 | **faster-whisper-server** | projet renommé → **speaches-ai/speaches v0.8.3** (stable, 2025-09-19) ; `v0.9.0-rc.3` (2025-12-27) | https://github.com/speaches-ai/speaches (ex fedirz/faster-whisper-server, 3576 ⭐) — images GHCR : `ghcr.io/speaches-ai/speaches` |
| 6 | **CUDA** | Dernière NVIDIA : **13.3 Update 1** — **mais CTranslate2 = CUDA 12.x** (wheels build ~12.8, image officielle 12.6.3) | https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/ — Arch : `cuda 13.3.1-1` |
| 7 | **cuDNN** | **9.25.0.15** (Arch extra, 2026-07-30) ; PyPI `nvidia-cudnn-cu12 9.24.0.43` | docs CTranslate2 : « cuDNN ≥8 pour CUDA 12 » ; cuDNN 9.x OK (utilisé par speaches : 9.10.2.21) |
| 8 | **Modèles Whisper CT2** | `Systran/faster-whisper-large-v3` (3,09 Go), `-medium` (1,53 Go), `-small` (0,48 Go) — FP16 en téléchargement ; `dropbox-dash/faster-whisper-large-v3-turbo` (1,62 Go FP16) | https://huggingface.co/Systran ; https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo |
| 9 | **Python (image Docker)** | **3.12** (`requires-python == "3.12.*"` dans pyproject speaches) | https://raw.githubusercontent.com/speaches-ai/speaches/master/pyproject.toml |

### 1.2 Client (CachyOS = Arch Linux, KDE Plasma, Wayland)

| # | Composant | Dernière version | Paquet Arch / AUR | Source |
|---|-----------|------------------|-------------------|--------|
| 10 | **Python** | **3.14.6-1** (défaut Arch, 2026-06-20) ; AUR `python313 3.13.14-1` si besoin | core `python` | https://archlinux.org/packages/core/x86_64/python/ |
| 11 | **fastapi** | **0.141.1** (2026-07-29), `>=3.10` | extra `python-fastapi 0.141.1-1` | https://pypi.org/project/fastapi/ |
| 12 | **uvicorn** | **0.52.1** (2026-08-01), `>=3.10` | extra `uvicorn 0.52.0-1` (⚠ paquet nommé `uvicorn`, pas `python-uvicorn`) | https://pypi.org/project/uvicorn/ |
| 13 | **httpx** | **0.28.1** (2024-12-06), `>=3.8` | extra `python-httpx 0.28.1-7` | https://pypi.org/project/httpx/ |
| 14 | **sounddevice** | **0.5.5** (2026-01-23), `>=3.7` | **AUR** `python-sounddevice 0.5.5-1` (pas dans extra) | https://pypi.org/project/sounddevice/ |
| 15 | **numpy** | **2.5.1** (2026-07-04), `>=3.12` | extra `python-numpy 2.5.1-1` | https://pypi.org/project/numpy/ |
| 16 | **python-evdev** | **1.9.3** (2026-02-05), `>=3.9` | extra `python-evdev 1.9.3-1` (⚠ **pas de wheel PyPI**, source only → paquet Arch obligatoire) | https://pypi.org/project/evdev/ |
| 17 | **pyperclip** | **1.11.0** (2025-09-26) | extra `python-pyperclip 1.11.0-2` | https://pypi.org/project/pyperclip/ |
| 18 | **wl-clipboard** | **2.3.0** (2026-03-23) | extra `wl-clipboard 2.3.0-1` | https://archlinux.org/packages/extra/x86_64/wl-clipboard/ |
| 19 | **ydotool** | **1.0.4** (2023-01-30) | extra `ydotool 1.0.4-2` | https://github.com/ReimuNotMoe/ydotool/releases/tag/v1.0.4 |
| 20 | **wtype** | **0.4** (2022-01-27) | extra `wtype 0.4-2` | https://github.com/atx/wtype/releases/tag/v0.4 |
| — | **portaudio** | **19.7.0-4** (2026-03-19) | extra `portaudio` (deps : alsa-lib, jack — **pas** libpulse) | https://archlinux.org/packages/extra/x86_64/portaudio/ |
| — | **pipewire** | **1.6.8-1** (2026-07-14) | extra `pipewire` | https://archlinux.org/packages/extra/x86_64/pipewire/ |
| — | **python-cffi** | **2.1.1-1** (dép. de sounddevice) | extra `python-cffi` | https://archlinux.org/packages/extra/x86_64/python-cffi/ |
| — | **Driver NVIDIA** | **610.57.04** (2026-08-06) | extra `nvidia-open-dkms 610.57.04-1` / `nvidia-open 610.57.04-2` (module ouvert, recommandé pour Ada) | https://archlinux.org/packages/extra/x86_64/nvidia-open-dkms/ |

---

## 2. Incompatibilités connues & points d'attention

| # | Point | Détail | Impact / parades |
|---|-------|--------|------------------|
| I1 | **Wheels CTranslate2 ≠ embarquées CUDA/cuDNN** | Wheel cp313 manylinux x86_64 = **39,5 Mo**, contient seulement `libctranslate2-*.so`, `libgomp`, `_ext` — pas de cuBLAS/cuDNN | Il faut fournir CUDA 12 + cuDNN au runtime (image Docker ou wheels `nvidia-*`). |
| I2 | **CTranslate2 = CUDA 12.x uniquement** | Docs officielles : « Install CUDA 12.x … cuDNN ≥8 » ; image officielle `ubuntu22.04-cuda12.8` ; pas de support CUDA 13 revendiqué | **Ne pas utiliser CUDA 13** pour le conteneur serveur. Rester sur 12.6.3/12.8/12.9. |
| I3 | **Driver hôte vs CUDA conteneur** | CUDA 12.6.3 → driver ≥ **560.35.05** ; CUDA 12.8 → ≥ 570 ; CUDA 12.9 → ≥ 575.51.03 | Driver Arch **610.57.04** largement suffisant. Le driver hôte doit rester ≥ minimum du CUDA de l'image. |
| I4 | **speaches épingle d'anciennes versions** | `uv.lock` de speaches (master) : **faster-whisper 1.1.1 + ctranslate2 4.5.0** (plus vieux que 1.2.1/4.8.1) | Garanti compatible tel quel. Pour la toute dernière version, reconstruire l'image (`uv sync`) — 1.2.1 exige `ctranslate2>=4.0,<5` → 4.8.1 OK. |
| I5 | **faster-whisper 1.2.1 ↔ CTranslate2 4.8.1** | Contrainte `ctranslate2<5,>=4.0` → 4.8.1 compatible | ✅ Aucun conflit. |
| I6 | **Python 3.14 (défaut Arch) côté client** | Toutes les libs client compatibles : fastapi ≥3.10, uvicorn ≥3.10, httpx ≥3.8, numpy ≥3.12, sounddevice ≥3.7 (+ cffi), pyperclip (pur) ; evdev : paquet Arch compilé pour 3.14 ; CTranslate2 : wheels cp314 | ✅ Rien à bloquer. Ne pas forcer 3.13 sauf besoin spécifique. |
| I7 | **evdev : pas de wheel PyPI** | `evdev 1.9.3` = source uniquement sur PyPI (compilation Cython) | Utiliser le paquet Arch `python-evdev 1.9.3-1` (précompilé pour Python 3.14). |
| I8 | **Permissions evdev** | /dev/input/* sont `root:input` 0660 (udev systemd) | Ajouter l'utilisateur au groupe **`input`** + re-login. Hotkeys globales fonctionnent sous Wayland (lecture raw du device). |
| I9 | **PortAudio Arch : ALSA+JACK, pas PulseAudio** | `portaudio 19.7.0-4` deps : alsa-lib, jack (pas libpulse) | `sounddevice` utilise le host API **ALSA** par défaut. Sous PipeWire : installer **`pipewire-alsa`** (routage ALSA→PipeWire). JACK dispo en alternative. |
| I10 | **pyperclip + Wayland** | `pyperclip 1.11.0` (≥1.9 requis pour Wayland) détecte `wl-copy`/`wl-paste` | Installer **`wl-clipboard 2.3.0`** ; session Wayland active requise (pas de serveur headless). |
| I11 | **Hotkeys : `keyboard` incompatible Wayland** | Le module `keyboard` (Windows/X11) ne marche pas sous Wayland | Remplacer par **python-evdev** (grab raw) — OK sous Wayland. Éviter `grab()` exclusif qui bloque le clavier pour les autres apps. |
| I12 | **Injection de texte sous Wayland** | Ctrl+V simulé = évènement de clavier virtuel ; presse-papier = wl-clipboard | **ydotool 1.0.4** (daemon `ydotoold`, /dev/uinput, service systemd user) ou **wtype 0.4** (fallback). |
| I13 | **numpy 2.5.1 : `requires-python >=3.12`** | numpy 2.5 a abandonné Python < 3.12 | Client en 3.12+ obligatoire → OK avec le 3.14 par défaut d'Arch. |
| I14 | **NVIDIA Container Toolkit ↔ Docker** | Toolkit 1.19.1 supporte Docker Engine / containerd / CRI-O / Podman | Config : `sudo nvidia-ctk runtime configure --runtime=docker` puis `docker run --gpus all`. Compatible Docker 29.7.2. |
| I15 | **Turbo : FP16 en téléchargement, INT8 au chargement** | `large-v3-turbo` = 1,62 Go FP16 (809 M params) ; quantisation INT8 appliquée par CTranslate2 à la volée (VRAM ≈ moitié, ~0,8 Go) | Ne pas confondre taille téléchargée (FP16) et VRAM INT8. |
| I16 | **VRAM RTX 4070 12 Go** | 4 modèles INT8 simultanés ≈ 3,4 Go de poids (large-v3 ~1,55 + medium ~0,77 + small ~0,24 + turbo ~0,81) + workspace | Large marge. Speaches **décharge dynamiquement** (TTL `stt_model_ttl`=300 s) → 1 seul modèle chargé à la fois. |
| I17 | **cuDNN 8 vs 9** | Docs CTranslate2 disent « cuDNN 8 », mais cuDNN **9.x fonctionne** (speaches embarque 9.10.2.21) | Utiliser cuDNN 9.x (version NVIDIA actuelle). |

---

## 3. Recommandations de versions exactes

### Stack SERVEUR (RTX 4070 12 Go, Docker)

| Couche | Version recommandée |
|--------|----------------------|
| Driver NVIDIA (hôte CachyOS) | `nvidia-open-dkms 610.57.04` (module ouvert, Ada) |
| Docker Engine | `docker 29.7.2` (extra) |
| NVIDIA Container Toolkit | `nvidia-container-toolkit 1.19.1` (extra) + `sudo nvidia-ctk runtime configure --runtime=docker` |
| Image serveur | **`ghcr.io/speaches-ai/speaches:0.8.3-cuda-12.6.3`** (stable, multi-arch amd64/arm64) |
| Base image (Dockerfile) | `nvidia/cuda:12.6.3-base-ubuntu24.04` |
| Python dans l'image | 3.12 (fixé par pyproject `==3.12.*`) |
| fast-api/uvicorn inclus | fastapi 0.135.2 / uvicorn ≥0.35 (lock) — non modifiés |
| faster-whisper / CTranslate2 | **1.1.1 / 4.5.0** (lock officiel, garanti) — *ou* 1.2.1 / 4.8.1 si rebuild |
| CUDA / cuDNN au runtime | CUDA **12.6.3** (base) + wheels `nvidia-cudnn-cu12 9.10.2.21`, `nvidia-cublas-cu12 12.8.4.1`, `nvidia-cuda-runtime-cu12 12.8.90` (dans le venv, enregistrées via ldconfig) |
| Quantisation | `WHISPER__COMPUTE_TYPE=int8` (env), `WHISPER__INFERENCE_DEVICE=cuda` |
| Volume modèles | `hf-hub-cache:/home/ubuntu/.cache/huggingface/hub` |
| Pré-chargement multi-modèles | `PRELOAD_MODELS=["Systran/faster-whisper-large-v3","Systran/faster-whisper-medium","Systran/faster-whisper-small","dropbox-dash/faster-whisper-large-v3-turbo"]` |
| Choix du modèle par requête | paramètre OpenAI `model` (ex. `large-v3-turbo`, `large-v3`, `medium`, `small`) — chargement dynamique |
| Dictée temps réel | **`large-v3-turbo`** (recommandé : ~4× plus rapide que large-v3, qualité quasi identique) |

### Stack CLIENT (CachyOS, KDE Plasma, Wayland)

| Composant | Version | Install |
|-----------|---------|---------|
| Python | **3.14.6** (défaut) — ou AUR `python313 3.13.14-1` | core |
| fastapi | 0.141.1 | `pacman -S python-fastapi` |
| uvicorn | 0.52.0 (extra) / 0.52.1 (pip) | `pacman -S uvicorn` |
| httpx | 0.28.1 | `pacman -S python-httpx` |
| numpy | 2.5.1 | `pacman -S python-numpy` |
| sounddevice | 0.5.5 | **AUR** `python-sounddevice` (ou pip) |
| python-evdev | 1.9.3 | `pacman -S python-evdev` |
| pyperclip | 1.11.0 | `pacman -S python-pyperclip` |
| wl-clipboard | 2.3.0 | `pacman -S wl-clipboard` |
| portaudio | 19.7.0 | `pacman -S portaudio` |
| pipewire + alsa | 1.6.8 + `pipewire-alsa` (et `pipewire-pulse`) | `pacman -S pipewire pipewire-alsa pipewire-pulse` |
| python-cffi | 2.1.1 (dép. sounddevice) | `pacman -S python-cffi` |
| ydotool (injection, optionnel) | 1.0.4 | `pacman -S ydotool` + service user |
| wtype (fallback injection) | 0.4 | `pacman -S wtype` |

**Actions post-install (Wayland) :**
1. `sudo usermod -aG input $USER` puis re-login (evdev).
2. `systemctl --user enable --now ydotool` (daemon `ydotoold`, nécessite `/dev/uinput` accessible — udev/groupe `input`).
3. Vérifier `wl-copy`/`wl-paste` dans le PATH (pyperclip les détecte automatiquement).
4. Vérifier que `pipewire-alsa` est actif pour que PortAudio/ALSA route vers PipeWire.

---

## 4. Infos de version importantes découvertes

1. **`fedirz/faster-whisper-server` a été renommé → `speaches-ai/speaches`** (3576 ⭐, push 2026-08-07). Le projet couvre maintenant STT + TTS (kokoro/piper), streaming SSE, API temps réel, chargement/déchargement **dynamique** de modèles. Docs : https://speaches.ai/.
2. **Dernière stable speaches = v0.8.3** (2025-09-19) ; `v0.9.0-rc.3` (2025-12-27) en cours. Tags GHCR : `0.8.3-cuda`, `0.8.3-cuda-12.4.1`, `0.8.3-cuda-12.6.3`, `latest-cuda-12.6.3`, etc.
3. **`large-v3-turbo` est supporté par faster-whisper depuis v1.1.0** (2024-11-21) : alias `turbo` / `large-v3-turbo` → repo HF `mobiuslabsgmbh/faster-whisper-large-v3-turbo` **qui redirige vers `dropbox-dash/faster-whisper-large-v3-turbo`** (1,67 M téléchargements). Modèle 809 M params, FP16 1,62 Go, INT8 ~0,8 Go VRAM → **idéal pour la dictée temps réel**.
4. **Python 3.14 est maintenant la version par défaut d'Arch** (3.14.6, juin 2026) — pas 3.13. Tout l'écosystème requis est compatible (y compris wheels CTranslate2 cp314).
5. **CUDA 13.3 Update 1** est la dernière CUDA stable NVIDIA (driver ≥ 610.43.02) — **mais CTranslate2 reste en CUDA 12.x** : inutile (et non supporté) d'utiliser CUDA 13 dans le conteneur serveur.
6. **`nvidia-container-toolkit` est dans les dépôts officiels `extra`** (1.19.1-1) — pas besoin d'AUR.
7. **`python-evdev` n'a pas de wheel PyPI** (source uniquement) → indispensable de passer par le paquet Arch précompilé pour Python 3.14.
8. **`uvicorn` sur Arch s'appelle `uvicorn`** (pas `python-uvicorn`), et **`python-sounddevice` n'est pas dans extra** (→ AUR 0.5.5-1).
9. **PortAudio Arch est buildé ALSA+JACK** (pas PulseAudio) — couplage avec PipeWire via `pipewire-alsa`.
10. **faster-whisper 1.2.1** : upgrade Silero VAD V6, fix batched inference ; **1.2.0** : support `distil-large-v3.5`, modèles HF privés, révision spécifique.
11. **CTranslate2 4.8.x** : support Gemma4/T5Gemma2, optimisations MKL/softmax — wheels jusqu'à **cp314**.
12. **ydotool 1.0.4** (2023) et **wtype 0.4** (2022) n'ont pas de nouvelle release — ce sont les dernières versions.

---

## 5. Matrice de compatibilité globale (synthèse)

| Lien | Verdict |
|------|---------|
| faster-whisper 1.2.1 ↔ CTranslate2 4.8.1 | ✅ (`ctranslate2>=4.0,<5`) |
| CTranslate2 ↔ CUDA | ✅ CUDA **12.x** uniquement (12.6.3/12.8/12.9) — ❌ CUDA 13 |
| CTranslate2 ↔ cuDNN | ✅ cuDNN ≥8 **ou** 9.x (9.x recommandé) |
| CUDA 12.x ↔ driver NVIDIA | ✅ driver ≥ 560 (12.6.3) / ≥ 570 (12.8) ; Arch 610.57.04 OK |
| sm_89 (RTX 4070 Ada) ↔ CUDA 12 | ✅ supporté |
| faster-whisper-server/speaches ↔ faster-whisper | ✅ lock 1.1.1 ; upgradable 1.2.1 (rebuild) |
| speaches image ↔ CUDA 12.6.3 base + wheels nvidia | ✅ (ldconfig dans le Dockerfile) |
| Python 3.14 ↔ fastapi/uvicorn/httpx/numpy/sounddevice/evdev/pyperclip | ✅ tous |
| Python 3.13 ↔ même jeu | ✅ aussi (AUR python313) |
| pyperclip ≥1.9 ↔ wl-clipboard (Wayland) | ✅ 1.11.0 + wl-clipboard 2.3.0 |
| sounddevice ↔ PortAudio Arch ↔ PipeWire | ✅ via pipewire-alsa (ALSA) ou JACK |
| evdev ↔ Wayland (hotkeys) | ✅ lecture raw /dev/input ; groupe `input` requis |
| ydotool/wtype ↔ Wayland (injection) | ✅ (ydotoold + uinput ; wtype) |
| Docker 29.7.2 ↔ NVIDIA Container Toolkit 1.19.1 | ✅ (`--gpus all`) |
| large-v3-turbo ↔ faster-whisper | ✅ depuis 1.1.0 |

---

## 6. Pages de documentation archivées (dossier `research/docs/`)

| Fichier | Source officielle |
|---------|-------------------|
| `opennmt-ctranslate2-installation.html` | https://opennmt.net/CTranslate2/installation.html |
| `nvidia-cuda-toolkit-release-notes.html` | https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/ |
| `nvidia-container-toolkit-install-guide.html` | https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html |
| `speaches-installation.html` | https://speaches.ai/installation/ |
| `speaches-configuration.html` | https://speaches.ai/configuration/ |
| `speaches-readme.md` | https://github.com/speaches-ai/speaches (README) |
| `speaches-Dockerfile` / `speaches-compose.cuda.yaml` / `speaches-pyproject.toml` / `speaches-config.py` / `speaches-uv.lock` | https://github.com/speaches-ai/speaches (source) |
| `faster-whisper-readme.md` / `faster-whisper-utils.py` | https://github.com/SYSTRAN/faster-whisper |
| `ydotool-readme.md` | https://github.com/ReimuNotMoe/ydotool |
