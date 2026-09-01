# Talky CachyOS — Guide d'intégration E2E (phase P8)

**Objectif** : valider l'intégration réelle de « talky » sur le matériel de l'utilisateur :
un poste **CachyOS** (client) + un **serveur Unraid ou Proxmox** avec **RTX 4070 12 Go** (transcription
Whisper `large-v3-turbo`, CUDA, FP16), sur le même LAN.

**Durée totale estimée** : **≈ 1 h 15 de manipulation active** — hors téléchargement initial du
modèle large-v3-turbo (1,62 Go, au **premier client connecté** au serveur) et hors installation
des prérequis GPU (Unraid : plugins + reboot ; Proxmox : passthrough).

**Déroulé** : cochez chaque case au fur et à mesure. En cas d'échec, notez le message exact et les
étapes de reproduction (voir §8). Ce guide ne remplace pas le débogage.

| Étape | Durée | À faire sur |
|---|---|---|
| 0. Préparation | 5 min | poste + serveur |
| 1. Serveur de transcription | 15–30 min | serveur (Unraid / Proxmox) |
| 2. Client CachyOS | 10 min | poste |
| 3. Premier lancement & panneau | 5 min | poste |
| 4. Dictée push-to-talk | 5 min | poste |
| 5. Tests fonctionnels avancés | 10 min | poste |
| 6. Robustesse & erreurs | 10 min | poste + serveur |
| 7. Intégration bureau (optionnel) | 5 min | poste |
| 8. Récapitulatif & validation P8 | 5 min | poste + serveur |

> **Rappel architecture** : le client ne fait aucune inférence — il envoie l'audio au serveur
> whisper-live (`hwdsl2/whisper-live-server`, WhisperLive de Collabora) via REST
> (`POST /v1/audio/transcriptions`, API compatible OpenAI, port 8000) **ou** via WebSocket temps
> réel (port 9090, mode continu), et injecte le texte retourné dans la fenêtre active
> (wl-copy + Ctrl+V).

---

## 0. Préparation (5 min)

- [ ] Ce qu'il faut : poste **CachyOS** (KDE Plasma, Wayland), serveur **Unraid ou Proxmox** avec
      **RTX 4070 12 Go**, les deux sur le **même LAN**.
- [ ] Noter l'IP du **serveur** et celle du **poste** :
      ```bash
      ip a            # chercher l'adresse du LAN (ex. 192.168.1.x)
      ```
- [ ] Vérifier la version des paquets clés du poste (valeurs de référence, **non bloquantes** si
      légèrement différentes) :
      ```bash
      pacman -Q python python-fastapi python-evdev
      ```
      Versions attendues : `python 3.14.x`, `python-fastapi 0.141.1`, `python-evdev 1.9.3`.

---

## 1. Déployer le serveur de transcription (à faire sur le serveur)

### 1.1 Prérequis GPU (selon plateforme)

- [ ] **Unraid** : installer via **Community Apps** les plugins **NVIDIA Driver** puis
      **NVIDIA Container Toolkit**, redémarrer Unraid, puis activer le GPU dans les paramètres
      Docker (menu Docker → Advanced → GPU).
- [ ] **Proxmox** : Docker installé dans la VM/LXC + `nvidia-container-toolkit` + GPU en
      passthrough PCIe (**VM**, IOMMU/VFIO) ou en device passthrough (`/dev/nvidia*`,
      `/dev/dri/renderD128`) — **LXC**. Exemple dans une VM Debian :
      ```bash
      sudo apt update && sudo apt install -y nvidia-driver nvidia-container-toolkit
      sudo nvidia-ctk runtime configure --runtime=docker
      sudo systemctl restart docker
      ```
- [ ] Vérification universelle du toolkit (depuis l'hôte qui lance Docker) — doit afficher la
      **RTX 4070** :
      ```bash
      docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi
      ```
      - Erreur « could not select device driver "nvidia" » → toolkit/runtime non configuré (refaire §1.1).
      - Erreur « Unknown runtime specified: nvidia » → redémarrer Docker.

### 1.2 Lancement

- [ ] Copier le dossier `/projects/talky/server/` sur le serveur (scp, clé USB, partage Samba…).
      ```bash
      scp -r server/ user@<ip-serveur>:/chemin/destination/
      ```
- [ ] Optionnel : créer `.env` avec une clé API (**recommandé si le LAN n'est pas de confiance**).
      Une clé vide = aucune authentification (usage LAN uniquement).
      ```bash
      cd server
      cp .env.example .env
      openssl rand -hex 32        # générer une clé forte
      # éditer .env → TALKY_API_KEY=<clé générée>   (et TALKY_PORT=8000 par défaut)
      ```
- [ ] Lancer le serveur — **1er client connecté : télécharge large-v3-turbo (1,62 Go), patience**.
      ```bash
      docker compose up -d
      docker compose logs -f      # attendre « Talky serveur : REST+WS sur 0.0.0.0:9090 »
      ```
      > Le serveur expose **deux ports** : REST 8000 (batch, `POST /v1/audio/transcriptions`) et
      > WebSocket 9090 (mode continu). Le modèle est **configurable** (`TALKY_MODEL`, défaut
      > `large-v3-turbo`) et peut être **installé depuis le client** : `GET /v1/registry` (liste des
      > modèles disponibles) + `POST /v1/models` (téléchargement dans le cache). Le paramètre REST
      > `model` est honoré (alias faster-whisper ou repo ID complet).
- [ ] Sonde de disponibilité — la sonde applicative est `/docs` (Swagger UI) ou `/openapi.json`
      (FastAPI les sert toujours, y compris avec une clé) ; le serveur expose aussi `/health`,
      sonde sans secret **exemptée d'auth** (utilisée par le healthcheck Docker) :
      ```bash
      curl -fsS -o /dev/null -w '%{http_code}\n' http://<ip-serveur>:8000/health   # → 200, sans clé
      curl -fsS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TALKY_API_KEY" \
           http://<ip-serveur>:8000/docs      # → 200 (header si TALKY_API_KEY est défini)
      curl -fsS -H "Authorization: Bearer $TALKY_API_KEY" \
           http://<ip-serveur>:8000/openapi.json | head -c 200   # header si TALKY_API_KEY défini
      ```
- [ ] Smoke test — transcription de `test.wav` OK (le script lit `TALKY_API_KEY`/`TALKY_WS_PORT` dans
      `.env` automatiquement ; il teste /docs, la transcription puis le WebSocket en option) :
      ```bash
      cd server && ./smoke_test.sh
      # sur le poste, depuis /projects/talky/server :
      ./smoke_test.sh <ip-serveur>
      ```
      Résultat attendu : « Smoke test terminé avec succès ✓ ». Le `test.wav` est une **tonalité
      440 Hz** : l'API REST talky n'applique **pas** de VAD côté serveur → la réponse peut
      être un texte vide ou halluciné ; le succès = HTTP 200 + JSON valide (upload + auth + GPU +
      inférence OK). Pour du vrai texte : `WAV_FILE=/chemin/parole.wav ./smoke_test.sh`.
- [ ] (Bonus) redémarrer le conteneur → le modèle est en cache (bind mount persistant `hf-hub-cache`
      → `/var/lib/whisper-live`) :
      ```bash
      docker compose restart
      ./smoke_test.sh     # doit être nettement plus rapide qu'au 1er run
      ```

---

## 2. Installer le client sur CachyOS

- [ ] Lancer l'installation (répondre **oui** aux prompts AUR pour `python-sounddevice`) :
      ```bash
      cd /projects/talky/client && ./setup.sh
      ```
      Le script installe les paquets système (portaudio, pipewire-alsa, python-fastapi, uvicorn,
      python-httpx, python-websockets, python-numpy, python-evdev, python-pyperclip, wl-clipboard,
      python-pytest),
      crée le venv `.venv` (avec `--system-site-packages`), ajoute l'utilisateur au groupe
      `input` et crée `config.json` depuis le template (jamais modifié s'il existe déjà).
- [ ] Option injection : installer ydotool/wtype maintenant **ou plus tard** :
      ```bash
      ./setup.sh --with-inject-tools
      ```
      (sans eux, l'injection Ctrl+V utilisera le repli UInput intégré, moins robuste).
- [ ] **RECONNEXION de session** si le script a ajouté l'utilisateur au groupe `input`
      (**obligatoire** pour les hotkeys evdev — sinon statut moteur en erreur
      « ajouter l'utilisateur au groupe input »).
- [ ] Vérifier le groupe et les imports :
      ```bash
      id -nG                          # doit contenir « input »
      .venv/bin/python -c "import evdev, sounddevice"   # (ou python3 sans venv)
      ```
      Sans erreur = évdev (paquet Arch) et sounddevice (AUR) sont visibles.
- [ ] Configurer l'IP du serveur : éditer `client/config.json` (`server_url`, défaut
      `http://192.168.1.50:8000` **à adapter**) **OU** via la section « Serveur » du panneau web
      (plus tard, §3).
      ```json
      { "server_url": "http://<ip-serveur>:8000" }
      ```

---

## 3. Premier lancement & découverte du panneau

- [ ] Lancer le client :
      ```bash
      cd /projects/talky/client && ./run.sh
      # ou : python main.py   (écoute sur http://127.0.0.1:8000)
      ```
- [ ] Ouvrir **http://127.0.0.1:8000** dans le navigateur — le design « verre » s'affiche,
      aucun appel réseau externe.
- [ ] Le badge **« Serveur connecté »** est **vert** (mis à jour toutes les 5 s). S'il est rouge :
      vérifier `server_url`, le firewall du serveur et la sonde :
      ```bash
      ping <ip-serveur>
      curl -fsS -o /dev/null -w '%{http_code}\n' http://<ip-serveur>:8000/health   # → 200 (sans clé)
      curl -fsS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TALKY_API_KEY" \
           http://<ip-serveur>:8000/docs   # → 200 (header si TALKY_API_KEY est défini)
      ```
- [ ] Cliquer **« Tester la connexion »** → latence (ms) + **modèle `large-v3-turbo`** + device
      **`cuda`** + compute **`int8`** affichés (champs lecture seule).
- [ ] Vérifier que le **micro** apparaît dans le sélecteur « Périphérique audio » (liste
      `GET /api/devices/audio`). S'il est absent : `pipewire-alsa` manquant (voir §7 du README client).

---

## 4. Test dictée push-to-talk (cœur de l'app)

- [ ] Ouvrir un éditeur (Konsole, Kate…) **au premier plan** (fenêtre active).
- [ ] **Maintenir F8** → parler **3–4 s** → **relâcher** (mode `push_to_talk` par défaut).
- [ ] Le **texte transcrit apparaît injecté** dans l'éditeur (wl-copy + Ctrl+V ; accents OK).
- [ ] Le panneau montre la séquence : `Enregistrement…` → **« Transcription serveur… »** → `Succès`.
- [ ] L'**historique** du panneau contient la transcription (heure · durée · langue).
- [ ] Vérifier que le **presse-papier a été restauré** : coller manuellement (Ctrl+V) → on retrouve
      **l'ancien contenu**, pas le texte dicté — si `keep_in_clipboard=false` (défaut).
      > Si le texte n'est pas injecté : installer ydotool/wtype (§2) ou passer `inject_text=false`
      > (copie seule — l'app cible refuse peut-être Ctrl+V).

---

## 4bis. Test dictée continue — mode continu WebSocket WhisperLive

> Le mode continu **fonctionne maintenant** (depuis la migration vers whisper-live) : le client
> stream l'audio sur le WebSocket du serveur (`ws://<ip-serveur>:9090`) et le **VAD serveur**
> découpe les phrases en temps réel. Par défaut `continuous_mode=true`.

- [ ] Ouvrir un éditeur **au premier plan**.
- [ ] **Maintenir F8** → **parler normalement** (phrase longue, plusieurs phrases) → **ne pas relâcher**.
- [ ] Pendant que F8 est maintenu : le **texte apparaît en direct** dans la zone « Transcription en
      direct » du panneau (événement `partial_transcript`, par segments, VAD serveur) — première
      phrase visible en ~1-3 s après le début de la parole.
- [ ] **Relâcher F8** → le texte final propre (sans doublons) est **injecté** dans l'éditeur et
      ajouté à l'historique.
- [ ] **Fallback** : couper le conteneur serveur pendant une dictée continue (F8 maintenu) → à la
      relâche, repli automatique sur le batch complet : le texte est transcrit quand même (ou
      erreur claire « Serveur injoignable » si le serveur est down). La dictée n'est jamais perdue.
- [ ] **Toggle mode continu** : passer `continuous_mode=false` dans la config → F8 revient au mode
      batch (transcription complète à la relâche) ; `true` → retour au temps réel (à chaud).

---

## 5. Tests fonctionnels avancés

- [ ] **Mode toggle** : passer `input_mode=toggle` → un appui sur F8 démarre l'enregistrement,
      reparler, un second appui l'arrête et transcrit → texte injecté.
- [ ] **Changement modèle/langue à chaud** : modifier `language` (`fr` → `auto` ou `en`) dans le
      panneau → **Enregistrer** → dicter à nouveau → OK **sans redémarrage** (feedback « appliqués
      à chaud »).
- [ ] **Changement de hotkey à chaud** : capturer une nouvelle hotkey (ex. `ctrl+space`) → la
      tester → OK **sans redémarrage** (le raccourci est réinstallé à chaud).
- [ ] **Changement du micro** : sélectionner un autre périphérique audio → **Enregistrer** → le
      moteur redémarre proprement (reload `audio_device`, feedback « redémarrage requis ») → dicter
      → OK.
- [ ] **Historique** : cliquer **« Vider »** → la liste se vide (DELETE `/api/history`).

---

## 6. Robustesse & erreurs

- [ ] **Coupure serveur** : arrêter le conteneur serveur, puis dicter →
      ```bash
      # sur le serveur, dans server/ :
      docker compose stop
      ```
      Le panneau affiche l'état `Erreur` avec le message **« Serveur injoignable — vérifier
      server_url »**, le badge serveur passe **rouge**, puis le moteur revient à `En attente`
      (pas de retry automatique).
- [ ] **Relance serveur** : redémarrer le conteneur, dicter à nouveau → **OK sans redémarrage du
      client** :
      ```bash
      docker compose start      # puis attendre « WhisperLive real-time transcription server is ready »
      ```
- [ ] **Mauvaise API key** : configurer une clé erronée côté client (section « Serveur » ou
      `config.json`) → dicter → message **« Authentification refusée (API key) »** ; remettre la
      bonne clé (identique à `TALKY_API_KEY` du serveur) → dicter → OK.
- [ ] **Hotkey en conflit KDE** : si une touche ne déclenche rien, choisir une touche libre dans les
      raccourcis KDE — **F8** ou **`ctrl+space`** de préférence (capture à chaud, §5).

---

## 7. Intégration bureau (optionnel)

- [ ] **Unité systemd user** — l'app démarre au login :
      ```bash
      cp client/talky.service.example ~/.config/systemd/user/talky.service
      # adapter : WorkingDirectory=/projects/talky/client
      #           ExecStart=/projects/talky/client/.venv/bin/python main.py
      systemctl --user daemon-reload
      systemctl --user enable --now talky.service
      systemctl --user status talky.service
      ```
      Avec `"auto_start": true` dans la config, le moteur démarre aussi automatiquement au login
      (hotkeys actives dès la session).
- [ ] **Panneau depuis le téléphone** (même LAN) : http://<ip-poste>:8000
      > ⚠️ `main.py` écoute sur `127.0.0.1` par défaut. Pour l'accès LAN sans modifier les fichiers,
      > lancer uvicorn avec `--host 0.0.0.0` :
      > ```bash
      > cd /projects/talky/client
      > .venv/bin/uvicorn app.api.factory:build_app --factory --host 0.0.0.0 --port 8000
      > ```

---

## 8. Récapitulatif final & validation P8

- [ ] **Les critères « done » P8 du roadmap §11 sont remplis** :
  - [ ] 1. Dictée push-to-talk → texte injecté dans la fenêtre active
  - [ ] 2. Mode toggle fonctionnel
  - [ ] 3. Modèle/langue changés à chaud
  - [ ] 4. Hotkey changée à chaud
  - [ ] 5. Coupure serveur → erreur claire → reprise
  - [ ] 6. Arrêt / relance du serveur sans redémarrage client
  - [ ] 7. Historique (affichage, effacement)
  - [ ] 8. Auto-start (systemd user / auto_start)
  - [ ] 9. Presse-papier restauré après injection
  - [ ] 10. Serveur déployé et validé sur la plateforme cible (Unraid **ou** Proxmox)
  - [ ] 11. **Mode continu WebSocket WhisperLive** : F8 maintenu → texte en direct
        (partial_transcript) → relâche → injection propre ; fallback batch sur coupure serveur
- [ ] Si un test échoue : noter **l'erreur exacte** (message entre guillemets, sortie console) et
      **les étapes pour la reproduire** (configuration, commandes, ordre des actions) — utile pour
      ouvrir un ticket de débogage.

---

## Pied de page — rappels

- Les **205 tests unitaires** du client passent (aucun matériel ni serveur requis) :
  ```bash
  cd /projects/talky/client && pytest tests/ -q
  ```
- Ce guide **ne remplace pas le débogage** : en cas d'échec d'une étape, ne modifiez pas le code —
  **ouvrez un ticket** avec l'erreur exacte et les étapes de reproduction notées au §8.
- Le serveur reste à jour via `docker compose pull && docker compose up -d` ; le client via
  `./setup.sh` (idempotent, ne modifie jamais un `config.json` existant).
