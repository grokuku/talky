#!/usr/bin/env bash
# =============================================================================
# run.sh — Lancement du client « talky »
#
#   ./run.sh
#
# - Active le venv (.venv) s'il existe, sinon python3 système.
# - Vérifie l'appartenance au groupe 'input' (hotkeys evdev) avant de lancer
#   et affiche un avertissement clair sinon.
# - Lance `python main.py` : serveur web local sur http://127.0.0.1:8000
#   (uvicorn, écoute sur 127.0.0.1 — pas besoin de root).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Couleurs ----------------------------------------------------------------
if [ -t 1 ]; then
  YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  YELLOW=""; CYAN=""; RED=""; RESET=""
fi

info() { printf '%s[INFO]%s %s\n'        "$CYAN"   "$RESET" "$*"; }
warn() { printf '%s[ATTENTION]%s %s\n'   "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%s[ERREUR]%s %s\n'      "$RED"    "$RESET" "$*" >&2; exit 1; }

# --- Groupe 'input' (hotkeys evdev) ------------------------------------------
if id -nG "${USER:-$(id -un)}" | tr ' ' '\n' | grep -qx 'input'; then
  info "Groupe 'input' : OK (lecture des hotkeys evdev possible)."
else
  warn "Vous n'êtes pas dans le groupe 'input' : les raccourcis clavier (F8)"
  warn "ne fonctionneront PAS. Pour corriger :"
  warn "    sudo usermod -aG input \$USER"
  warn "puis RECONNEXION obligatoire avant de relancer."
fi

# --- Choix de l'interpréteur Python ------------------------------------------
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PY="$SCRIPT_DIR/.venv/bin/python"
  info "Utilisation du venv : .venv/bin/python"
else
  PY="python3"
  info "Aucun venv détecté — utilisation de python3 système."
fi
command -v "$PY" >/dev/null 2>&1 || die "Interpréteur introuvable : $PY"

# --- Lancement ---------------------------------------------------------------
info "Lancement de Talky — arrêt avec Ctrl+C."
exec "$PY" main.py
