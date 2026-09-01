# Talky — serveur de transcription (conteneur maison)

Serveur de dictée vocale pour **talky** (client : `/projects/talky/client`),
basé sur **faster-whisper** (CTranslate2, CUDA). Remplaçant de
`hwdsl2/whisper-live-server` (limites : `compute_type` non configurable,
tag d'image rolling) et de speaches (Realtime API cassée).

Conteneur **100 % maîtrisé** : on pin les dépendances, on choisit la
précision, on expose les endpoints dont le client a besoin.

## Fonctionnalités

| Endpoint | Rôle |
|---|---|
| `POST /v1/audio/transcriptions` | Transcription batch (OpenAI-compatible, `response_format=verbose_json` supporté) |
| `GET  /docs` | Sonde de disponibilité (FastAPI Swagger) |
| `WS   /` (port WS) | Transcription temps réel (VAD Silero, protocole compatible client talky) |
| `GET  /v1/models` | Modèles présents dans le cache local |
| `GET  /v1/registry?task=…` | Modèles disponibles à l'installation (registry HuggingFace) |
| `POST /v1/models` | **Installation d'un modèle depuis le client** (téléchargement dans le cache, body JSON `{"model": ...}`) |

- **Précision** : `TALKY_COMPUTE_TYPE` (défaut `int8` → ~0,8 Go VRAM pour
  large-v3-turbo sur la RTX 4070 12 Go).
- **Modèle** : `TALKY_MODEL` (défaut `large-v3-turbo`, alias faster-whisper).
- **VAD** : Silero, `TALKY_VAD_THRESHOLD` (0.5) + `TALKY_VAD_SILENCE_MS` (500).
- **Architecture** : un seul port interne (9090) sert REST **et** WebSocket ;
  le docker-compose mappe les ports hôtes 8000 (REST) et 9090 (WS) vers ce
  port unique.

## Démarrage rapide

```bash
cd server
docker build -t talky-server .     # ~5-10 min (torch CUDA ~2,5 Go)
docker compose up -d
docker compose logs -f             # attendre « Talky serveur : REST+WS sur 0.0.0.0:9090 »
```

> 1er client connecté : le modèle `large-v3-turbo` (~1,6 Go) est téléchargé
> dans `/var/lib/whisper-live` (bind mount) puis chargé en INT8. Les appuis
> suivants sont instantanés (modèle gardé en VRAM).

## Configuration (`.env` — voir `.env.example`)

| Variable | Défaut | Rôle |
|---|---|---|
| `TALKY_PORT` | `8000` | Port hôte REST (le client : `server_url`) |
| `TALKY_WS_PORT` | `9090` | Port hôte WebSocket (le client : `ws_port`) |
| `TALKY_MODEL` | `large-v3-turbo` | Modèle par défaut |
| `TALKY_LANGUAGE` | *(vide)* | Langue par défaut (vide = auto) |
| `TALKY_COMPUTE_TYPE` | `int8` | Précision faster-whisper |
| `TALKY_VAD_THRESHOLD` | `0.5` | Seuil de détection de parole |
| `TALKY_VAD_SILENCE_MS` | `500` | Silence pour finaliser une phrase |
| `TALKY_MAX_CLIENTS` | `4` | Sessions WebSocket simultanées |
| `TALKY_API_KEY` | *(vide)* | Clé API optionnelle : si non vide, exige `Authorization: Bearer <clé>` sur REST + handshake WS (vide = aucune auth) |
| `TALKY_LOG_LEVEL` | `INFO` | Niveau de log |
| `HF_CACHE_PATH` | `./hf-hub-cache` | Dossier de cache côté hôte |

## Installation de modèles depuis le client

Le client talky expose la carte « Modèles (installation) » dans le panneau
web : il interroge `GET /v1/registry` (liste des modèles disponibles) puis
installe via `POST /v1/models` avec un body JSON `{"model": ...}`
(téléchargement + mise en cache). Une fois installé, un modèle peut être
choisi comme `TALKY_MODEL` (ou envoyé par requête via le paramètre
`model`).

```bash
# Via API (ex. installer medium) :
curl -X POST http://10.10.0.5:8000/v1/models \
     -H 'Content-Type: application/json' \
     -d '{"model": "medium"}'
# Vérifier l'installation :
curl http://10.10.0.5:8000/v1/models
```

## Test

```bash
./smoke_test.sh 10.10.0.5      # sonde /docs + transcription test.wav + WS optionnel
```

## Dépannage

| Symptôme | Correctif |
|---|---|
| `could not select device driver "nvidia"` | runtime NVIDIA absent → `nvidia-ctk runtime configure` + restart Docker |
| `CUDA out of memory` | VRAM saturée (autre service) → `TALKY_COMPUTE_TYPE=int8` + vérifier `nvidia-smi` |
| `buffer size must be a multiple of element size` au chargement | modèle corrompu → purger le cache : `docker compose down && rm -rf hf-hub-cache/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo && docker compose up -d` |
| Première transcription très lente | téléchargement du modèle au 1er client — attendre (log « Fetching… ») |
| Port 8000 occupé | changer `TALKY_PORT` dans `.env` |

## Sécurité

- **Authentification optionnelle** : par défaut aucune (`TALKY_API_KEY` vide) — usage LAN privé. Ne pas exposer les ports sur Internet sans reverse proxy + HTTPS.
- Si `TALKY_API_KEY` est non vide, le serveur exige `Authorization: Bearer <clé>` sur **toutes** les routes REST (y compris `/docs`, `/openapi.json`) et dans le **handshake WebSocket** (header `Authorization`). Le client talky envoie cette clé via `server_api_key` (config.json).
- **Origines WebSocket (client)** : le panneau web du client n'accepte que les origines `http://127.0.0.1:8000` et `http://localhost:8000` ; pour un accès LAN, ajouter l'origine via `TALKY_ALLOWED_ORIGINS` (liste CSV) côté client.
