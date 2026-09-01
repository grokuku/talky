/* =========================================================================
 * app.js — Frontend du panneau web « Talky » (P7)
 *
 * Communication :
 *   - WebSocket  /ws   : événements temps réel (hello, state, log, transcript)
 *   - REST       /api  : config, contrôle moteur, historique, périphériques,
 *                        section « Serveur » (status + test)
 *
 * Design « verre » sur fond #141821 (palette mint/sky/lavender/peach/rose).
 * Aucune dépendance externe : fonctionne hors-ligne sur le LAN.
 *
 * Theming par état : body[data-status="idle|ready|recording|transcribing|error"]
 * pilote --accent (badge, carte héro, caret) ; plus de curseur « progress ».
 * Visualisation audio : waveform à barres miroir façon messagerie vocale
 * (envelope follower + lerp rAF), avec monitoring animé au repos.
 * ========================================================================= */

"use strict";

// --------------------------------------------------------------------------
// État global de l'application
// --------------------------------------------------------------------------
const app = {
  ws: null,
  connected: false,
  state: null,             // snapshot du moteur (/api/engine, WS)
  config: null,            // configuration courante (/api/config, WS hello)
  history: [],             // transcriptions récentes (plus récentes en premier)
  serverStatus: null,      // /api/server/status (badge + modèles)
  pollingTimer: null,      // repli REST quand la WS est indisponible
  serverPollTimer: null,   // polling badge serveur (toutes les 5 s)
};

const $ = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --------------------------------------------------------------------------
// Icônes SVG inline (zéro dépendance) réutilisées par le JS (boutons copier…)
// --------------------------------------------------------------------------
const ICONS = {
  copy: '<svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  check: '<svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>',
};

// Change le libellé d'un bouton sans écraser son icône SVG éventuelle.
function setBtnLabel(btn, text) {
  const lbl = btn.querySelector(".btn-label");
  if (lbl) lbl.textContent = text;
  else btn.textContent = text;
}
function getBtnLabel(btn) {
  const lbl = btn.querySelector(".btn-label");
  return lbl ? lbl.textContent : btn.textContent;
}

// --------------------------------------------------------------------------
// Aide réseau (REST)
// --------------------------------------------------------------------------
async function api(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let msg = `Erreur HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.errors) {
        // FastAPI renvoie une chaîne JSON (dict champ -> message) ou une liste.
        if (typeof body.errors === "string") {
          try {
            const map = JSON.parse(body.errors);
            msg = Object.entries(map)
              .map(([k, v]) => `${k} : ${v}`)
              .join(" · ");
          } catch { msg = body.errors; }
        } else if (Array.isArray(body.errors)) {
          msg = body.errors.join(" · ");
        } else if (body.errors && typeof body.errors === "object") {
          msg = Object.entries(body.errors)
            .map(([k, v]) => `${k} : ${v}`)
            .join(" · ");
        }
      } else if (body.error) {
        msg = body.error;
      } else if (body.detail) {
        msg = typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail);
      }
    } catch { /* corps non JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

// --------------------------------------------------------------------------
// WebSocket : connexion + reconnexion automatique (avec repli polling)
// --------------------------------------------------------------------------
let wsAttempts = 0;

// Construction défensive de l'URL WebSocket à partir de l'origine de la page.
function wsURL() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  let host = location.host
    || `${location.hostname || "127.0.0.1"}${location.port ? ":" + location.port : ""}`;
  host = String(host).replace(/^\/+|\/+$/g, "");
  host = host.replace(/\/ws[^.]*$/, "");
  return `${scheme}://${host}/ws`;
}

function connectWS() {
  // La WebSocket ne fonctionne que si la page est servie en HTTP(S).
  if (!/^https?:/.test(location.protocol)) {
    console.warn("[talky] Page non servie en HTTP — repli polling uniquement.");
    startPolling();
    return;
  }

  const url = wsURL();
  app.ws = new WebSocket(url);

  app.ws.onopen = () => {
    wsAttempts = 0;
    app.connected = true;
    stopPolling();
    const liveBadge = $("history-live");
    if (liveBadge) liveBadge.classList.remove("hidden");
  };

  app.ws.onclose = () => {
    app.connected = false;
    const liveBadge = $("history-live");
    if (liveBadge) liveBadge.classList.add("hidden");
    startPolling();                      // repli REST tant que la WS est coupée
    wsAttempts += 1;
    const delay = wsAttempts > 3 ? 15000 : 3000;   // backoff après 3 échecs
    setTimeout(connectWS, delay);
  };

  app.ws.onerror = () => {
    console.error(`[talky] WebSocket indisponible : ${url}`);
    app.ws.close();
  };

  app.ws.onmessage = (evt) => {
    let events;
    try { events = JSON.parse(evt.data); } catch { return; }
    const list = Array.isArray(events) ? events : [events];
    for (const ev of list) handleEvent(ev);
  };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "hello":          // message initial du serveur à la connexion
      app.state = ev.data.state;
      app.config = ev.data.config;
      updateState();
      updateMotorInfo();
      renderConfig();
      loadDevices();
      loadHistory();   // non silencieux : remplace le placeholder initial
      break;
    case "state":
      app.state = ev.data;
      updateState();
      updateMotorInfo();
      break;
    case "transcript":
      app.history.unshift(ev.data);
      const max = (app.config && app.config.max_history) || 50;
      if (app.history.length > max) app.history.pop();
      renderHistory();
      // Une transcription finale a été ajoutée à l'historique : on vide la
      // zone « en direct » (le texte verrouillé y réapparaîtra dans l'historique).
      clearLiveTranscript();
      // Annonce ARIA ciblée (la liste d'historique n'est plus aria-live).
      announce(`Nouvelle transcription : ${String((ev.data && ev.data.text) || "").slice(0, 140)}`);
      break;
    case "log":
      appendLog(ev.data);
      break;
    case "audio":
      handleAudio(ev.data);
      break;
    case "partial_transcript":
      handlePartialTranscript(ev.data);
      break;
  }
}

// --------------------------------------------------------------------------
// Repli : polling REST (utilisé si la WebSocket est coupée)
// --------------------------------------------------------------------------
function startPolling() {
  if (app.pollingTimer) return;
  app.pollingTimer = setInterval(async () => {
    try {
      const [state, config, history] = await Promise.all([
        api("/api/engine"),
        api("/api/config"),
        api("/api/history"),
      ]);
      app.state = state;
      app.config = config;
      updateState();
      updateMotorInfo();
      renderConfigIfIdle();
      const hist = Array.isArray(history.history)
        ? history.history.slice().reverse() : [];
      app.history = hist;
      renderHistory();
    } catch { /* serveur injoignable — on attend la reconnexion WS */ }
  }, 2500);
}

function stopPolling() {
  if (app.pollingTimer) {
    clearInterval(app.pollingTimer);
    app.pollingTimer = null;
  }
}

// --------------------------------------------------------------------------
// Affichage de l'état du moteur (badge + barre d'info)
// --------------------------------------------------------------------------
// Mapping état -> libellé + groupe de theming (roadmap §5.10 / DESIGN.md).
// Le groupe alimente body[data-status] : tout le chrome (badge, halo de la
// carte héro, liseré, caret) suit --accent défini en CSS, sans dispersion de
// classes dot-*.
//   idle=gris · ready/success=mint · recording=rose · transcribing=sky · error=rouge
const STATUS_META = {
  idle:         { label: "Hors ligne",             group: "idle",         live: false, pulse: false },
  booting:      { label: "Démarrage…",             group: "transcribing", live: true,  pulse: true },
  ready:        { label: "En attente",             group: "ready",        live: true,  pulse: false },
  recording:    { label: "Enregistrement…",        group: "recording",    live: true,  pulse: true },
  transcribing: { label: "Transcription serveur…", group: "transcribing", live: true,  pulse: true },
  success:      { label: "Succès",                 group: "ready",        live: false, pulse: false },
  error:        { label: "Erreur",                 group: "error",        live: false, pulse: true },
  stopping:     { label: "Arrêt…",                 group: "idle",         live: false, pulse: true },
};

function updateState() {
  const status = app.state && app.state.status;
  const meta = STATUS_META[status] || STATUS_META.error;

  // Theming par état : variable CSS --accent consommée par badge/bordures/halo.
  document.body.dataset.status = meta.group;

  const dot = $("status-dot");
  dot.classList.toggle("pulse", !!meta.pulse);
  dot.classList.toggle("live", !!meta.live);
  $("status-text").textContent = meta.label;
  $("status-detail").textContent = (app.state && app.state.status_msg) || "";

  // Interrupteur On/Off (role="switch" dans index.html)
  const running = !!(app.state && app.state.running);
  const toggle = $("power-toggle");
  toggle.classList.toggle("on", running);
  toggle.setAttribute("aria-checked", String(running));
  $("power-knob").classList.toggle("on", running);

  // NB : plus de curseur « progress » global pendant la transcription —
  // le theming data-status (halo + badge) signale déjà l'état occupé.

  // Zone « Transcription en direct » : visible uniquement en mode continu
  // pendant l'enregistrement (et masquée sinon). Quand l'enregistrement
  // s'arrête (success/ready/error/idle), on vide la zone.
  syncLiveTranscript(status);

  // Waveform : bascule figée/monitoring + libellé selon le nouvel état.
  syncVizMode();
  syncVizLabel();
}

function updateMotorInfo() {
  if (!app.state) return;
  $("info-model").textContent = app.state.model || "—";
  $("info-device").textContent = app.state.device || "—";
  $("info-compute").textContent = app.state.compute_type || "—";
  $("info-hotkey").textContent = app.state.hotkey || "—";
  // Chip kbd de la carte héro (affichage seul, identique au réglage)
  if (app.state.hotkey && $("hero-hotkey")) {
    $("hero-hotkey").textContent = app.state.hotkey;
  }
}

// --------------------------------------------------------------------------
// Remplissage du formulaire de configuration
// --------------------------------------------------------------------------
function renderConfig() {
  const c = app.config;
  if (!c) return;

  $("cfg-server-url").value = c.server_url || "";
  $("cfg-server-api-key").value = c.server_api_key || "";
  $("cfg-server-timeout").value = c.server_timeout ?? 30;
  $("cfg-model").value = c.model || "";
  $("cfg-language").value = c.language || "auto";
  $("cfg-task").value = c.task || "transcribe";
  $("cfg-vad").checked = !!c.vad_filter;
  $("cfg-compute").value = c.compute_type || "int8";
  $("cfg-mode").value = c.input_mode || "push_to_talk";
  $("cfg-continuous").checked = c.continuous_mode != null ? !!c.continuous_mode : true;
  $("cfg-audio-device").value = c.audio_device == null ? "" : String(c.audio_device);
  $("cfg-inject").checked = !!c.inject_text;
  $("cfg-add-space").checked = !!c.add_space;
  $("cfg-keep-clipboard").checked = !!c.keep_in_clipboard;
  $("cfg-auto-start").checked = !!c.auto_start;
  $("cfg-max-history").value = c.max_history ?? 50;
  if ($("hotkey-pick")) $("hotkey-pick").textContent = c.hotkey || "—";
  if ($("hero-hotkey")) $("hero-hotkey").textContent = c.hotkey || "—";
  updateHeroMicLabel();
}

// Version « prudente » : ne touche pas aux champs en cours de saisie
// (évite d'écraser le texte pendant le polling REST).
function renderConfigIfIdle() {
  const el = document.activeElement;
  if (el && (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA")) return;
  renderConfig();
}

// --------------------------------------------------------------------------
// Collecte de la configuration (formulaire complet)
// --------------------------------------------------------------------------
function collectConfig() {
  return {
    server_url: $("cfg-server-url").value.trim(),
    server_api_key: $("cfg-server-api-key").value,
    server_timeout: parseInt($("cfg-server-timeout").value, 10) || 30,
    model: $("cfg-model").value.trim(),
    language: $("cfg-language").value === "auto" ? "auto" : $("cfg-language").value,
    task: $("cfg-task").value,
    vad_filter: $("cfg-vad").checked,
    compute_type: $("cfg-compute").value,
    hotkey: (app.config && app.config.hotkey)
      || ($("hotkey-pick").textContent !== "—" ? $("hotkey-pick").textContent : "f8"),
    input_mode: $("cfg-mode").value,
    continuous_mode: $("cfg-continuous").checked,
    audio_device: $("cfg-audio-device").value === ""
      ? null
      : parseInt($("cfg-audio-device").value, 10),
    inject_text: $("cfg-inject").checked,
    add_space: $("cfg-add-space").checked,
    keep_in_clipboard: $("cfg-keep-clipboard").checked,
    auto_start: $("cfg-auto-start").checked,
    max_history: parseInt($("cfg-max-history").value, 10) || 50,
  };
}

function postConfig(payload) {
  return api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function saveConfig() {
  const btn = $("btn-save");
  const feedback = $("save-feedback");
  const payload = collectConfig();
  btn.disabled = true;
  setBtnLabel(btn, "Enregistrement…");
  feedback.textContent = "";
  feedback.className = "feedback";
  // Warning non bloquant si aucun modèle n'est sélectionné : la sauvegarde
  // proceede quand même (le backend accepte un model vide).
  if (!payload.model) {
    showToast("Attention : aucun modèle sélectionné.");
  }
  try {
    const res = await postConfig(payload);
    if (!res.saved) throw new Error("Sauvegarde refusée par le serveur.");
    app.config = res.config;
    renderConfig();
    if (res.reload_needed) {
      feedback.textContent = "Paramètres enregistrés — redémarrage du moteur en cours…";
      feedback.classList.add("peach");
      showToast("Paramètres enregistrés (redémarrage requis).");
    } else {
      const fields = (res.live_changed && res.live_changed.length)
        ? ` (${res.live_changed.join(", ")})` : "";
      feedback.textContent = `Paramètres enregistrés — appliqués à chaud${fields}.`;
      feedback.classList.add("mint");
      showToast("Paramètres enregistrés (appliqués à chaud).");
    }
    loadServerStatus();          // refresh badge + datalist des modèles
  } catch (err) {
    feedback.textContent = `Erreur : ${err.message}`;
    feedback.classList.add("rose");
    showToast(`Erreur : ${err.message}`);
  } finally {
    btn.disabled = false;
    setBtnLabel(btn, "Enregistrer les paramètres");
  }
}

// Réglages appliqués immédiatement à chaud (champs HOT_FIELDS).
async function quickApply() {
  const payload = {
    input_mode: $("cfg-mode").value,
    continuous_mode: $("cfg-continuous").checked,
    inject_text: $("cfg-inject").checked,
    add_space: $("cfg-add-space").checked,
    keep_in_clipboard: $("cfg-keep-clipboard").checked,
    compute_type: $("cfg-compute").value,
  };
  try {
    const res = await postConfig(payload);
    if (res.saved) {
      app.config = res.config;
      renderConfig();
      showToast("Réglages appliqués à chaud.");
    }
  } catch (err) {
    showToast(`Erreur : ${err.message}`);
  }
}

// --------------------------------------------------------------------------
// Contrôle du moteur (On/Off principal + Redémarrer)
// --------------------------------------------------------------------------
async function togglePower() {
  const running = !!(app.state && app.state.running);
  try {
    if (running) {
      await api("/api/engine/stop", { method: "POST" });
      showToast("Moteur arrêté.");
    } else {
      await api("/api/engine/start", { method: "POST" });
      showToast("Démarrage du moteur…");
    }
  } catch (err) {
    showToast(`Erreur : ${err.message}`);
  }
}

async function restartEngine() {
  try {
    await api("/api/engine/restart", { method: "POST" });
    showToast("Redémarrage du moteur en cours…");
  } catch (err) {
    showToast(`Erreur : ${err.message}`);
  }
}

// --------------------------------------------------------------------------
// Chargement des microphones système (GET /api/devices/audio)
// --------------------------------------------------------------------------
async function loadDevices() {
  const select = $("cfg-audio-device");
  const current = select.value;
  try {
    const data = await api("/api/devices/audio");
    select.innerHTML = "";
    const def = document.createElement("option");
    def.value = "";
    def.textContent = "Périphérique par défaut";
    select.appendChild(def);

    for (const dev of data.devices || []) {
      const opt = document.createElement("option");
      opt.value = String(dev.index);
      opt.textContent = `${dev.index} — ${dev.name} (${dev.channels} canaux)`;
      select.appendChild(opt);
    }

    if (data.error) showToast(data.error);
    if (current && Array.from(select.options).some((o) => o.value === current)) {
      select.value = current;
    }
  } catch (err) {
    select.innerHTML = '<option value="">Aucun micro détecté</option>';
    showToast(`Impossible de lister les micros : ${err.message}`);
  }
  updateHeroMicLabel();
}

// --------------------------------------------------------------------------
// Section « Serveur » : badge + test de connexion + liste des modèles
// --------------------------------------------------------------------------
const COMMON_MODELS = [
  "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
  "Systran/faster-whisper-large-v3",
  "Systran/faster-whisper-medium",
  "Systran/faster-whisper-small",
  "Systran/faster-whisper-base",
  "Systran/faster-whisper-tiny",
];

// Alimente le <select> du modèle avec la liste des modèles supportés.
// La liste vient de /api/server/status (list_models côté serveur talky,
// liste locale en lecture seule) ; si elle est vide (serveur injoignable),
// on retombe sur COMMON_MODELS.
function populateModelSelect(models) {
  const select = $("cfg-model");
  if (!select) return;
  const current = (app.config && app.config.model) || select.value || "";
  const available = (Array.isArray(models) ? models : []).map(String);
  if (!available.length) available.push(...COMMON_MODELS);
  select.innerHTML = "";

  if (!available.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Aucun modèle disponible";
    opt.disabled = true;
    opt.selected = true;
    select.appendChild(opt);
    return;
  }

  let found = false;
  for (const m of available) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    select.appendChild(opt);
    if (m === current) found = true;
  }
  // Si le modèle courant n'est pas dans la liste, on l'affiche quand même (info).
  if (current && !found) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = current + " (non listé)";
    select.appendChild(opt);
  }
  select.value = found ? current : available[0];
}

function updateServerBadge(status) {
  if (status) app.serverStatus = status;
  const s = app.serverStatus;
  const reachable = !!(s && s.reachable);

  const applyBadge = (id, text) => {
    const el = $(id);
    if (!el) return;
    el.classList.toggle("on", reachable);
    el.classList.toggle("off", !reachable);
    const txt = el.querySelector(".badge-text") || el.lastElementChild;
    if (txt) txt.textContent = text;
  };
  applyBadge("server-badge", reachable ? "Serveur connecté" : "Serveur déconnecté");
  applyBadge("server-badge-header", reachable ? "Connecté" : "Déconnecté");

  // Accélération / compute type (côté serveur talky — compute_type vient
  // de la config client, device reste en lecture seule)
  if ($("cfg-device")) $("cfg-device").textContent = s && s.device ? s.device : "cuda";
  // Le select #cfg-compute est piloté par renderConfig (config client) ;
  // on ne l'écrase pas ici avec la valeur du serveur.

  // Liste des modèles supportés (serveur talky) -> <select> du modèle.
  if (s && Array.isArray(s.models)) populateModelSelect(s.models);
}

async function loadServerStatus() {
  try {
    updateServerBadge(await api("/api/server/status"));
  } catch (err) {
    updateServerBadge(null);
  }
}

// --------------------------------------------------------------------------
// Section « Modèles (installation) » — registry + installation (combobox)
// --------------------------------------------------------------------------
let installInFlight = false;

// Liste des modèles disponibles (dicts {id, name, params, vram_int8, repo}
// ou, pour compatibilité, des strings simples).
let registryModels = [];
// Modèle sélectionné dans le dropdown (id). Vide tant que rien n'est choisi.
let registrySelectedId = "";

function _registryModelLabel(m) {
  // Format dictionnaire riche : "Name — params — vram"
  if (m && typeof m === "object" && m.id) {
    const parts = [m.name || m.id];
    if (m.params) parts.push(m.params);
    if (m.vram_int8) parts.push(m.vram_int8 + " VRAM");
    return parts.join(" — ");
  }
  // Compatibilité : simple string
  return String(m || "");
}

function _registryModelId(m) {
  if (m && typeof m === "object" && m.id) return String(m.id);
  return String(m || "");
}

function _filterRegistryModels(query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return registryModels;
  return registryModels.filter((m) => {
    const name = (typeof m === "object" && m.name) ? String(m.name) : "";
    const id = _registryModelId(m);
    return name.toLowerCase().includes(q) || id.toLowerCase().includes(q);
  });
}

function _renderRegistryDropdown(models) {
  const dropdown = $("registry-dropdown");
  if (!dropdown) return;
  dropdown.innerHTML = "";
  if (!models.length) {
    const empty = document.createElement("div");
    empty.className = "combobox-option combobox-empty";
    empty.textContent = "Aucun modèle ne correspond.";
    dropdown.appendChild(empty);
    return;
  }
  for (const m of models) {
    const opt = document.createElement("div");
    opt.className = "combobox-option";
    const id = _registryModelId(m);
    opt.dataset.modelId = id;
    const label = document.createElement("span");
    label.className = "opt-label";
    label.textContent = (typeof m === "object" && m.name) ? m.name : id;
    opt.appendChild(label);
    // Métadonnées (params + vram) si disponibles
    if (typeof m === "object" && (m.params || m.vram_int8)) {
      const meta = document.createElement("span");
      meta.className = "opt-meta";
      const metaParts = [];
      if (m.params) metaParts.push(m.params);
      if (m.vram_int8) metaParts.push(m.vram_int8 + " VRAM");
      meta.textContent = metaParts.join(" · ");
      opt.appendChild(meta);
    }
    opt.addEventListener("mousedown", (ev) => {
      // mousedown (et non click) pour éviter que l'input ne perde le focus
      // avant que la valeur ne soit lue.
      ev.preventDefault();
      _selectRegistryModel(m);
    });
    dropdown.appendChild(opt);
  }
}

function _selectRegistryModel(m) {
  const input = $("registry-search");
  const dropdown = $("registry-dropdown");
  if (!input) return;
  const id = _registryModelId(m);
  // Affiche le nom lisible dans l'input, mais stocke l'id pour l'installation.
  const displayName = (typeof m === "object" && m.name) ? m.name : id;
  input.value = displayName;
  registrySelectedId = id;
  if (dropdown) dropdown.classList.add("hidden");
}

function _openRegistryDropdown() {
  const dropdown = $("registry-dropdown");
  if (!dropdown) return;
  const filtered = _filterRegistryModels($("registry-search").value);
  _renderRegistryDropdown(filtered);
  dropdown.classList.remove("hidden");
}

function _closeRegistryDropdown() {
  const dropdown = $("registry-dropdown");
  if (dropdown) dropdown.classList.add("hidden");
}

function _initComboboxEvents() {
  const input = $("registry-search");
  const dropdown = $("registry-dropdown");
  if (!input || !dropdown) return;

  // Saisie : filtrage en temps réel + ouverture du dropdown
  input.addEventListener("input", () => {
    registrySelectedId = "";   // l'utilisateur modifie : on invalide la sélection
    _openRegistryDropdown();
  });

  input.addEventListener("focus", () => {
    _openRegistryDropdown();
  });

  // Fermeture au clic extérieur
  document.addEventListener("click", (ev) => {
    const host = input.closest(".combobox-host");
    if (host && !host.contains(ev.target)) {
      _closeRegistryDropdown();
    }
  });

  // Échap ferme le dropdown
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      _closeRegistryDropdown();
    }
  });
}

async function loadModelRegistry() {
  const input = $("registry-search");
  const dropdown = $("registry-dropdown");
  const refresh = $("btn-registry-refresh");
  if (!input || !dropdown) return;
  if (refresh) refresh.disabled = true;
  input.placeholder = "Chargement…";
  input.value = "";
  registrySelectedId = "";
  registryModels = [];
  dropdown.classList.add("hidden");
  try {
    const res = await api("/api/server/registry");
    const models = Array.isArray(res && res.models) ? res.models : [];
    registryModels = models;
    if (!models.length) {
      input.placeholder = "Aucun modèle disponible (serveur injoignable ?)";
    } else {
      input.placeholder = "Rechercher un modèle…";
    }
  } catch (err) {
    input.placeholder = "Registry indisponible";
  } finally {
    if (refresh) refresh.disabled = false;
  }
}

async function installModel() {
  const input = $("registry-search");
  const feedback = $("install-feedback");
  const btn = $("btn-install-model");
  if (!input || !feedback || !btn || installInFlight) return;

  // Priorité 1 : un modèle a été sélectionné dans le dropdown (id connu).
  // Priorité 2 : le texte tapé (id alias ou repo HF complet, ex.
  // "Systran/faster-whisper-tiny").
  let model = (registrySelectedId || "").trim();
  if (!model) {
    const typed = input.value.trim();
    if (!typed) {
      feedback.className = "feedback";
      feedback.textContent = "Choisissez ou saisissez un modèle dans la liste.";
      return;
    }
    // Si le texte tapé correspond au nom d'un modèle connu, on utilise son id.
    const match = registryModels.find((m) => {
      const name = (typeof m === "object" && m.name) ? String(m.name) : "";
      return name && name.toLowerCase() === typed.toLowerCase();
    });
    model = match ? _registryModelId(match) : typed;
  }

  installInFlight = true;
  const old = getBtnLabel(btn);
  btn.disabled = true;
  setBtnLabel(btn, "Installation en cours…");
  feedback.className = "feedback";
  feedback.textContent = "Téléchargement du modèle… (cela peut prendre plusieurs minutes)";
  try {
    const res = await api("/api/server/models/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    if (res && res.ok) {
      feedback.className = "feedback ok";
      feedback.textContent = `Modèle « ${model} » installé avec succès.`;
      // Rafraîchit la liste des modèles installés (dropdown de transcription).
      loadServerStatus();
    } else {
      feedback.className = "feedback";
      feedback.textContent = (res && res.error) || "Échec de l'installation.";
    }
  } catch (err) {
    feedback.className = "feedback";
    feedback.textContent = "Erreur pendant l'installation : " + err;
  } finally {
    installInFlight = false;
    btn.disabled = false;
    setBtnLabel(btn, old);
  }
}

async function testServer() {
  const btn = $("btn-server-test");
  const result = $("server-test-result");
  btn.disabled = true;
  const old = getBtnLabel(btn);
  setBtnLabel(btn, "Test en cours…");
  result.className = "server-test-result";
  result.textContent = "Vérification de la connexion…";
  try {
    // On envoie les valeurs actuellement saisies dans le formulaire (avant
    // sauvegarde) afin que le test reflète ce que l'utilisateur voit à
    // l'écran, pas la config persistée sur disque.
    const res = await api("/api/server/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: $("cfg-server-url").value.trim(),
        server_api_key: $("cfg-server-api-key").value,
      }),
    });
    if (res.reachable) {
      result.className = "server-test-result ok";
      const latency = (res.latency_ms == null) ? "—" : `${res.latency_ms} ms`;
      const models = (res.models || []).map((m) => escapeHtml(String(m))).join(", ")
        || "aucun modèle listé";
      result.innerHTML =
        `<div class="test-title"><span class="dot mint"></span>Serveur joignable</div>
         <div class="test-line">Latence : <strong>${latency}</strong></div>
         <div class="test-line">Modèles : <span class="mono">${models}</span></div>`;
      showToast("Connexion au serveur réussie.");
    } else {
      result.className = "server-test-result err";
      result.innerHTML =
        `<div class="test-title"><span class="dot rose"></span>Serveur injoignable</div>
         <div class="test-line">${escapeHtml(res.error || "Vérifiez l'URL et la clé API.")}</div>`;
      showToast("Connexion au serveur impossible.");
    }
    loadServerStatus();          // rafraîchit badge + modèles
  } catch (err) {
    result.className = "server-test-result err";
    result.innerHTML =
      `<div class="test-title"><span class="dot rose"></span>Test impossible</div>
       <div class="test-line">${escapeHtml(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    setBtnLabel(btn, old);
  }
}

// --------------------------------------------------------------------------
// Historique des transcriptions
// --------------------------------------------------------------------------
function fmtTime(ts) {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleTimeString("fr-FR", {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch { return ""; }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function fmtDuration(d) {
  if (typeof d !== "number" || !isFinite(d) || d <= 0) return "—";
  return `${d.toFixed(2)} s`;
}

async function loadHistory(silent = false) {
  try {
    const data = await api("/api/history");
    const history = Array.isArray(data.history) ? data.history.slice().reverse() : [];
    const same = history.length === app.history.length &&
      history.every((e, i) => e.ts === (app.history[i] && app.history[i].ts));
    if (silent && same) return;
    app.history = history;
    renderHistory();
  } catch (err) {
    console.warn(`[talky] Historique indisponible : ${err.message}`);
    markHistoryFallback();
  }
}

// Si les squelettes de chargement sont encore affichés (échec du premier
// fetch), on bascule sur le message « vide » pour ne pas simuler un chargement
// sans fin.
function markHistoryFallback() {
  const list = $("history-list");
  if (list && list.querySelector(".history-skeleton")) renderHistory();
}

function renderHistory() {
  const list = $("history-list");
  list.innerHTML = "";

  // La carte héro reflète toujours la transcription la plus récente.
  updateHeroLast();

  if (!app.history.length) {
    const li = document.createElement("li");
    li.className = "history-empty";
    li.textContent = "Aucune transcription pour l'instant.";
    list.appendChild(li);
    return;
  }

  for (const item of app.history) {
    if (!item || typeof item !== "object" || !item.text) continue;
    const li = document.createElement("li");
    li.className = "history-item glass-soft";
    li.innerHTML =
      `<div class="meta">
         <span>${fmtTime(item.ts)} · ${fmtDuration(item.duration)} · ${escapeHtml(item.language || "?")}</span>
         <button type="button" class="copy-btn"
                 data-text="${encodeURIComponent(String(item.text))}"
                 title="Copier cette transcription" aria-label="Copier cette transcription">
           ${ICONS.copy}${ICONS.check}<span class="btn-label">Copier</span>
         </button>
       </div>
       <p>${escapeHtml(item.text)}</p>`;
    list.appendChild(li);
  }
}

// Copie au presse-papier avec repli exécCommand (contexte http non sécurisé).
// `btn` (optionnel) reçoit un feedback inline « Copié ✓ » éphémère en plus du
// toast global.
async function copyText(text, btn) {
  let ok = true;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    } catch {
      ok = false;
    }
  }
  showToast(ok ? "Texte copié dans le presse-papier." : "Copie impossible (presse-papier refusé).");
  if (btn && ok) {
    const label = btn.querySelector(".btn-label");
    btn.classList.add("copied");
    if (label) {
      if (!btn.dataset.label) btn.dataset.label = label.textContent;
      label.textContent = "Copié";
    }
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(() => {
      btn.classList.remove("copied");
      if (label && btn.dataset.label) label.textContent = btn.dataset.label;
    }, 1600);
    announce("Transcription copiée dans le presse-papier.");
  }
}

// --------------------------------------------------------------------------
// Annonces ARIA (région unique, à la place de aria-live sur la liste entière)
// --------------------------------------------------------------------------
let announceTimer = null;

// Ré-annonce fiable : on vide d'abord la région ( sinon un contenu identique
// ne serait pas lu de nouveau par les lecteurs d'écran), puis on la remplit.
function announce(message) {
  const el = $("live-region");
  if (!el || !message) return;
  el.textContent = "";
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => { el.textContent = message; }, 80);
}

// --------------------------------------------------------------------------
// Carte héro : dernière dictée + libellé du micro
// --------------------------------------------------------------------------
function updateHeroLast() {
  const txt = $("hero-last-text");
  const btn = $("hero-last-copy");
  const last = app.history[0];
  const text = last && last.text ? String(last.text) : "";
  if (txt) {
    txt.textContent = text || "Aucune transcription pour l'instant.";
    txt.classList.toggle("empty", !text);
  }
  if (btn) btn.disabled = !text;
}

function updateHeroMicLabel() {
  const el = $("hero-mic-name");
  if (!el) return;
  const sel = $("cfg-audio-device");
  const opt = sel && sel.selectedOptions && sel.selectedOptions[0];
  el.textContent = (!sel || !sel.value || !opt)
    ? "Micro par défaut"
    : opt.textContent;
}

// --------------------------------------------------------------------------
// Modale de confirmation « Vider l'historique » (remplace confirm() natif)
// --------------------------------------------------------------------------
let confirmReturnFocus = null;

function openClearConfirm() {
  const modal = $("confirm-modal");
  if (!modal) return;
  const n = app.history.length;
  $("confirm-count").textContent = n > 0
    ? `${n} transcription${n > 1 ? "s" : ""} seront définitivement supprimées.`
    : "L'historique est déjà vide.";
  confirmReturnFocus = document.activeElement;
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  // Focus sur « Annuler » : évite toute suppression par simple Entrée réflexe.
  $("confirm-cancel").focus();
}

function closeClearConfirm() {
  const modal = $("confirm-modal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (confirmReturnFocus && typeof confirmReturnFocus.focus === "function") {
    confirmReturnFocus.focus();
  }
  confirmReturnFocus = null;
}

async function doClearHistory() {
  try {
    await api("/api/history", { method: "DELETE" });
    app.history = [];
    renderHistory();
    showToast("Historique vidé.");
    announce("Historique des transcriptions vidé.");
  } catch (err) {
    showToast(`Erreur : ${err.message}`);
  } finally {
    closeClearConfirm();
  }
}

function initConfirmModal() {
  const modal = $("confirm-modal");
  if (!modal) return;
  $("confirm-ok").addEventListener("click", doClearHistory);
  $("confirm-cancel").addEventListener("click", closeClearConfirm);
  // Clic sur le fond (hors de la carte) = Annuler.
  modal.addEventListener("mousedown", (ev) => {
    if (ev.target === modal) closeClearConfirm();
  });
  // Échap ferme ; Tab reste piégé entre les deux boutons (focus trap minimal).
  modal.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      closeClearConfirm();
      return;
    }
    if (ev.key !== "Tab") return;
    const first = $("confirm-cancel");
    const last = $("confirm-ok");
    const active = document.activeElement;
    const inside = modal.contains(active);
    if (ev.shiftKey && (!inside || active === first)) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && (!inside || active === last)) {
      ev.preventDefault();
      first.focus();
    }
  });
}

// --------------------------------------------------------------------------
// Visualisation audio — waveform à barres miroir façon messagerie vocale
// --------------------------------------------------------------------------
// Chaque événement WS "audio" ({levels: [floats -1..1], recording}) arrive à
// ~20 fps, MÊME au repos (recording=false : micro ouvert, pas de dictée).
//
// Pipeline de rendu :
//   1. auto-gain : pic courant amorti (retombée ×0.96/événement), gain cible
//      0.9/pic plafonné à 20×, lissé — les niveaux bruts de parole (~0.05)
//      sont amplifiés pour occuper la hauteur des barres ;
//   2. envelope follower par échantillon : env = max(|v|·gain, env·0.85)
//      → attaque instantanée (max), relâchement lent (décroissance ×0.85) ;
//   3. accumulation en colonnes de 20 échantillons (~64 colonnes/s) : chaque
//      colonne garde le max local + une retombée douce entre colonnes ; la
//      trace la plus récente est ancrée à droite (défilement vers la gauche,
//      ~2.5 s de mémoire visible) ;
//   4. boucle requestAnimationFrame : hauteurs affichées interpolées vers les
//      colonnes cibles (lerp 0.35) → rendu 60 fps sans à-coups.
//   Chaque barre : 3 px arrondis, gap 1 px, symétrique autour de l'axe central,
//   dégradé vertical mint → sky (+ halo doux pendant l'enregistrement).
//
// Modes d'affichage :
//   - live       (recording=true)        : défilement temps réel + halo ;
//   - frozen     (status=transcribing)   : trace figée estompée ;
//   - monitoring (micro ouvert au repos) : barres quasi plates animées d'une
//     micro-respiration sinusoïdale + libellé « Micro actif · en attente » ;
//   - moteur arrêté : barres au minimum, sans animation.
// prefers-reduced-motion : ni interpolation ni respiration ; redessin
// uniquement à l'arrivée de nouvelles données (rendu allégé).
const VIZ_BAR_W = 3;            // largeur d'une barre (px CSS)
const VIZ_GAP = 1;              // espace entre barres (px CSS)
const VIZ_AMP = 0.42;           // amplitude max (fraction de la hauteur)
const VIZ_RELEASE = 0.85;       // release de l'envelope follower (par échantillon)
const VIZ_COL_RELEASE = 0.90;   // retombée douce entre colonnes
const VIZ_SAMPLES_PER_COL = 20; // échantillons entrants par colonne (~64 col/s)
const VIZ_LERP = 0.35;          // interpolation des hauteurs affichées
const VIZ_MAX_GAIN = 20;        // plafond d'auto-gain (bruit de fond)

const viz = {
  canvas: null,
  ctx: null,
  dpr: 1,
  cols: [],          // colonnes cibles 0..1 (ancien → récent)
  disp: [],          // hauteurs affichées (interpolées)
  env: 0,            // envelope follower courant
  colPeak: 0,        // max local en cours de colonne
  colCount: 0,       // échantillons accumulés dans la colonne en cours
  peak: 0.02,        // pic brut amorti (auto-gain)
  gain: 1,           // gain lissé
  recording: false,  // enregistrement en cours (dernier événement reçu)
  frozen: false,     // transcription serveur : trace figée estompée
  lastAudioAt: 0,    // horodatage du dernier événement audio (garde-fou flux mort)
  labelKey: "",      // dernier libellé affiché (évite les écritures DOM à 20 fps)
  grad: null,        // dégradé vertical mint → sky (recréé si taille change)
  gradW: 0, gradH: 0,
  raf: 0,
  dirty: true,       // nouvelles données à dessiner (mode reduced-motion)
  reduced: (typeof matchMedia === "function")
    ? matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false, addEventListener() {} },
};

function initAudioCanvas() {
  if (viz.canvas) return;
  viz.canvas = $("audio-canvas");
  if (!viz.canvas) return;
  viz.ctx = viz.canvas.getContext("2d");
  resizeAudioCanvas();
  // Suit le layout fluide (largeur du canvas variable) et force un redraw.
  window.addEventListener("resize", () => {
    resizeAudioCanvas();
    viz.dirty = true;
  });
  // Les changements de préférence sont rares, mais on redessine proprement.
  if (viz.reduced.addEventListener) {
    viz.reduced.addEventListener("change", () => { viz.dirty = true; });
  }
  if (!viz.raf) viz.raf = requestAnimationFrame(vizFrame);
  syncVizLabel();
}

function resizeAudioCanvas() {
  if (!viz.canvas) return;
  viz.dpr = window.devicePixelRatio || 1;
  const rect = viz.canvas.getBoundingClientRect();
  // Fallback : si le layout n'est pas encore calculé (rect width = 0), on
  // garde les attributs HTML du canvas comme valeurs par défaut.
  const cssW = rect.width || (viz.canvas.width / viz.dpr) || 600;
  const cssH = rect.height || (viz.canvas.height / viz.dpr) || 76;
  const w = Math.max(1, Math.floor(cssW * viz.dpr));
  const h = Math.max(1, Math.floor(cssH * viz.dpr));
  if (viz.canvas.width !== w || viz.canvas.height !== h) {
    viz.canvas.width = w;
    viz.canvas.height = h;
    viz.grad = null;          // dégradé à recréer à la nouvelle taille
  }
}

// Nombre de colonnes visibles (une barre par colonne, ancrées à droite).
function vizCapacity() {
  if (!viz.canvas) return 16;
  const pitch = VIZ_BAR_W + VIZ_GAP;
  const cssW = viz.canvas.width / (viz.dpr || 1);
  return Math.max(16, Math.floor(cssW / pitch));
}

function handleAudio(data) {
  if (!viz.canvas) initAudioCanvas();
  if (!viz.canvas) return;

  const levels = data && Array.isArray(data.levels) ? data.levels : null;
  if (!levels) return;

  const recording = !!(data && data.recording);

  // (Re)prise d'enregistrement : on repart d'une trace vierge.
  if (recording && !viz.recording) {
    viz.cols.length = 0;
    viz.disp.length = 0;
    viz.env = 0;
    viz.colPeak = 0;
    viz.colCount = 0;
    viz.peak = 0.02;
  }
  viz.recording = recording;
  viz.lastAudioAt = performance.now();
  syncVizMode();
  syncVizLabel();

  // Contexte 2D indisponible (canvas non rendu, env. de test) : on met
  // quand même à jour les libellés, seul le pipeline de dessin est ignoré.
  if (!viz.ctx) return;

  // Transcription serveur en cours : la trace est figée (plus rien n'est
  // poussé, le rendu reste affiché estompé jusqu'au retour au repos).
  if (viz.frozen) return;

  // --- Auto-gain -----------------------------------------------------------
  // Le pic brut amorti (retombée ×0.96 par événement ≈ demi-vie ~0.4 s)
  // donne un gain stable, sans pompage visible pendant le défilement.
  let blockPeak = 0;
  for (let i = 0; i < levels.length; i++) {
    const v = Number(levels[i]);
    if (Number.isFinite(v) && Math.abs(v) > blockPeak) blockPeak = Math.abs(v);
  }
  viz.peak = Math.max(blockPeak, viz.peak * 0.96);
  const targetGain = Math.min(VIZ_MAX_GAIN, 0.9 / Math.max(viz.peak, 0.004));
  viz.gain += (targetGain - viz.gain) * 0.15;

  // --- Envelope follower + accumulation en colonnes -------------------------
  for (let i = 0; i < levels.length; i++) {
    let v = Number(levels[i]);
    if (!Number.isFinite(v)) continue;
    v = Math.max(-1, Math.min(1, v));
    // Attaque rapide (max), relâchement lent (décroissance ×0.85).
    viz.env = Math.max(Math.abs(v) * viz.gain, viz.env * VIZ_RELEASE);
    viz.colPeak = Math.max(viz.colPeak, viz.env);
    if (++viz.colCount >= VIZ_SAMPLES_PER_COL) {
      pushVizColumn(Math.min(1, viz.colPeak));
      viz.colPeak = 0;
      viz.colCount = 0;
    }
  }
  viz.dirty = true;
}

// Empile une colonne (max local de l'enveloppe) avec retombée douce, la plus
// récente à droite ; la plus ancienne sort à gauche une fois la fenêtre pleine.
function pushVizColumn(v) {
  const prev = viz.cols.length ? viz.cols[viz.cols.length - 1] : 0;
  viz.cols.push(Math.max(v, prev * VIZ_COL_RELEASE));
  const cap = vizCapacity();
  while (viz.cols.length > cap) viz.cols.shift();
}

// État figé = transcription serveur en cours (hors enregistrement).
// Garde-fou « flux audio mort » : les événements audio (20 fps) sont la
// vérité sur le micro ; mais si plus aucun n'arrive (>1 s) alors que le
// moteur n'est plus en enregistrement, on se resynchronise sur l'état
// moteur (sinon le libellé de monitoring resterait masqué pour toujours).
function syncVizMode() {
  const st = app.state && app.state.status;
  const audioFresh = viz.lastAudioAt > 0
    && (performance.now() - viz.lastAudioAt) < 1000;
  if (!audioFresh) {
    if (viz.recording && st !== "recording") viz.recording = false;
    if (!viz.recording) viz.frozen = st === "transcribing";
  } else if (!viz.recording) {
    viz.frozen = false;   // le micro reprend : la trace repart
  }
}

// Libellé superposé au canvas selon le mode :
// monitoring au repos, transcription en cours, moteur arrêté…
function syncVizLabel() {
  const el = $("audio-placeholder");
  if (!el) return;
  const st = app.state && app.state.status;
  // Clé de state pour n'écrire le DOM que sur changement réel (20 fps).
  const key = `${viz.recording ? "R" : viz.frozen ? "F" : "-"}|${st || ""}`;
  if (key === viz.labelKey) return;
  viz.labelKey = key;

  if (viz.recording) {
    el.classList.add("hidden");       // place aux barres temps réel
    return;
  }
  el.classList.remove("hidden");

  let text, dim = false;
  if (st == null) {
    text = "Initialisation…";
  } else if (viz.frozen || st === "transcribing") {
    text = "Transcription serveur en cours…";
    dim = true;
  } else if (st === "booting") {
    text = "Démarrage du moteur…";
  } else if (st === "stopping") {
    text = "Arrêt du moteur…";
  } else if (st === "error") {
    text = "Erreur moteur — voir le détail sous l'historique";
    dim = true;
  } else if (st === "idle") {
    text = "Moteur arrêté — démarrez le moteur pour dicter";
    dim = true;
  } else {
    // ready / success : micro ouvert, en attente d'une dictée
    text = "Micro actif · en attente de dictée";
  }
  el.textContent = text;
  el.classList.toggle("dim", dim);
}

// Boucle rAF : interpolation des hauteurs affichées puis dessin.
// En reduced-motion : aucun lissage ni animation, redessin sur données neuves.
function vizFrame(t) {
  viz.raf = requestAnimationFrame(vizFrame);
  if (!viz.ctx || document.hidden) return;

  const reduced = viz.reduced.matches;
  if (reduced) {
    if (!viz.dirty) return;
    viz.dirty = false;
    drawViz(t, true);
    return;
  }

  // Interpolation des hauteurs affichées vers les colonnes cibles (60 fps,
  // lerp 0.35) : lisse le pas de 20 fps des événements WS sans effacer
  // complètement les transitoires.
  const n = vizCapacity();
  if (viz.disp.length !== n) viz.disp.length = n;
  const cols = viz.cols;
  const base = cols.length - n;   // < 0 tant que la trace ne remplit pas l'écran
  const k = VIZ_LERP;
  for (let i = 0; i < n; i++) {
    const target = (base + i >= 0) ? (cols[base + i] || 0) : 0;
    const cur = viz.disp[i] || 0;
    viz.disp[i] = cur + (target - cur) * k;
  }
  drawViz(t, false);
}

// Dessin : barres verticales arrondies miroir (3 px / gap 1 px), dégradé
// vertical mint → sky, halo doux en enregistrement, respiration au repos.
function drawViz(t, reduced) {
  resizeAudioCanvas();
  if (!viz.ctx) return;
  const ctx = viz.ctx;
  const W = viz.canvas.width;
  const H = viz.canvas.height;
  const dpr = viz.dpr || 1;
  ctx.clearRect(0, 0, W, H);

  const pitch = (VIZ_BAR_W + VIZ_GAP) * dpr;
  const barW = VIZ_BAR_W * dpr;
  const n = Math.max(8, Math.floor(W / pitch));
  const mid = H / 2;
  const maxHalf = H * VIZ_AMP;   // demi-hauteur max (marge haut/bas)
  const minHalf = 1.5 * dpr;     // barre minimale : trait continu discret

  // Mode courant : live / frozen (transcription) / monitoring (repos).
  const mode = viz.recording ? "live" : (viz.frozen ? "frozen" : "monitor");
  const running = !!(app.state && app.state.running);

  // Dégradé vertical mint → sky, centré sur l'axe (miroir cohérent) —
  // recréé uniquement quand la taille du canvas change.
  if (!viz.grad || viz.gradW !== W || viz.gradH !== H) {
    const g = ctx.createLinearGradient(0, mid - maxHalf, 0, mid + maxHalf);
    g.addColorStop(0, "#8ff0c4");    // mint
    g.addColorStop(0.5, "#96e3e1"); // mélange (centre)
    g.addColorStop(1, "#9cd6ff");    // sky
    viz.grad = g;
    viz.gradW = W;
    viz.gradH = H;
  }
  ctx.fillStyle = viz.grad;

  // Halo doux uniquement pendant l'enregistrement (jamais en reduced-motion).
  if (mode === "live" && !reduced) {
    ctx.shadowColor = "rgba(255, 168, 184, 0.5)";
    ctx.shadowBlur = 10 * dpr;
  }
  if (mode === "frozen") ctx.globalAlpha = 0.4;       // barres estompées
  else if (mode === "monitor") ctx.globalAlpha = 0.7; // barres quasi plates

  const cols = viz.cols;
  const base = cols.length - n;
  const disp = viz.disp;
  // Micro-respiration : vague sinusoïdale discrète parcourant les barres
  // (amplitude totale ~2–4 px) — signale que le micro est sous tension.
  const breathe = mode === "monitor" && running && !reduced;
  const tsec = t / 1000;

  for (let i = 0; i < n; i++) {
    let v;
    if (mode === "monitor") {
      v = (base + i >= 0) ? (cols[base + i] || 0) : 0;
      if (breathe) v += 0.045 + 0.035 * Math.sin(tsec * 2.0 - i * 0.3);
    } else {
      v = reduced ? ((base + i >= 0) ? (cols[base + i] || 0) : 0)
                  : (disp[i] || 0);
    }
    const half = Math.max(minHalf, Math.min(maxHalf, v * maxHalf));
    const x = W - (n - i) * pitch + (pitch - barW) / 2;
    barPath(ctx, x, mid - half, barW, half * 2, barW / 2);
    ctx.fill();
  }

  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
}

// Chemin d'une barre à extrémités arrondies (équivalent roundRect, qui n'est
// pas disponible dans tous les moteurs — fallback manuel volontaire).
function barPath(ctx, x, y, w, h, r) {
  if (r > w / 2) r = w / 2;
  if (r > h / 2) r = h / 2;
  const x2 = x + w;
  const y2 = y + h;
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x2 - r, y);
  ctx.arc(x2 - r, y + r, r, -Math.PI / 2, 0);
  ctx.lineTo(x2, y2 - r);
  ctx.arc(x2 - r, y2 - r, r, 0, Math.PI / 2);
  ctx.lineTo(x + r, y2);
  ctx.arc(x + r, y2 - r, r, Math.PI / 2, Math.PI);
  ctx.lineTo(x, y + r);
  ctx.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
  ctx.closePath();
}

// --------------------------------------------------------------------------
// Transcription en direct (mode continu, événements WS "partial_transcript")
// --------------------------------------------------------------------------
// Affiche le texte au fur et à mesure pendant l'enregistrement. En mode
// talky, le VAD serveur découpe les phrases : chaque segment reçu est
// déjà final ({is_final: true, recording: true}) et s'accumule dans
// finalText. Le chemin « partiel » (is_final=false → partialText) reste géré
// pour compatibilité. À la fin de l'enregistrement (state → success/ready) la
// zone se vide et le texte final apparaît dans l'historique (événement
// "transcript").
const live = {
  active: false,          // true pendant l'enregistrement en mode continu
  finalText: "",           // segments déjà verrouillés (concaténés)
  partialText: "",         // segment partiel en cours
};

function liveZoneEl() { return $("live-transcript"); }
function liveFinalEl() { return $("live-transcript-final"); }
function livePartialEl() { return $("live-transcript-partial"); }
function liveIdleEl() { return $("live-transcript-idle"); }

function continuousModeEnabled() {
  // Le mode continu est actif si la config l'indique (défaut: true).
  return !!(app.config && app.config.continuous_mode != null
    ? app.config.continuous_mode : true);
}

// Affiche/masque la zone selon l'état du moteur. Appelé depuis updateState().
function syncLiveTranscript(status) {
  const zone = liveZoneEl();
  if (!zone) return;
  const recording = status === "recording";
  const continuous = continuousModeEnabled();

  if (recording && continuous) {
    live.active = true;
    zone.classList.remove("hidden");
    zone.classList.remove("idle");
    zone.classList.remove("final");
    if (!live.finalText && !live.partialText) {
      // Début d'enregistrement, pas encore de texte : message d'attente discret.
      zone.classList.add("idle");
    }
    renderLiveTranscript();
  } else {
    // L'enregistrement s'est arrêté (success/ready/error/idle/transcribing) :
    // on vide la zone et on la masque. Le texte final arrive dans l'historique.
    if (live.active) clearLiveTranscript();
    zone.classList.add("hidden");
  }
}

// Gère un événement partial_transcript : { text, is_final }.
function handlePartialTranscript(data) {
  if (!data || typeof data.text !== "string") return;
  if (!live.active) {
    // Repli : si on reçoit des partials alors qu'on n'était pas marqué actif
    // (ex. état WS pas encore reçu), on active la zone si le mode continu l'autorise.
    if (!continuousModeEnabled()) return;
    live.active = true;
    const zone = liveZoneEl();
    if (zone) zone.classList.remove("hidden");
  }

  const zone = liveZoneEl();
  if (zone) { zone.classList.remove("idle"); }

  if (data.is_final) {
    // Segment verrouillé : on l'accumule au texte final.
    const seg = data.text.trim();
    if (seg) {
      live.finalText = (live.finalText ? live.finalText + " " : "") + seg;
    }
    live.partialText = "";
    if (zone) zone.classList.add("final");
  } else {
    live.partialText = data.text;
    if (zone) zone.classList.remove("final");
  }
  renderLiveTranscript();
}

// Met à jour le DOM de la zone live.
function renderLiveTranscript() {
  const finalEl = liveFinalEl();
  const partialEl = livePartialEl();
  const idleEl = liveIdleEl();
  if (!finalEl || !partialEl) return;

  finalEl.textContent = live.finalText ? live.finalText + " " : "";
  partialEl.textContent = live.partialText;

  const hasContent = !!(live.finalText || live.partialText);
  if (idleEl) idleEl.classList.toggle("hidden", live.active ? hasContent : false);
}

// Remet à zéro la zone live (après ajout à l'historique ou fin d'enregistrement).
function clearLiveTranscript() {
  live.active = false;
  live.finalText = "";
  live.partialText = "";
  renderLiveTranscript();
  const zone = liveZoneEl();
  if (zone) {
    zone.classList.remove("final");
    zone.classList.add("idle");
    const idleEl = liveIdleEl();
    if (idleEl) idleEl.classList.remove("hidden");
  }
}

// --------------------------------------------------------------------------
// Journal (mini-console + toast sur erreur)
// --------------------------------------------------------------------------
function appendLog(entry) {
  if (!entry) return;
  if (entry.level === "ERROR") {
    showToast(`[${entry.level}] ${entry.message}`);
  } else if (entry.level === "WARN") {
    console.warn(`[talky] ${entry.message}`);
  } else {
    console.info(`[talky] ${entry.message}`);
  }
}

// --------------------------------------------------------------------------
// Notifications toast éphémère
// --------------------------------------------------------------------------
let toastTimer = null;
function showToast(message) {
  let toast = document.getElementById("toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.className = "glass";
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("show");
  }, 2600);
}

// --------------------------------------------------------------------------
// Capture du raccourci (ev.code avec repli ev.key)
// --------------------------------------------------------------------------
let capturingHotkey = false;

// Noms canoniques compatibles avec parse_hotkey() (client/app/engine/hotkeys.py) :
// lettres minuscules, chiffres, f1-f12, navigation/édition, ponctuation.
const HOTKEY_CODES = {
  "Space": "space", "Enter": "enter", "Tab": "tab", "Escape": "esc",
  "Backspace": "backspace", "Delete": "delete", "Insert": "insert",
  "Home": "home", "End": "end", "PageUp": "pageup", "PageDown": "pagedown",
  "CapsLock": "caps_lock",
  "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
  "Minus": "minus", "Equal": "equal",
  "BracketLeft": "bracketleft", "BracketRight": "bracketright",
  "Backslash": "backslash", "Semicolon": "semicolon", "Apostrophe": "apostrophe",
  "Grave": "grave", "Comma": "comma", "Period": "dot", "Slash": "slash",
};

function hotkeyName(ev) {
  const code = ev.code || "";
  let main = "";
  if (/^Key[A-Z]$/.test(code)) main = code.slice(3).toLowerCase();        // KeyA -> a
  else if (/^Digit[0-9]$/.test(code)) main = code.slice(5);               // Digit1 -> 1
  else if (/^F([1-9]|1[0-2])$/.test(code)) main = code.toLowerCase();    // F8 -> f8
  else if (HOTKEY_CODES[code]) main = HOTKEY_CODES[code];
  // Repli sur ev.key : caractère simple produit (layout variés, dead keys).
  if (!main && ev.key && /^[a-zA-Z0-9]$/.test(ev.key)) main = ev.key.toLowerCase();
  if (!main) return "";   // modificateur seul ou touche non supportée

  const mods = [];
  if (ev.ctrlKey) mods.push("ctrl");
  if (ev.altKey) mods.push("alt");
  if (ev.metaKey) mods.push("super");          // méta Linux = super (parse_hotkey)
  // Le décalage maj est demandé explicitement pour lettre/chiffre/espace.
  if (ev.shiftKey && /^[a-z0-9 ]$/.test(main)) mods.push("shift");
  return mods.concat(main).join("+");
}

function initHotkeyCapture() {
  const btn = $("hotkey-pick");
  btn.addEventListener("click", () => {
    capturingHotkey = true;
    btn.textContent = "…";
    btn.classList.add("capturing");
  });
  document.addEventListener("keydown", (ev) => {
    if (!capturingHotkey) return;
    ev.preventDefault();
    if (ev.key === "Escape") {
      capturingHotkey = false;
      btn.classList.remove("capturing");
      btn.textContent = (app.config && app.config.hotkey) || "—";
      return;
    }
    const name = hotkeyName(ev);
    if (!name) return;   // modificateur seul : on attend la touche principale
    capturingHotkey = false;
    btn.classList.remove("capturing");
    btn.textContent = name;
    (async () => {
      try {
        const res = await postConfig({ hotkey: name });
        if (res.saved) {
          app.config = res.config;
          btn.textContent = name;
          showToast(`Raccourci « ${name} » enregistré (appliqué à chaud).`);
        } else {
          throw new Error("Sauvegarde refusée par le serveur.");
        }
      } catch (err) {
        showToast(`Erreur : ${err.message}`);
        btn.textContent = (app.config && app.config.hotkey) || "—";
      }
    })();
  });
}

// --------------------------------------------------------------------------
// Initialisation
// --------------------------------------------------------------------------
function bindEvents() {
  // Moteur
  $("power-toggle").addEventListener("click", togglePower);
  $("btn-restart").addEventListener("click", restartEngine);

  // Configuration complète
  $("btn-save").addEventListener("click", saveConfig);

  // Réglages appliqués à chaud (HOT_FIELDS)
  $("cfg-mode").addEventListener("change", quickApply);
  $("cfg-continuous").addEventListener("change", quickApply);
  $("cfg-inject").addEventListener("change", quickApply);
  $("cfg-add-space").addEventListener("change", quickApply);
  $("cfg-keep-clipboard").addEventListener("change", quickApply);
  $("cfg-compute").addEventListener("change", quickApply);

  // Raccourci à la volée
  initHotkeyCapture();

  // Périphériques audio
  $("btn-devices-refresh").addEventListener("click", loadDevices);

  // Historique : copie par délégation ; « Vider » passe par la modale de
  // confirmation custom (plus de suppression immédiate ni de confirm() natif).
  $("history-list").addEventListener("click", (evt) => {
    const btn = evt.target.closest(".copy-btn");
    if (btn) copyText(decodeURIComponent(btn.dataset.text), btn);
  });
  $("clear-history").addEventListener("click", openClearConfirm);

  // Carte héro : copie rapide de la dernière transcription
  $("hero-last-copy").addEventListener("click", () => {
    const last = app.history[0];
    if (last && last.text) copyText(String(last.text), $("hero-last-copy"));
  });

  // Libellé du micro affiché dans la carte héro (affichage seul, pas d'
  // application à chaud — le changement de micro redémarre le moteur côté
  // serveur via « Enregistrer »).
  $("cfg-audio-device").addEventListener("change", updateHeroMicLabel);

  // Modale de confirmation « Vider l'historique »
  initConfirmModal();

  // Section « Serveur »
  $("btn-server-test").addEventListener("click", testServer);
  // Section « Modèles (installation) »
  $("btn-registry-refresh").addEventListener("click", loadModelRegistry);
  $("btn-install-model").addEventListener("click", installModel);
}

async function initLoad() {
  // Chargement initial indépendant : un échec sur une route ne bloque pas
  // les autres (ex. /api/server/status en erreur pendant que config OK).
  const [config, history, status] = await Promise.allSettled([
    api("/api/config"),
    api("/api/history"),
    api("/api/server/status"),
  ]);

  if (config.status === "fulfilled") {
    app.config = config.value;
    renderConfig();
  } else {
    console.warn(`[talky] Config indisponible au chargement : ${config.reason && config.reason.message}`);
  }

  if (history.status === "fulfilled") {
    app.history = Array.isArray(history.value.history)
      ? history.value.history.slice().reverse() : [];
    renderHistory();
  } else {
    console.warn(`[talky] Historique indisponible au chargement : ${history.reason && history.reason.message}`);
    markHistoryFallback();   // remplace les squelettes shimmer par l'état vide
  }

  if (status.status === "fulfilled") {
    updateServerBadge(status.value);
  } else {
    updateServerBadge(null);
  }

  loadDevices();
  loadModelRegistry();      // carte « Modèles (installation) »

  try {
    app.state = await api("/api/engine");
    updateState();
    updateMotorInfo();
  } catch (err) {
    console.warn(`[talky] État moteur indisponible au chargement : ${err.message}`);
  }
}

function init() {
  bindEvents();
  initAudioCanvas();
  _initComboboxEvents();
  initLoad();
  connectWS();

  // Polling du badge serveur toutes les 5 s (pauses si l'onglet est caché)
  loadServerStatus();
  app.serverPollTimer = setInterval(() => {
    if (document.visibilityState === "hidden") return;
    loadServerStatus();
  }, 5000);

  // Garde-fou temps réel : resynchronisation silencieuse de l'historique
  // (rattrape tout événement perdu, même si la WS a été coupée brièvement).
  setInterval(() => {
    if (document.visibilityState === "hidden") return;
    loadHistory(true);
  }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
