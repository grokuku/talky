#!/usr/bin/env bash
# =============================================================================
# setup.sh — Installation & configuration du client « talky » (CachyOS / Arch)
#
# Usage :
#   ./setup.sh                        # recommandé : venv + paquets système
#   ./setup.sh --no-venv              # utilise les paquets Python système
#   ./setup.sh --with-inject-tools    # installe aussi ydotool/wtype (Ctrl+V)
#   ./setup.sh --assume-yes           # répond « oui » aux questions (AUR)
#   ./setup.sh --no-update            # usage avancé : ignore la vérification de mise à jour
#   ./setup.sh --assume-uptodate      # alias de --no-update
#   ./setup.sh --allow-partial        # usage avancé : continue même si système non à jour (risque)
#   ./setup.sh --help
#
# Prérequis :
#   - CachyOS / Arch Linux (KDE Plasma, Wayland) — Python 3.14 conseillé
#   - sudo configuré pour l'utilisateur courant
#   - le serveur de transcription est DISTANT (rien à installer localement)
#
# Le script est IDEMPOTENT : relançable sans risque. Il ne modifie JAMAIS un
# config.json existant.
#
# Pièges gérés (voir §5.11 et §6 du roadmap.md) :
#   - python-evdev : PAS de wheel PyPI → paquet Arch obligatoire (R10)
#   - python-sounddevice : absent des dépôts officiels → AUR (paru/yay) (R9)
#   - pipewire-alsa : obligatoire pour router ALSA → PipeWire (micro)
#   - wl-clipboard : requis par pyperclip sous Wayland
#   - groupe input : requis pour les hotkeys evdev (R1)
#   - partial upgrade : le script ne fait JAMAIS `pacman -Sy` puis `-S` ; il
#     vérifie `pacman -Qu` et propose `sudo pacman -Syu` avant d'installer.
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 0) Chemins & paramètres modifiables
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
CONFIG_FILE="$SCRIPT_DIR/config.json"
CONFIG_TEMPLATE="$SCRIPT_DIR/config.template.json"
VENV_DIR="$SCRIPT_DIR/.venv"

# Options (surpassables en ligne de commande)
USE_VENV=true
WITH_INJECT_TOOLS=false
ASSUME_YES=false

# Paquets système installés via pacman (voir §5.11 + §6 du roadmap.md).
PACMAN_PKGS=(
  portaudio            # dépendance C de python-sounddevice
  pipewire-alsa        # route ALSA -> PipeWire : OBLIGATOIRE pour le micro (R9)
  python-fastapi       # fastapi (dépôt officiel Arch)
  uvicorn              # ⚠ paquet nommé `uvicorn` (pas python-uvicorn)
  python-httpx         # client HTTP -> serveur whisper-live (REST 8000)
  python-websockets    # client WebSocket temps réel -> WhisperLive (port 9090)
  python-numpy         # numpy (dépôt officiel Arch)
  python-evdev         # ⚠ PAS de wheel PyPI : paquet Arch obligatoire (R10)
  python-pyperclip     # presse-papier (pyperclip 1.11.0)
  wl-clipboard         # wl-copy/wl-paste — requis par pyperclip sous Wayland
  python-pytest        # tests : `pytest tests/` (205 tests, sans matériel)
  # pipewire-pulse     # souvent déjà présent avec Plasma ; à ajouter si besoin
)

# Options de mise à jour / installation pacman.
# Règle Arch : JAMAIS de `pacman -Sy` puis `-S` (partial upgrade interdit — peut
# casser les dépendances, ex. pipewire 1.6.8-1.1 installé vs 1.6.8-1 aux dépôts).
# On vérifie donc `pacman -Qu` avant d'installer et on propose `sudo pacman -Syu`.
SKIP_UPDATE_CHECK=false      # --no-update / --assume-uptodate : ne pas vérifier
ALLOW_PARTIAL=false          # --allow-partial : continuer même si système non à jour
UPDATE_NOTE="vérification de mise à jour non effectuée."

# Drapeaux pacman / AUR — modifiables selon vos besoins.
PACMAN_INSTALL_FLAGS=(-S --needed --noconfirm)
AUR_INSTALL_FLAGS=(-S --needed --noconfirm)

# ---------------------------------------------------------------------------
# 1) Utilitaires d'affichage & helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

log()  { printf '%s[INFO]%s %s\n'      "$GREEN"  "$RESET" "$*"; }
info() { printf '%s[INFO]%s %s\n'      "$CYAN"   "$RESET" "$*"; }
warn() { printf '%s[ATTENTION]%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
err()  { printf '%s[ERREUR]%s %s\n'    "$RED"    "$RESET" "$*" >&2; }
die()  { err "$*"; exit 1; }

STEP_N=0
step() { STEP_N=$((STEP_N + 1)); info "── Étape $STEP_N : $* ──"; }

# Demande de confirmation interactive (retour 0 = oui). --assume-yes force oui.
confirm() {
  [ "$ASSUME_YES" = true ] && return 0
  local ans
  printf '%s [o/N] ' "$1"
  read -r ans
  case "$ans" in
    o|O|y|Y|oui|Oui|yes|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

# Détecte le helper AUR installé (paru préféré, sinon yay). Retourne 1 si aucun.
detect_aur_helper() {
  if command -v paru >/dev/null 2>&1; then printf 'paru'; return 0; fi
  if command -v yay  >/dev/null 2>&1; then printf 'yay';  return 0; fi
  return 1
}

# ---------------------------------------------------------------------------
# 2) Préparation : sudo, utilisateur réel, binaires de base
# ---------------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  REAL_USER="${SUDO_USER:-root}"
else
  command -v sudo >/dev/null 2>&1 || die "sudo est requis (ou lancez le script en root)."
  sudo -v || die "sudo a échoué — vérifiez vos droits."
  SUDO="sudo"
  REAL_USER="${USER:-$(id -un)}"
fi

PACMAN=(pacman)
[ -n "$SUDO" ] && PACMAN=("$SUDO" pacman)
run_pacman() { "${PACMAN[@]}" "$@"; }

SYS_PKGS_AVAILABLE=true
command -v pacman >/dev/null 2>&1 || SYS_PKGS_AVAILABLE=false
command -v python3 >/dev/null 2>&1 || die "python3 introuvable — installez le paquet 'python' (Arch)."
[ -f "$REQUIREMENTS" ]   || die "requirements.txt introuvable : $REQUIREMENTS"
[ -f "$CONFIG_TEMPLATE" ] || die "Template de config introuvable : $CONFIG_TEMPLATE"

# ---------------------------------------------------------------------------
# 3) Étapes
# ---------------------------------------------------------------------------
check_distro() {
  local id="unknown"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    id="${ID:-unknown}"
  fi
  log "Distribution détectée : ${PRETTY_NAME:-$id} (ID=$id)"
  case "$id" in
    arch|archarm|cachyos)
      log "Cible compatible (Arch-based)."
      ;;
    *)
      warn "Distribution non-Arch détectée (ID=$id). Talky cible CachyOS/Arch."
      warn "Les étapes pacman seront ignorées — installez les paquets équivalents manuellement."
      SYS_PKGS_AVAILABLE=false
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Vérification « système à jour » — AVANT toute installation pacman.
#
# Principe (règle Arch) : on ne fait JAMAIS `pacman -Sy` puis `pacman -S`
# (partial upgrade). À la place :
#   - si `pacman -Qu` est vide  → système synchronisé : `-S --needed` direct ;
#   - si `pacman -Qu` liste des paquets → on propose `sudo pacman -Syu` ;
#   - `--no-update`/`--assume-uptodate` → saute la vérification (usage avancé) ;
#   - `--allow-partial` → autorise l'installation malgré un système non à jour.
#
# NON-BLOCAGE : si l'utilisateur refuse l'upgrade complet, le script ne
# JAMAIS abandonner. Il propose de continuer en n'installant que les paquets
# MANQUANTS (nouveaux) — sans toucher aux paquets existants — ce qui évite le
# risque de partial upgrade. Le filtrage `pacman -Q` dans install_system_pkgs()
# garantit qu'aucun paquet déjà installé n'est passé à pacman.
#
# die() n'est utilisé que dans des cas extrêmes :
#   - `pacman -Syu` lancé mais échoué, ET l'utilisateur refuse de continuer.
# ---------------------------------------------------------------------------

# Affiche un avertissement puis propose de continuer en n'installant que les
# paquets manquants. Retourne 0 si l'utilisateur accepte, 1 sinon.
# $1 = contexte court (« état inconnu » ou « système non à jour »).
offer_continue_missing_only() {
  local context="$1"
  warn "Continuer en n'installant que les paquets MANQUANTS (nouveaux) ?"
  warn "→ Aucun paquet existant ne sera mis à jour (le filtrage pacman -Q protège"
  warn "   contre les conflits de version, ex. pipewire)."
  if confirm "Continuer l'installation des paquets manquants uniquement ?"; then
    warn "Installation des paquets manquants uniquement (pas de mise à jour des paquets existants)."
    UPDATE_NOTE="${context} — installation des paquets manquants uniquement (pas de mise à jour)."
    return 0
  fi
  return 1
}

ensure_system_uptodate() {
  if [ "$SYS_PKGS_AVAILABLE" = false ]; then
    UPDATE_NOTE="paquets système non gérés (distribution non-Arch)."
    return 0
  fi
  if [ "$SKIP_UPDATE_CHECK" = true ]; then
    UPDATE_NOTE="vérification de mise à jour ignorée (--no-update/--assume-uptodate)."
    log "Option --no-update/--assume-uptodate : vérification de mise à jour ignorée."
    return 0
  fi

  log "Vérification de l'état du système (pacman -Qu)…"
  local outdated rc=0
  outdated="$(run_pacman -Qu 2>/dev/null)" || rc=$?

  # pacman -Qu en échec (base de données locale dans un état particulier,
  # lock résiduel, etc.) → état inconnu, mais le système peut très bien être
  # à jour. On AVERTIT (pas d'erreur) et on propose -Syu ; si refus → on continue.
  if [ "$rc" -ne 0 ]; then
    warn "pacman -Qu a échoué : état des mises à jour inconnu."
    warn "Cela peut arriver même sur un système à jour (lock résiduel, base locale particulière)."
    if confirm "Lancer un upgrade complet 'sudo pacman -Syu' maintenant ?"; then
      run_full_upgrade
      return 0
    fi
    # --allow-partial : pas de confirmation supplémentaire, on continue direct.
    if [ "$ALLOW_PARTIAL" = true ]; then
      warn "Option --allow-partial : on continue malgré l'état inconnu du système."
      UPDATE_NOTE="état du système non vérifié (pacman -Qu en échec, --allow-partial)."
      return 0
    fi
    # Sinon : proposer de continuer avec les paquets manquants uniquement.
    if offer_continue_missing_only "État du système inconnu"; then
      return 0
    fi
    die "Abandon : état du système inconnu et vous avez refusé de continuer."
  fi

  if [ -z "$outdated" ]; then
    log "Système à jour : aucune mise à jour en attente (pacman -Qu vide)."
    UPDATE_NOTE="système à jour (pacman -Qu vide)."
    return 0
  fi

  local count
  count="$(printf '%s\n' "$outdated" | sed '/^[[:space:]]*$/d' | wc -l)"
  warn "Votre système n'est pas totalement à jour ($count paquet(s) obsolète(s))."
  warn "Un upgrade partiel (pacman -Sy puis -S) peut casser des dépendances."
  warn "Exemple rencontré : pipewire 1:1.6.8-1.1 installé vs 1:1.6.8-1 aux dépôts →"
  warn "« l'installation de libpipewire casse la dépendance requise par gst-plugin-pipewire »."
  if confirm "Lancer un upgrade complet 'sudo pacman -Syu' maintenant ?"; then
    run_full_upgrade
    return 0
  fi

  # --allow-partial : pas de confirmation supplémentaire, on continue direct.
  if [ "$ALLOW_PARTIAL" = true ]; then
    warn "Option --allow-partial : on continue avec les dépôts actuels malgré le risque."
    UPDATE_NOTE="système non à jour mais --allow-partial accepté (risque assumé)."
    return 0
  fi

  # Sinon : proposer de continuer avec les paquets manquants uniquement.
  if offer_continue_missing_only "Système non à jour"; then
    return 0
  fi
  die "Abandon : système non à jour et vous avez refusé de continuer."
}

# Lance `sudo pacman -Syu` (confirmation déjà donnée) puis re-vérifie -Qu.
# En cas d'échec de l'upgrade, propose de continuer avec les paquets manquants
# uniquement. Termine par die() seulement si l'upgrade échoue ET l'utilisateur
# refuse de continuer. Sinon retourne 0.
run_full_upgrade() {
  log "Exécution de sudo pacman -Syu… (cela peut prendre du temps)"
  if ! run_pacman -Syu --noconfirm; then
    err "L'upgrade complet a échoué (conflits de dépendances ?)."
    if offer_continue_missing_only "Upgrade complet échoué"; then
      return 0
    fi
    die "Upgrade échoué et vous avez refusé de continuer. Corrigez les conflits"
    die "('sudo pacman -Syu' à la main), puis relancez ce script."
  fi
  log "Upgrade complet terminé."

  # Re-vérification : un reste = souvent des paquets ignorés (IgnorePkg/IgnoreGroup).
  local still rc2=0
  still="$(run_pacman -Qu 2>/dev/null)" || rc2=$?
  if [ "$rc2" -eq 0 ] && [ -n "$still" ]; then
    warn "Certains paquets restent « obsolètes » (IgnorePkg/IgnoreGroup dans /etc/pacman.conf ?)."
    warn "On continue : l'installation ciblée reste cohérente avec les dépôts."
  fi
  UPDATE_NOTE="système mis à jour (sudo pacman -Syu exécuté)."
  return 0
}

install_system_pkgs() {
  if [ "$SYS_PKGS_AVAILABLE" = false ]; then
    warn "pas de pacman détecté — installation des paquets système ignorée."
    return 0
  fi

  # Jamais de `pacman -Sy` ici : on vérifie d'abord que le système est à jour.
  ensure_system_uptodate

  # -----------------------------------------------------------------------
  # Filtrage des paquets DÉJÀ installés.
  #
  # Pourquoi ? Sur CachyOS, pipewire-alsa peut être installé en version plus
  # récente que le dépôt « extra » (ex. 1:1.6.8-1.1 installé vs 1:1.6.8-1 au
  # dépôt). Avec `--needed`, pacman devrait l'ignorer, mais le déséquilibre de
  # version déclenche quand même la résolution de dépendances → pacman veut
  # downgrader pipewire → conflit avec gst-plugin-pipewire/pipewire-pulse →
  # blocage. La solution : ne JAMAIS passer à pacman un paquet déjà présent.
  #
  # Méthode : `pacman -Q <paquet>` retourne 0 si installé, non-0 sinon.
  # -----------------------------------------------------------------------
  local missing_pkgs=()
  local present_pkgs=()
  local pkg

  for pkg in "${PACMAN_PKGS[@]}"; do
    if pacman -Q "$pkg" >/dev/null 2>&1; then
      present_pkgs+=("$pkg")
    else
      missing_pkgs+=("$pkg")
    fi
  done

  if [ "${#present_pkgs[@]}" -gt 0 ]; then
    log "Paquets système déjà installés (${#present_pkgs[@]}) : ${present_pkgs[*]}"
    log "→ non passés à pacman (évite le blocage version plus récente que le dépôt)."
  fi

  if [ "${#missing_pkgs[@]}" -eq 0 ]; then
    log "Tous les paquets système sont déjà installés — rien à faire."
    return 0
  fi

  log "Paquets système à installer (${#missing_pkgs[@]}) : ${missing_pkgs[*]}"
  if ! run_pacman "${PACMAN_INSTALL_FLAGS[@]}" "${missing_pkgs[@]}"; then
    echo
    err "L'installation pacman a ÉCHOUÉ (conflit de dépendances probable)."
    err "Cause classique : système non totalement à jour (partial upgrade),"
    err "ex. pipewire 1:1.6.8-1.1 installé vs 1:1.6.8-1 aux dépôts →"
    err "« l'installation de libpipewire casse la dépendance requise par gst-plugin-pipewire »."
    err
    err "Correction recommandée :"
    err "    sudo pacman -Syu"
    err "    ./setup.sh"
    die "Installation interrompue — relancez après un upgrade complet."
  fi
  log "Paquets système manquants installés."
}

install_sounddevice() {
  # Déjà installé (dépôts ou AUR) ?
  if [ "$SYS_PKGS_AVAILABLE" = true ] && pacman -Q python-sounddevice >/dev/null 2>&1; then
    log "python-sounddevice déjà installé : $(pacman -Q python-sounddevice 2>/dev/null)"
    return 0
  fi
  # 1) Dépôts officiels ?
  if [ "$SYS_PKGS_AVAILABLE" = true ] && pacman -Si python-sounddevice >/dev/null 2>&1; then
    log "python-sounddevice présent dans les dépôts officiels → pacman."
    run_pacman "${PACMAN_INSTALL_FLAGS[@]}" python-sounddevice
    return 0
  fi
  # 2) AUR (paru ou yay)
  local helper
  if helper="$(detect_aur_helper)"; then
    log "python-sounddevice absent des dépôts officiels — disponible dans l'AUR ($helper)."
    if [ "$(id -u)" -eq 0 ]; then
      warn "L'AUR ne doit pas être lancé en root. Dans une session utilisateur :"
      warn "    $helper ${AUR_INSTALL_FLAGS[*]} python-sounddevice"
      return 1
    fi
    if confirm "Installer python-sounddevice depuis l'AUR avec $helper ?"; then
      "$helper" "${AUR_INSTALL_FLAGS[@]}" python-sounddevice
      log "python-sounddevice installé depuis l'AUR."
    else
      warn "Installation AUR refusée — le micro (enregistrement 16 kHz) ne fonctionnera pas."
      warn "Vous pourrez relancer plus tard : $helper -S python-sounddevice"
    fi
  else
    warn "python-sounddevice absent des dépôts officiels et AUCUN helper AUR (paru/yay) détecté."
    warn "Installez-le manuellement depuis l'AUR :"
    warn "    paru -S python-sounddevice     (ou : yay -S python-sounddevice)"
    warn "Prérequis : python-cffi (dépendance auto) + portaudio (déjà installé)."
  fi
}

install_inject_tools() {
  local helper
  helper="$(detect_aur_helper || true)"

  # --- ydotool ---------------------------------------------------------------
  if command -v ydotool >/dev/null 2>&1; then
    log "ydotool déjà installé : $(command -v ydotool)"
  elif [ "$SYS_PKGS_AVAILABLE" = true ] && pacman -Q ydotool >/dev/null 2>&1; then
    # Paquet installé mais binaire absent du PATH (edge case).
    log "ydotool déjà installé (paquet pacman) — binaire non dans le PATH, ignoré."
  else
    log "Installation de ydotool (injection Ctrl+V)…"
    if [ "$SYS_PKGS_AVAILABLE" = true ] && pacman -Si ydotool >/dev/null 2>&1; then
      run_pacman "${PACMAN_INSTALL_FLAGS[@]}" ydotool
    elif [ -n "$helper" ]; then
      if [ "$(id -u)" -eq 0 ]; then
        warn "AUR impossible en root — installez ydotool en session utilisateur :"
        warn "    $helper ${AUR_INSTALL_FLAGS[*]} ydotool"
      else
        "$helper" "${AUR_INSTALL_FLAGS[@]}" ydotool
      fi
    else
      warn "ydotool introuvable dans les dépôts ni l'AUR (aucun helper paru/yay)."
      warn "Installez-le manuellement : paru -S ydotool  (ou yay -S ydotool)"
    fi
  fi

  # --- wtype -----------------------------------------------------------------
  if command -v wtype >/dev/null 2>&1; then
    log "wtype déjà installé : $(command -v wtype)"
  elif [ "$SYS_PKGS_AVAILABLE" = true ] && pacman -Q wtype >/dev/null 2>&1; then
    # Paquet installé mais binaire absent du PATH (edge case).
    log "wtype déjà installé (paquet pacman) — binaire non dans le PATH, ignoré."
  else
    log "Installation de wtype (injection Ctrl+V)…"
    if [ "$SYS_PKGS_AVAILABLE" = true ] && pacman -Si wtype >/dev/null 2>&1; then
      run_pacman "${PACMAN_INSTALL_FLAGS[@]}" wtype
    elif [ -n "$helper" ]; then
      if [ "$(id -u)" -eq 0 ]; then
        warn "AUR impossible en root — installez wtype en session utilisateur :"
        warn "    $helper ${AUR_INSTALL_FLAGS[*]} wtype"
      else
        "$helper" "${AUR_INSTALL_FLAGS[@]}" wtype
      fi
    else
      warn "wtype introuvable — installez-le manuellement : paru -S wtype  (ou yay -S wtype)"
    fi
  fi

  # --- Service user ydotool --------------------------------------------------
  if command -v ydotool >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
      warn "Service user ydotool non activé (session root). Dans votre session graphique :"
      warn "    systemctl --user enable --now ydotool"
    elif command -v systemctl >/dev/null 2>&1; then
      log "Activation du service user ydotool…"
      if systemctl --user enable --now ydotool 2>/dev/null \
         || systemctl --user enable --now ydotoold 2>/dev/null; then
        log "Service user 'ydotool' activé (démon ydotoold)."
      else
        warn "Impossible d'activer le service user ydotool (session non graphique ?)."
        warn "À la prochaine session : systemctl --user enable --now ydotool"
      fi
    else
      warn "systemctl absent — démarrez le démon ydotoold manuellement."
    fi
  else
    warn "ydotool non installé — l'injection Ctrl+V utilisera wtype ou l'UInput intégré (injector.py)."
  fi
}

ensure_input_group() {
  if ! id "$REAL_USER" >/dev/null 2>&1; then
    warn "Utilisateur '$REAL_USER' inconnu — vérification du groupe 'input' ignorée."
    return 0
  fi
  if id -nG "$REAL_USER" | tr ' ' '\n' | grep -qx 'input'; then
    log "L'utilisateur '$REAL_USER' est déjà dans le groupe 'input' (hotkeys evdev OK)."
    return 0
  fi
  warn "L'utilisateur '$REAL_USER' n'est PAS dans le groupe 'input' — les hotkeys evdev échoueront."
  log "Ajout de '$REAL_USER' au groupe 'input'…"
  if [ "$(id -u)" -eq 0 ]; then
    usermod -aG input "$REAL_USER"
  else
    sudo usermod -aG input "$REAL_USER"
  fi
  warn "RECONNEXION (déconnexion/connexion) requise pour que le groupe 'input' soit actif."
  warn "Tant que ce n'est pas fait : statut hotkeys = error (voir §6 roadmap.md)."
}

setup_python() {
  log "Python détecté : $(python3 --version)"
  if [ "$USE_VENV" = false ]; then
    log "Mode --no-venv : utilisation des paquets Python système (pacman/AUR)."
    return 0
  fi

  # --- Création / réutilisation du venv --------------------------------------
  if [ -d "$VENV_DIR" ]; then
    log "Venve existant détecté : $VENV_DIR (réutilisé tel quel)."
    if ! "$VENV_DIR/bin/python" -c 'import evdev' >/dev/null 2>&1; then
      warn "evdev n'est pas visible depuis le venv — venv créé sans --system-site-packages ?"
      warn "Recommandé : rm -rf \"$VENV_DIR\" puis relancez ce script."
    fi
  else
    log "Création du venv : $VENV_DIR (avec --system-site-packages)…"
    if ! python3 -m venv --system-site-packages "$VENV_DIR"; then
      die "Échec de création du venv. Sur Arch, le paquet 'python' inclut venv ; vérifiez python3."
    fi
    log "Venve créé."
  fi

  # --- Installation pip (sans les paquets natifs evdev/sounddevice) ----------
  # Pourquoi filtrer ? python-evdev n'a PAS de wheel PyPI pour Python 3.14
  # (R10 roadmap) et python-sounddevice doit être lié à portaudio/pipewire-alsa
  # (paquet AUR). Ces deux-là viennent des paquets système, visibles via
  # --system-site-packages. Les autres (fastapi, uvicorn, httpx, websockets, numpy,
  # pyperclip, pytest) sont installés depuis PyPI avec les versions verrouillées.
  local tmp_req
  tmp_req="$(mktemp)"
  grep -Ev '^[[:space:]]*#|^[[:space:]]*$' "$REQUIREMENTS" \
    | grep -Eiv '(^|[^a-z0-9_-])(evdev|sounddevice)([^a-z0-9_-]|$)' \
    > "$tmp_req" || true

  log "Installation des dépendances pip (versions verrouillées de requirements.txt)…"
  "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip
  "$VENV_DIR/bin/pip" install --disable-pip-version-check -r "$tmp_req"
  rm -f "$tmp_req"
  log "Dépendances pip installées dans le venv."
}

setup_config() {
  if [ -f "$CONFIG_FILE" ]; then
    log "config.json existe déjà — JAMAIS modifié (préservé)."
    log "  → $CONFIG_FILE"
  else
    cp "$CONFIG_TEMPLATE" "$CONFIG_FILE"
    log "config.json créé depuis le template :"
    log "  → $CONFIG_FILE"
    log "    (server_url http://192.168.1.50:8000, ws_port 9090, model large-v3-turbo, language fr,"
    log "     hotkey f8, input_mode push_to_talk)"
  fi
}

verify_imports() {
  local py="$VENV_DIR/bin/python"
  { [ "$USE_VENV" = true ] && [ -x "$py" ]; } || py="python3"

  local err_file
  err_file="$(mktemp)"
  if "$py" -c 'import fastapi, uvicorn, httpx, websockets, numpy, sounddevice, evdev, pyperclip' 2>"$err_file"; then
    log "Toutes les dépendances Python sont importables :"
    log "  fastapi, uvicorn, httpx, websockets, numpy, sounddevice, evdev, pyperclip"
    rm -f "$err_file"
  else
    warn "Certaines dépendances Python ne sont PAS importables :"
    sed 's/^/    /' "$err_file" >&2 || true
    rm -f "$err_file"
    warn "Vérifiez : python-evdev (paquet Arch), python-sounddevice (AUR),"
    warn "          portaudio + pipewire-alsa."
  fi
}

summary() {
  echo
  printf '%s\n' "=================================================================="
  printf '%s\n' "  Récapitulatif — Talky (client CachyOS)"
  printf '%s\n' "=================================================================="
  echo
  info "1) Installé / vérifié :"
  if [ "$SYS_PKGS_AVAILABLE" = true ]; then
    info "   • Paquets système : portaudio, pipewire-alsa, python-fastapi, uvicorn,"
    info "     python-httpx, python-websockets, python-numpy, python-evdev,"
    info "     python-pyperclip, wl-clipboard, python-pytest."
  else
    warn "   • Paquets système : non installés (distribution non-Arch)."
  fi
  info "   • Mise à jour système : ${UPDATE_NOTE}"
  if python3 -c 'import sounddevice' >/dev/null 2>&1; then
    info "   • python-sounddevice : OK (micro 16 kHz)."
  else
    warn "   • python-sounddevice : MANQUANT (AUR) — le micro ne fonctionnera pas."
  fi
  if id -nG "$REAL_USER" 2>/dev/null | tr ' ' '\n' | grep -qx 'input'; then
    info "   • Groupe 'input' : OK."
  else
    warn "   • Groupe 'input' : AJOUTÉ (ou à vérifier) — reconnexion requise pour les hotkeys."
  fi
  if [ "$USE_VENV" = true ]; then
    info "   • Venv Python : $VENV_DIR"
  else
    info "   • Python : mode --no-venv (paquets système)."
  fi

  echo
  info "2) Prochaines étapes :"
  info "   a. Adapter l'URL du serveur si besoin (config.json → server_url, actuellement"
  info "      http://192.168.1.50:8000) — ou via la section « Serveur » du panneau web."
  info "   b. Lancer le client :"
  info "        cd client && ./run.sh"
  info "        (ou : python main.py)"
  info "   c. Ouvrir le panneau web : http://127.0.0.1:8000"
  info "   d. Vérifier les tests (optionnel, sans matériel ni serveur) :"
  info "        cd client && pytest tests/    (205 tests)"

  echo
  warn "3) Pièges à connaître :"
  warn "   • Partial upgrade interdit : le script ne fait JAMAIS 'pacman -Sy' puis"
  warn "     'pacman -S' — il vérifie 'pacman -Qu' et propose 'sudo pacman -Syu' si besoin."
  warn "   • Groupe 'input' : une RECONNEXION est nécessaire après usermod, sinon"
  warn "     les hotkeys (F8) restent en erreur."
  warn "   • pipewire-alsa : indispensable pour router ALSA → PipeWire (micro)."
  warn "   • python-sounddevice : AUR (paru/yay) — absent des dépôts officiels."
  warn "   • python-evdev : paquet Arch obligatoire (pas de wheel PyPI)."
  warn "   • wl-clipboard : requis pour pyperclip sous Wayland (déjà installé)."
  warn "   • Le serveur de transcription est DISTANT : rien à installer localement."
  echo
  printf '%s\n' "=================================================================="
}

usage() {
  cat <<EOF
Usage : $0 [options]

Options :
  --no-venv            Utiliser les paquets Python système (pas de venv .venv)
  --with-inject-tools  Installer ydotool/wtype + activer le service user ydotool
  --assume-yes         Répondre « oui » aux questions (installation AUR)
  --no-update          Usage avancé : ignorer la vérification de mise à jour (pacman -Qu)
  --assume-uptodate    Alias de --no-update
  --allow-partial      Usage avancé : continuer même si le système n'est pas à jour (risque)
  -h, --help           Afficher cette aide

Sans option : venv + paquets système (recommandé pour CachyOS).
Le script ne fait JAMAIS de partial upgrade : il vérifie « pacman -Qu » et
propose « sudo pacman -Syu » avant d'installer si le système n'est pas à jour.
EOF
}

main() {
  printf '%s\n' "================================================================"
  printf '%s\n' "  Talky — Installation du client (CachyOS / Arch, Wayland)"
  printf '%s\n' "================================================================"

  step "Vérification de la distribution"
  check_distro

  step "Paquets système (pacman)"
  install_system_pkgs

  step "python-sounddevice (AUR si absent des dépôts)"
  install_sounddevice

  if [ "$WITH_INJECT_TOOLS" = true ]; then
    step "Outils d'injection (ydotool/wtype)"
    install_inject_tools
  fi

  step "Groupe 'input' (hotkeys evdev)"
  ensure_input_group

  step "Environnement Python (venv / pip)"
  setup_python

  step "Configuration config.json"
  setup_config

  step "Vérification des imports Python"
  verify_imports

  summary
}

# ---------------------------------------------------------------------------
# 4) Analyse des arguments puis exécution
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --no-venv)            USE_VENV=false ;;
    --with-inject-tools)  WITH_INJECT_TOOLS=true ;;
    --assume-yes)         ASSUME_YES=true ;;
    --no-update|--assume-uptodate)  SKIP_UPDATE_CHECK=true ;;
    --allow-partial)                ALLOW_PARTIAL=true ;;
    -h|--help)            usage; exit 0 ;;
    *)
      err "Option inconnue : $1"
      usage
      exit 1
      ;;
  esac
  shift
done

main "$@"
