#!/usr/bin/env bash
#
# smoke_test.sh — vérification du déploiement talky (conteneur maison)
# =============================================================================
# Ce script valide, dans l'ordre :
#   1. la disponibilité de l'API REST    (GET /docs puis /openapi.json)
#   2. une transcription réelle          (POST /v1/audio/transcriptions)
#   3. (optionnel) le WebSocket temps réel — si python3 + module
#      websocket-client sont disponibles (sinon, simple contrôle du port TCP
#      9090).
#
# Utilisation :
#   ./smoke_test.sh [hôte]            # ex. ./smoke_test.sh 192.168.1.10
#                                     # (hôte par défaut : localhost)
#
# Configuration (priorité : variable d'env > .env > défaut) :
#   TALKY_HOST=192.168.1.10    adresse du serveur
#   TALKY_PORT=8000            port REST (défaut 8000)
#   TALKY_WS_PORT=9090         port WebSocket (défaut 9090)
#   TALKY_API_KEY=sk-…         clé API (vide = pas d'authentification)
#   WAV_FILE=/chemin/test.wav  fichier audio à transcrire (défaut : test.wav du
#                              même dossier)
#   MODEL=large-v3-turbo       modèle demandé au niveau API (voir note ci-dessous)
#   LANGUAGE=fr                langue BCP-47 optionnelle (omise si vide)
#
# NOTE sur `MODEL` : le serveur talky utilise le paramètre OpenAI `model`
# pour charger le modèle demandé (alias faster-whisper). Le serveur a aussi
# un modèle par défaut (TALKY_MODEL). La valeur par défaut ci-dessous est
# donc cohérente avec la config serveur (large-v3-turbo).
#
# Prérequis : curl (obligatoire), jq (optionnel, sortie jolie si présent).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Couleurs (désactivées si sortie non-tty) ---------------------------------
if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_INFO=$'\033[36m'; C_RST=$'\033[0m'
else
  C_OK=""; C_ERR=""; C_INFO=""; C_RST=""
fi

# --- Chargement prudent de .env (uniquement les variables TALKY_* et WHISPER_*)
# Ne source PAS le fichier (évite d'exécuter du contenu arbitraire) : on lit les
# lignes "TALKY_XXX=..." et on n'écrase pas une variable déjà exportée.
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      TALKY_HOST|TALKY_PORT|TALKY_WS_PORT|TALKY_API_KEY|WHISPER_MODEL|WHISPER_LANGUAGE)
        if [[ -z "${!key:-}" ]]; then
          # retire guillemets simples/doubles et espace blanc de fin
          value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
          export "$key=$value"
        fi
        ;;
    esac
  done < <(grep -E '^(TALKY|WHISPER)_[A-Z_]+=' "${SCRIPT_DIR}/.env" 2>/dev/null || true)
fi

# --- Paramètres ----------------------------------------------------------------
TALKY_HOST="${TALKY_HOST:-localhost}"
TALKY_PORT="${TALKY_PORT:-8000}"
TALKY_WS_PORT="${TALKY_WS_PORT:-9090}"
TALKY_API_KEY="${TALKY_API_KEY:-}"
WAV_FILE="${WAV_FILE:-${SCRIPT_DIR}/test.wav}"
MODEL="${MODEL:-${WHISPER_MODEL:-large-v3-turbo}}"
LANGUAGE="${LANGUAGE:-${WHISPER_LANGUAGE:-}}"
BASE_URL="http://${TALKY_HOST}:${TALKY_PORT}"
WS_URL="ws://${TALKY_HOST}:${TALKY_WS_PORT}"

AUTH_HEADER=()
if [[ -n "${TALKY_API_KEY}" ]]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${TALKY_API_KEY}")
fi

# --- Préconditions -------------------------------------------------------------
if ! command -v curl >/dev/null 2>&1; then
  echo "${C_ERR}✗ curl est requis${C_RST}" >&2; exit 1
fi
if [[ ! -f "${WAV_FILE}" ]]; then
  echo "${C_ERR}✗ fichier audio introuvable : ${WAV_FILE}${C_RST}" >&2
  echo "  Générer un échantillon : python3 -c \"import wave,math,struct; ...\"" >&2
  exit 1
fi

echo "${C_INFO}==> talky smoke test : ${BASE_URL} (WS ${WS_URL})${C_RST}"
echo "    fichier : ${WAV_FILE} | modèle : ${MODEL} | langue : ${LANGUAGE:-auto} | auth : $([[ -n "${TALKY_API_KEY}" ]] && echo 'Bearer (clé)' || echo 'aucune')"

# --- 1. Disponibilité REST -----------------------------------------------------
# FastAPI sert toujours /docs et /openapi.json — c'est la sonde de readiness
# utilisée par run.sh lui-même. Si une clé API est configurée, ces endpoints
# exigent aussi le header Authorization.
probe_rest() {
  local url="$1" i
  for i in 1 2 3; do
    if curl -fsS -m 10 "${AUTH_HEADER[@]}" "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

echo
echo "${C_INFO}==> 1/3 sonde REST (GET /docs puis /openapi.json)${C_RST}"
if probe_rest "${BASE_URL}/docs"; then
  REST_PROBE="${BASE_URL}/docs"
elif probe_rest "${BASE_URL}/openapi.json"; then
  REST_PROBE="${BASE_URL}/openapi.json"
else
  echo "${C_ERR}✗ API REST injoignable (serveur démarré ? port correct ? clé API ?)${C_RST}" >&2
  echo "  Vérifier : docker compose ps / docker compose logs -f (dossier server/)" >&2
  exit 1
fi
echo "    ${C_OK}OK${C_RST} : ${REST_PROBE}"

# --- 2. Transcription ----------------------------------------------------------
echo
echo "${C_INFO}==> 2/3 POST /v1/audio/transcriptions (model=${MODEL})${C_RST}"

# Paramètres supportés : file, model (alias ou repo HF), language,
# vad_filter, response_format (json / verbose_json).
response="$(curl -fsS -m 300 -X POST "${BASE_URL}/v1/audio/transcriptions" \
  "${AUTH_HEADER[@]}" \
  -F "file=@${WAV_FILE}" \
  -F "model=${MODEL}" \
  ${LANGUAGE:+-F "language=${LANGUAGE}"} \
  -F "response_format=verbose_json" 2>/dev/null)" || {
    rc=$?
    echo "${C_ERR}✗ transcription en échec (curl exit ${rc})${C_RST}" >&2
    if [[ $rc -eq 22 ]]; then
      echo "  HTTP ≥ 400 — ex. 401 Unauthorized (clé API ?) ou 4xx côté serveur." >&2
    fi
    exit $rc
  }

# --- Sortie lisible ------------------------------------------------------------
if command -v jq >/dev/null 2>&1; then
  text="$(jq -r '.text // "(texte vide)"' <<<"${response}")"
  lang="$(jq -r '.language // "-"' <<<"${response}")"
  segs="$(jq -r '.segments | length // 0' <<<"${response}")"
  echo "    ${C_OK}OK${C_RST} — langue détectée : ${lang} | segments : ${segs}"
  echo
  echo "    Transcription : ${text}"
else
  echo "    ${C_OK}OK${C_RST} — réponse brute (jq absent) :"
  echo "${response}"
fi

# --- 3. WebSocket (optionnel) ---------------------------------------------------
echo
echo "${C_INFO}==> 3/3 WebSocket temps réel (${WS_URL})${C_RST}"
if python3 -c 'import websocket' >/dev/null 2>&1; then
  echo "    Test WebSocket via python3 + websocket-client…"
  if python3 - "${WS_URL}" "${WAV_FILE}" "${TALKY_API_KEY}" "${LANGUAGE:-auto}" <<'PYEOF'
import json, sys, time, wave
import websocket

url, wav, api_key, language = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
headers = [f"Authorization: Bearer {api_key}"] if api_key else []
try:
    ws = websocket.create_connection(url, timeout=15, header=headers)
    ws.send(json.dumps({
        "uid": "smoke-test",
        "language": language,
        "model": "large-v3-turbo",
        "use_vad": True,
    }))
    with wave.open(wav, "rb") as w:
        ws.send_binary(w.readframes(w.getnframes()))
    # Le serveur a accepté le handshake : on attend (au plus 8 s) un message ;
    # une tonalité 440 Hz peut ne produire aucun segment, ce n'est pas un échec.
    end = time.time() + 8
    ws.settimeout(2)
    msg = None
    while time.time() < end:
        try:
            m = ws.recv()
            if m:
                msg = m
                break
        except Exception:
            continue
    ws.close()
    print("OK" + (f" — message reçu : {str(msg)[:120]}" if msg else " — connexion stable (aucun segment pour une tonalité)"))
    sys.exit(0)
except Exception as exc:  # noqa: BLE001 — report explicite
    print(f"ECHEC — {exc}")
    sys.exit(1)
PYEOF
  then
    echo "    ${C_OK}OK${C_RST} : handshake WebSocket accepté (détail ci-dessus)"
  else
    echo "    ${C_ERR}✗ échec du test WebSocket${C_RST}"
  fi
else
  # Repli minimal : le port TCP 9090 est-il ouvert (service à l'écoute) ?
  if timeout 5 bash -c "cat < /dev/null > /dev/tcp/${TALKY_HOST}/${TALKY_WS_PORT}" 2>/dev/null; then
    echo "    ${C_OK}OK${C_RST} : port TCP ${TALKY_WS_PORT} ouvert (test WS complet ignoré —"
    echo "           installer python3-websocket-client pour le handshake)"
  else
    echo "    ${C_ERR}✗ port TCP ${TALKY_WS_PORT} fermé (service WebSocket injoignable)${C_RST}"
  fi
fi

echo
echo "${C_OK}==> Smoke test terminé avec succès ✓${C_RST}"
echo "    (une tonalité 440 Hz peut donner un texte vide — l'API REST whisper-live"
echo "     n'applique pas de VAD côté serveur ; le succès valide upload + auth + GPU"
echo "     + inférence. Pour du texte, fournir un vrai fichier de parole via"
echo "     WAV_FILE=...)"
exit 0
