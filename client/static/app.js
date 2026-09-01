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
 * Visualisation audio : anneau radial « V1 Hub » (36 rayons autour d'un cercle
 * central, couleurs --accent/--accent-glow lues à chaque frame, halo en
 * enregistrement).
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
  // Timeout 15 s sur tous les appels (AbortSignal.timeout) : une route qui
  // mouline (téléchargement de modèle, serveur injoignable…) ne doit jamais
  // laisser une requête pendre indéfiniment. Repli sans signal si l'API
  // n'existe pas (anciens navigateurs) — alors pas de timeout, comme avant.
  let opts = options || {};
  if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
    opts = { ...opts, signal: AbortSignal.timeout(15000) };
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (err) {
    // AbortError (timeout) : message générique, jamais de crash — remonté
    // comme une erreur normale pour que les catch existants l'affichent.
    if (err && err.name === "AbortError") {
      throw new Error(`Le serveur n'a pas répondu en 15 s (${url})`);
    }
    throw err;
  }
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

// Chrono d'enregistrement : affiche « Enregistrement · MM:SS » dans le badge
// de statut pendant la dictée (mis à jour toutes les 500 ms, nettoyé sinon).
let chronoTimer = null;
let chronoStart = 0;
// Dernier état « running » vu par updateState — pour repérer la transition
// non-running → running (reset du pic de gain AGC de l'anneau).
let lastRunning = false;

function startChrono() {
  if (chronoTimer) return;
  chronoStart = Date.now();
  updateChrono();
  chronoTimer = setInterval(updateChrono, 500);
}
function stopChrono() {
  if (chronoTimer) { clearInterval(chronoTimer); chronoTimer = null; }
}
function updateChrono() {
  const el = $("status-text");
  if (!el) return;
  const sec = Math.floor((Date.now() - chronoStart) / 1000);
  const mm = String(Math.floor(sec / 60)).padStart(2, "0");
  const ss = String(sec % 60).padStart(2, "0");
  el.textContent = `Enregistrement · ${mm}:${ss}`;
}

function updateState() {
  const status = app.state && app.state.status;
  const meta = STATUS_META[status] || STATUS_META.error;

  // Theming par état : variable CSS --accent consommée par badge/bordures/halo.
  document.body.dataset.status = meta.group;

  const dot = $("status-dot");
  dot.classList.toggle("pulse", !!meta.pulse);
  dot.classList.toggle("live", !!meta.live);
  $("status-detail").textContent = (app.state && app.state.status_msg) || "";

  // Chrono d'enregistrement : « Enregistrement · MM:SS » pendant la dictée,
  // libellé statique sinon (les autres états gardent leur libellé existant).
  if (status === "recording") {
    startChrono();
  } else {
    stopChrono();
    $("status-text").textContent = meta.label;
  }

  // Bouton power dédié (id=power-toggle) : états visuels ON/OFF synchronisés
  // avec l'état réel du moteur (halo + glyphe coloré quand ON).
  // ARIA : role=button ne supporte que aria-pressed (bouton bascule) — pas
  // aria-checked (réservé aux rôles checkbox/radio/switch).
  // L'anneau radial, lui, est un pur indicateur (plus de rôle de bouton).
  const running = !!(app.state && app.state.running);
  const toggle = $("power-toggle");
  toggle.classList.toggle("on", running);
  toggle.classList.toggle("off", !running);
  toggle.setAttribute("aria-pressed", String(running));
  // Anneau radial : synchronise l'état d'enregistrement et, moteur arrêté,
  // ramène les niveaux cibles à zéro pour que les rayons redescendent au
  // minimum via le lerp existant (au lieu de rester figés sur l'anneau éteint).
  RING.recording = (status === "recording");
  // Transition non-running → running : le pic de gain conservé pendant l'arrêt
  // (peut être bruité/élevé du silence) repartirait trop haut et gonflerait
  // l'anneau au démarrage. On le ramène au plancher pour une montée nette.
  if (running && !lastRunning) {
    RING.peak = RING.floor;
  }
  lastRunning = running;
  if (!running) {
    RING.levels = RING.levels.map(() => 0);
  }

  // NB : plus de curseur « progress » global pendant la transcription —
  // le theming data-status (halo + badge) signale déjà l'état occupé.

  // Zone « Transcription en direct » : visible uniquement en mode continu
  // pendant l'enregistrement (et masquée sinon). Quand l'enregistrement
  // s'arrête (success/ready/error/idle), on vide la zone.
  syncLiveTranscript(status);

  // NB : plus de scheduleFitZoom() ici — un changement d'état (badge,
  // live-transcript) ne doit PAS re-baser le zoom. Le scale reste stable
  // tant qu'il n'y a pas de resize.
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
  // NB : plus de scheduleFitZoom() ici pour un simple remplissage — le scale
  // ne se recalcule QUE sur resize / first-run. La croissance de contenu de
  // .col-side (config rendue, feedbacks) est couverte par le MutationObserver
  // debouncé installé dans init() — pas besoin de one-shot.
  // Premier renderConfig : débloque le premier fitZoom (qui attendait pour
  // mesurer la base avec le VRAI contenu, pas la colonne encore vide).
  if (fitFirstPending) {
    fitFirstPending = false;
    scheduleFitZoom(true);
  }
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
  const toggle = $("power-toggle");
  // Garde anti-double-clic : on désactive le bouton pendant l'await du fetch
  // (réactivé en finally, que l'appel réussisse ou échoue).
  toggle.disabled = true;
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
  } finally {
    toggle.disabled = false;
  }
}

async function restartEngine() {
  const btn = $("btn-restart");
  // Garde anti-double-clic : désactive pendant l'await du fetch, réactivé en
  // finally (identique à togglePower — un restart prend du temps côté serveur).
  if (btn) btn.disabled = true;
  try {
    await api("/api/engine/restart", { method: "POST" });
    showToast("Redémarrage du moteur en cours…");
  } catch (err) {
    showToast(`Erreur : ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
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

  // NB : plus de scheduleFitZoom() ici — la croissance de l'historique ne doit
  // pas dézoomer. La hauteur de référence est figée ; le scroll interne de la
  // liste absorbe l'excédent.

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
// Visualisation audio — anneau radial « V1 Hub »
// --------------------------------------------------------------------------
// Chaque événement WS "audio" ({levels: [64 floats -1..1], recording}) arrive
// à ~20 fps, MÊME au repos (recording=false : micro ouvert, pas de dictée).
//
// Rendu : 36 rayons autour d'un cercle central (canvas 150×150, DPR géré).
//   - les 64 niveaux sont regroupés en 36 rayons (RMS par groupe, valeurs
//     absolues) ;
//   - auto-gain glissant (peak-hold ~4 s) : la voix normale (RMS 0.05–0.15)
//     est normalisée par le pic récent → les rayons oscillent nettement entre
//     ~30 et ~90 % de leur longueur ; attaque rapide / relâchement lent pour
//     éviter le pompage ;
//   - courbe gamma v^0.55 pour rendre les petites amplitudes visibles ;
//   - longueur d'un rayon = base 5 px + valeur × ~34 px (amplitude max plus
//     grande), depuis un rayon interne ~40 px ;
//   - lissage lerp 0.35 en rAF (60 fps) ;
//   - couleurs --accent / --accent-glow lues à chaque frame via
//     getComputedStyle(document.body) → thématisation automatique par état
//     (body[data-status]) ;
//   - halo (glow) uniquement pendant l'enregistrement ;
//   - moteur arrêté (idle/off) : rayons au minimum, opacité réduite.
// prefers-reduced-motion : ni interpolation ni halo ; redessin uniquement à
// l'arrivée de nouvelles données (rendu allégé).
const RING = {
  canvas: null,
  ctx: null,
  dpr: 1,
  size: 150,          // taille CSS du canvas (px)
  rays: 36,           // nombre de rayons
  base: 5,            // longueur de base d'un rayon (px CSS)
  amp: 34,            // valeur × amp = longueur additionnelle (px CSS)
  inner: 40,          // rayon interne (px CSS)
  lerp: 0.35,         // interpolation des niveaux affichés
  // Auto-gain glissant : pic RMS récent normalisant les niveaux.
  peak: 0,            // pic RMS courant (value, 0..1) — peak-hold
  floor: 0.015,       // plancher (bruit/silence) soustrait avant normalisation
  release: 0.035,     // relâchement / frame (~3-4 s : 0.15 → plancher)
  gamma: 0.55,        // courbe de réponse v^gamma (petites amplitudes visibles)
  levels: [],         // niveaux cibles 0..1 (36) — cible courante (dernière frame jouée)
  disp: [],           // niveaux affichés (interpolés)
  recording: false,   // enregistrement en cours (frame consommée)
  // File de relecture des événements audio WS : le serveur broadcast par
  // paquets (~0.2 s) une LISTE d'événements {levels, recording} (≈20 frames de
  // 50 ms). handleAudio pousse chaque frame ici ; la boucle rAF les consomme
  // via un JITTER BUFFER « temps réel décalé » (voir consumeAudioFrames) : on
  // attend que la file soit garnie (≥3 frames ≈ 150 ms) puis on lit à un taux
  // adaptatif pour garder une profondeur stable → mouvement CONTINU de l'anneau
  // (une cible toutes les ~50-90 ms) quel que soit le jitter réseau/broadcast.
  audioQueue: [],     // file bornée {levels:[..], recording:bool}
  frameMs: 50,        // cadence de production d'une frame (50 ms)
  lastFrameTime: 0,   // horloge (performance.now) de la dernière consommation
  raf: 0,
  dirty: true,        // nouvelles données à dessiner (mode reduced-motion)
  reduced: (typeof matchMedia === "function")
    ? matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false, addEventListener() {} },
};

function initRing() {
  if (RING.canvas) return;
  RING.canvas = $("audio-canvas");
  if (!RING.canvas) return;
  RING.ctx = RING.canvas.getContext("2d");
  resizeRing();
  // Le canvas est carré et fixe (150×150) : on ne suit que le DPR.
  window.addEventListener("resize", () => { resizeRing(); RING.dirty = true; });
  if (RING.reduced.addEventListener) {
    RING.reduced.addEventListener("change", () => { RING.dirty = true; });
  }
  if (!RING.raf) RING.raf = requestAnimationFrame(ringFrame);
}

function resizeRing() {
  if (!RING.canvas) return;
  // Netteté à zoom CSS >1 : on compose le DPR avec le zoom courant du
  // conteneur (.layout) pour re-rasteriser le canvas à la bonne résolution.
  // getComputedStyle().zoom retourne "" en repli transform scale → parseFloat
  // = NaN → on retombe sur 1 (comportement d'avant).
  const layout = document.querySelector(".layout");
  let zoom = 1;
  if (layout) {
    const z = parseFloat(getComputedStyle(layout).zoom);
    if (Number.isFinite(z) && z > 0) zoom = z;
  }
  RING.dpr = (window.devicePixelRatio || 1) * zoom;
  const w = Math.max(1, Math.floor(RING.size * RING.dpr));
  if (RING.canvas.width !== w || RING.canvas.height !== w) {
    RING.canvas.width = w;
    RING.canvas.height = w;
  }
}

// Regroupe les 64 niveaux bruts en 36 rayons : RMS par groupe (valeurs
// absolues). Retourne un tableau de 36 valeurs 0..1.
function groupRingLevels(levels) {
  const n = RING.rays;
  const out = new Array(n).fill(0);
  const total = levels.length;
  for (let i = 0; i < n; i++) {
    const start = Math.floor(i * total / n);
    const end = Math.floor((i + 1) * total / n);
    let sum = 0, count = 0;
    for (let j = start; j < end; j++) {
      const v = Number(levels[j]);
      if (Number.isFinite(v)) { sum += v * v; count++; }
    }
    out[i] = count ? Math.sqrt(sum / count) : 0;
  }
  return out;
}

// Auto-gain glissant + courbe gamma appliqués à un groupe de niveaux RMS 0..1.
//
// Principe audio-classique peak-hold : on maintient un pic RMS récent
// (fenêtre ~4 s) — montée immédiate si le pic dépasse (attaque rapide),
// décroissance lente sinon (relâchement). Chaque niveau est ensuite normalisé
// par ce pic : la voix normale (RMS ~0.05–0.15) remplit ainsi toute la plage et
// les rayons oscillent nettement entre ~30 et ~90 % de leur longueur. Le
// plancher (RING.floor) est soustrait comme un passe-haut : en silence pur les
// rayons restent au minimum (pas de bruit amplifié). Une courbe gamma v^0.55
// rehausse encore les petites amplitudes. Aucun pompage : l'attaque monte vite
// mais la référence ne retombe que lentement.
function adaptiveGain(out) {
  const f = RING.floor;
  // Pic du frame courant (la plus forte amplitude parmi les rayons).
  let framePeak = 0;
  for (let i = 0; i < out.length; i++) if (out[i] > framePeak) framePeak = out[i];
  framePeak = Math.max(framePeak, f);

  // Attaque rapide : le pic s'aligne immédiatement s'il dépasse la référence.
  if (framePeak > RING.peak) {
    RING.peak = framePeak;
  } else {
    // Relâchement lent : décroissance ~ -3,5 % / frame → fenêtre ~4 s.
    RING.peak *= (1 - RING.release);
  }
  // Cap du gain : au repos le plancher bruité du micro peut faire monter le pic
  // jusqu'à >1 et normaliser le bruit ambiant à pleine échelle, saturant
  // l'anneau même en silence. On borne le gain à ~0.06 : le bruit reste près du
  // minimum, seule une vraie voix (RMS nettement plus haut) remplit les rayons.
  const g = Math.min(Math.max(RING.peak, f), 0.06);
  const span = g - f;
  if (span <= 0) {
    for (let i = 0; i < out.length; i++) out[i] = 0;
    return out;
  }
  for (let i = 0; i < out.length; i++) {
    // Passe-haut (soustraire le plancher) puis normalisation par l'étendue.
    const above = out[i] - f;
    let v = above > 0 ? above / span : 0;
    if (v > 1) v = 1;
    out[i] = Math.pow(v, RING.gamma);
  }
  return out;
}

// Reçoit un événement audio WS. Producteur de file : chaque événement pousse
// une frame {levels, recording} dans RING.audioQueue (file bornée — on éjecte
// les plus vieilles en cas de saturation, jamais de croissance infinie).
// La consommation réelle (mise à jour des cibles + AGC) se fait dans la boucle
// rAF, à la cadence réelle des frames (50 ms/frame).
const AUDIO_QUEUE_MAX = 24;   // ≈ 1.2 s de buffer, plafonne le repli de frames

function handleAudio(data) {
  if (!RING.canvas) initRing();
  if (!RING.canvas) return;

  const levels = data && Array.isArray(data.levels) ? data.levels : null;
  if (!levels) return;

  RING.audioQueue.push({
    levels,
    recording: !!(data && data.recording),
  });
  if (RING.audioQueue.length > AUDIO_QUEUE_MAX) {
    RING.audioQueue.splice(0, RING.audioQueue.length - AUDIO_QUEUE_MAX);
  }
}

// Consomme la file via un JITTER BUFFER « temps réel décalé ». Horloge via
// performance.now : on accumule le temps écoulé et on avance d'autant de
// frames que nécessaire, en gardant la dernière comme cible courante.
//
// Principe : le serveur broadcast par paquets (~0.2 s) ; si on consommait dès
// réception, la file se viderait immédiatement et l'anneau figerait entre deux
// paquets (escalier ~5 fps). On attend donc que la file soit garnie (≥3 frames
// ≈ 150 ms) avant de démarrer la lecture, puis on lit à un TAUX ADAPTATIF pour
// garder une profondeur de buffer stable (targetDepth = 4) :
//   - file profonde (8) → lecture accélérée (~25-50 ms) pour rattraper ;
//   - file peu garnie (1-2) → lecture ralentie (~65-90 ms) pour laisser le
//     buffer se remplir ;
//   - file vide → on n'accumule PAS d'horloge (lastFrameTime = now) pour éviter
//     une rafale au paquet suivant.
// Résultat : une cible toutes les ~50-90 ms quel que soit le jitter → mouvement
// CONTINU, sans jamais se vider ni sauter. Latence visuelle résultante ≈
// 150-250 ms — c'est voulu (« temps réel décalé »).
// L'AGC (adaptiveGain) s'applique PAR FRAME jouée, pas seulement sur la
// dernière reçue : le gain suit donc le replay à 20 fps.
function consumeAudioFrames() {
  // Autorité de l'état moteur : moteur arrêté → AUCUNE consommation. updateState
  // a déjà remis les cibles/niveaux à zéro ; on vide le buffer pour qu'une
  // frame retardée consommée APRÈS le state d'arrêt ne rallume pas un halo/hub
  // figé. Au démarrage les frames repoussent et sont consommées normalement.
  if (!(app && app.state && app.state.running)) {
    RING.audioQueue.length = 0;
    RING.lastFrameTime = 0;   // ré-arme l'horloge au (re)démarrage
    return;
  }
  const now = performance.now();
  if (RING.lastFrameTime === 0) {
    RING.lastFrameTime = now;      // amorce l'horloge, rien à consommer encore
    return;
  }

  // Jitter buffer : on ne démarre la lecture qu'une fois la file garnie
  // (≥3 frames ≈ 150 ms). En dessous, on ne consomme RIEN et on n'accumule pas
  // d'horloge (lastFrameTime = now) → pas de rafale au paquet suivant.
  if (RING.audioQueue.length < 3) {
    RING.lastFrameTime = now;
    return;
  }

  // Taux de lecture adaptatif pour garder une profondeur stable (targetDepth).
  // Recalculé à chaque tick : file profonde → accélère, file peu garnie → ralentit.
  const targetDepth = 4;
  const depth = RING.audioQueue.length;
  const rate = Math.max(0.5, Math.min(1.8, targetDepth / Math.max(depth, 1)));
  const interval = RING.frameMs * rate;   // intervalle effectif entre deux frames

  const elapsed = now - RING.lastFrameTime;
  if (elapsed < interval) return;

  let steps = Math.floor(elapsed / interval);
  RING.lastFrameTime += steps * interval;

  let consumed = null;
  // Rejoue au plus `steps` frames (borné par la longueur de la file : on ne
  // « rattrape » jamais plus que le buffer disponible).
  while (steps-- > 0 && RING.audioQueue.length) {
    consumed = RING.audioQueue.shift();
  }
  if (consumed) {
    RING.levels = adaptiveGain(groupRingLevels(consumed.levels));
    RING.recording = consumed.recording;
    RING.dirty = true;
  }
}

// Boucle rAF : consomme la file à cadence réelle, interpole les niveaux
// affichés puis dessine. En reduced-motion : aucun lissage ni halo, redessin
// sur chaque frame consommée.
function ringFrame() {
  RING.raf = requestAnimationFrame(ringFrame);
  if (!RING.ctx || document.hidden) return;

  // La cible courante avance à la cadence des frames (20 fps effectifs) tandis
  // que le rendu reste à 60 fps → mouvement continu, plus d'escalier.
  consumeAudioFrames();

  const reduced = RING.reduced.matches;
  if (reduced) {
    if (!RING.dirty) return;
    RING.dirty = false;
    drawRing(true);
    return;
  }

  const k = RING.lerp;
  for (let i = 0; i < RING.rays; i++) {
    const target = RING.levels[i] || 0;
    const cur = RING.disp[i] || 0;
    RING.disp[i] = cur + (target - cur) * k;
  }
  drawRing(false);
}

// Dessin : 36 rayons autour d'un cercle central. Couleurs --accent /
// --accent-glow lues à chaque frame (thématisation par état). Halo uniquement
// en enregistrement ; rayons au minimum + opacité réduite quand moteur arrêté.
function drawRing(reduced) {
  resizeRing();
  if (!RING.ctx) return;
  const ctx = RING.ctx;
  const dpr = RING.dpr || 1;
  const W = RING.canvas.width;
  const H = RING.canvas.height;
  ctx.clearRect(0, 0, W, H);

  const cs = getComputedStyle(document.body);
  const accent = (cs.getPropertyValue("--accent") || "").trim() || "#8ff0c4";
  const glow = (cs.getPropertyValue("--accent-glow") || "").trim() || accent;

  const cx = W / 2;
  const cy = H / 2;
  const inner = RING.inner * dpr;
  const base = RING.base * dpr;
  const amp = RING.amp * dpr;
  const n = RING.rays;
  const recording = RING.recording;
  const running = !!(app.state && app.state.running);

  // Moteur arrêté (idle/off) : rayons au minimum, opacité réduite.
  ctx.globalAlpha = running ? 1 : 0.45;

  ctx.strokeStyle = accent;
  ctx.lineWidth = Math.max(1, 2 * dpr);
  ctx.lineCap = "round";

  if (recording && !reduced) {
    ctx.shadowColor = glow;
    ctx.shadowBlur = 8 * dpr;
  }

  for (let i = 0; i < n; i++) {
    const v = reduced ? (RING.levels[i] || 0) : (RING.disp[i] || 0);
    const len = base + v * amp;
    const ang = (i / n) * Math.PI * 2 - Math.PI / 2;
    const x0 = cx + Math.cos(ang) * inner;
    const y0 = cy + Math.sin(ang) * inner;
    const x1 = cx + Math.cos(ang) * (inner + len);
    const y1 = cy + Math.sin(ang) * (inner + len);
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  }

  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
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
// Zoom global automatique « fit-vp » : la page s'adapte à la hauteur de la
// fenêtre SANS scroll, par un zoom CSS appliqué au conteneur principal.
//   - zoom = clamp(hauteur_dispo / hauteur_naturelle, 0.85, 1.6) ;
//   - `zoom` CSS supporté (Chromium + Firefox ≥ v126) ; sinon repli transform
//     scale (approx. : largeur compensée) ;
//   - la HAUTEUR NATURELLE DE RÉFÉRENCE est mesurée UNE SEULE FOIS
//     (premier fitZoom après init, à chaque resize, et sur document.fonts.ready) :
//     elle est mémorisée dans `fitBaseNatural` et ne dépend PLUS du contenu
//     courant. Le scale ne se recalcule QUE sur resize/first-run — le zoom ne
//     redézoom plus quand l'historique grandit (le scroll interne absorbe).
//   - AUTO-CORRECTION 2 PASSES : après le verrouillage initial, on mesure le
//     rendu RÉEL du .layout (getBoundingClientRect().height, qui intègre le
//     zoom standardisé + les paddings/marges réels) vs la cible (viewport sous
//     le topbar réel, mesuré via getBoundingClientRect().bottom) et on ajuste
//     lockH de delta/scale (max 2 itérations) — absorbe TOUS les offsets
//     constants sans les deviner.
//   - MutationObserver debouncé sur .col-side : si sa hauteur NATURELLE change
//     de plus de 8px (config rendue, liste registry, feedbacks), on invalide
//     la base et on re-déclenche fitZoom pour que la colonne droite remplisse
//     toujours la hauteur. (Le ResizeObserver ne voyait pas ces changements
//     quand la boîte est plafonnée par max-height : seul le contenu déborde.)
//   - si le contenu dépasse même au zoom min (0.85) → on laisse le scroll
//     normal, on n'écrase jamais le contenu.
// Sans JS (ou .fit-vp absent) : comportement scroll normal existant.
// --------------------------------------------------------------------------
// Source de vérité unique de la fenêtre desktop (≥1024 px) : utilisée par
// fitZoom (ré-engagement), init (bascule de classe) et applyFitVp.
const FIT_VP_MQ = window.matchMedia("(min-width: 1024px)");

let fitZoomTimer = null;
// Hauteur naturelle de référence (px) mesurée une fois, conservée entre deux
// resize. 0 = pas encore mesurée → fitZoom la mesure au passage.
let fitBaseNatural = 0;
// Premier fitZoom différé : on attend le premier renderConfig (pour mesurer la
// base avec le VRAI contenu, pas la colonne encore vide) OU un délai de 300 ms,
// selon ce qui vient en premier. Évite le flash initial en scale 1.6 sur une
// colonne vide puis le re-zoom brutal quand la config se rend.
let fitFirstPending = true;

function zoomSupported() {
  return "zoom" in document.body.style;
}

// Remet à zéro la hauteur de référence quand celle-ci doit être re-mesurée
// (resize, sortie/réentrée dans fit-vp, document.fonts.ready). Le prochain
// fitZoom la mesurera fraîchement.
function invalidateFitBase() {
  fitBaseNatural = 0;
}

// Planifie un re-calcul debouncé (les resize successifs n'empilent pas).
// `remeasure` (true) force la re-mesure de la hauteur de référence — utilisé
// uniquement par les entrées qui doivent re-baser le zoom (resize, first run,
// fonts.ready), jamais par un changement de contenu.
function scheduleFitZoom(remeasure) {
  if (fitZoomTimer) clearTimeout(fitZoomTimer);
  if (remeasure) invalidateFitBase();
  // Premier run : différé jusqu'au premier renderConfig (ou deadline 300 ms).
  if (fitFirstPending) return;
  fitZoomTimer = setTimeout(fitZoom, 150);
}

// Mesure la hauteur naturelle de la colonne de DROITE (.col-side) et la stocke
// comme référence de base. Retourne la valeur mémorisée.
//
// Le zoom est calibré pour que la colonne de droite remplisse exactement la
// hauteur ; la colonne gauche s'adapte (cadre historique réduit). On mesure
// donc UNIQUEMENT .col-side (jamais le .layout entier, qui serait influencé par
// la colonne gauche).
//
// Mesure la hauteur NATURELLE de .col-side (déverrouillée), indépendamment du
// zoom/verrouillage courant. Retourne 0 si indisponible. Ne laisse jamais
// l'état dégradé (restauration systématique en finally). Utilisée par
// measureFitBase ET par le MutationObserver (détection de croissance de
// contenu que le ResizeObserver ne voyait pas quand la boîte est plafonnée).
function measureColSideNatural() {
  const layout = document.querySelector(".layout");
  const colSide = document.querySelector(".col-side");
  if (!layout || !colSide) return 0;
  const savedLayout = {
    zoom: layout.style.zoom,
    transform: layout.style.transform,
    width: layout.style.width,
    height: layout.style.height,
  };
  const savedSide = { height: colSide.style.height, alignSelf: colSide.style.alignSelf };
  layout.style.zoom = "1";
  layout.style.transform = "";
  layout.style.width = "";
  layout.style.height = "auto";   // déverrouille la hauteur pour une mesure fiable
  // La colonne de droite doit être mesurée à sa hauteur NATURELLE : on
  // déverrouille sa hauteur inline et on neutralise le stretch du flex parent
  // (align-items:stretch) qui sinon la forcerait à remplir le conteneur.
  colSide.style.height = "auto";
  colSide.style.alignSelf = "flex-start";
  try {
    return colSide.offsetHeight || 0;
  } finally {
    // Restauration systématique (mesure jamais laissée en état dégradé).
    layout.style.zoom = savedLayout.zoom;
    layout.style.transform = savedLayout.transform;
    layout.style.width = savedLayout.width;
    layout.style.height = savedLayout.height;
    colSide.style.height = savedSide.height;
    colSide.style.alignSelf = savedSide.alignSelf;
  }
}

function measureFitBase() {
  const layout = document.querySelector(".layout");
  const colSide = document.querySelector(".col-side");
  if (!layout || !colSide) return 0;
  const tbEl = document.querySelector(".topbar");
  const topbarBottom = tbEl ? tbEl.getBoundingClientRect().bottom : 0;
  const available = Math.max(220, window.innerHeight - topbarBottom);
  fitBaseNatural = measureColSideNatural() || available;
  return fitBaseNatural;
}

function fitZoom() {
  const layout = document.querySelector(".layout");
  if (!layout) return;

  // Ré-engagement via la MQ desktop (source de vérité unique) :
  //  - hors fenêtre ≥1024 px → on sort de fit-vp (purge des styles inline +
  //    re-mesure à la prochaine entrée) et on s'arrête ;
  //  - dans la fenêtre mais classe absente (ex. sortie pour « config trop
  //    longue ») → on la ré-ajoute et on re-mesure la base fraîchement
  //    (add → mesure → éventuel re-exit, tout synchrone, aucun paint
  //    intermédiaire). La base n'est re-mesurée que sur ces entrées
  //    légitimes, jamais sur un changement de contenu.
  if (!FIT_VP_MQ.matches) {
    if (document.body.classList.contains("fit-vp")) {
      clearFitZoom();
      document.body.classList.remove("fit-vp");
      invalidateFitBase();
    }
    return;
  }
  if (!document.body.classList.contains("fit-vp")) {
    document.body.classList.add("fit-vp");
    invalidateFitBase();
  }

  const tbEl = document.querySelector(".topbar");
  const topbarBottom = tbEl ? tbEl.getBoundingClientRect().bottom : 0;
  const target = Math.max(220, window.innerHeight - topbarBottom);

  // 1) Base mesurée une fois (init / resize / fonts.ready). Le contenu courant
  //    (taille de l'historique…) n'influence jamais cette référence.
  if (!fitBaseNatural) measureFitBase();

  // 2) Échelle cible bornée [0.85, 1.6].
  const MIN = 0.85, MAX = 1.6;
  let scale = target / fitBaseNatural;
  if (scale > MAX) scale = MAX;
  if (scale < MIN) scale = MIN;

  // 3) Même au zoom min on déborde encore → on ne garde PAS d'hybride qui
  //    écrase le contenu : on retire la classe .fit-vp pour retomber sur le
  //    layout de BASE (sticky + scroll interne de l'historique). clearFitZoom
  //    purge les styles inline de hauteur/zoom ; invalidateFitBase force une
  //    re-mesure si une entrée (resize) tente de réactiver fit-vp plus tard.
  if (fitBaseNatural * MIN > target) {
    clearFitZoom();
    document.body.classList.remove("fit-vp");
    invalidateFitBase();
    return;
  }

  // 4) Verrouiller la hauteur du conteneur : le zoom standardisé MULTIPLIE
  //    les longueurs RENDUES (la boîte rendue fait layout × zoom). En fixant
  //    la hauteur à target/scale, une fois multipliée par le zoom elle
  //    occupe exactement l'espace disponible — sinon la page déborderait d'un
  //    facteur scale. Le repli transform fonctionne pareil (le scale agit sur
  //    le rendu final), d'où la largeur élargie inversement pour compenser.
  //
  //    PASSE 1 : verrouillage initial (formule historique).
  let lockH = Math.max(1, Math.round(target / scale));
  applyFitZoom(layout, scale, lockH);

  //    PASSE 2 : AUTO-CORRECTION — mesure le rendu RÉEL du .layout
  //    (getBoundingClientRect().height, qui intègre le zoom standardisé + les
  //    paddings/marges réels) vs la cible (viewport sous le topbar réel). Si
  //    l'écart dépasse 2px, on ajuste lockH de delta/scale et on ré-applique
  //    (max 2 itérations, garde-fou). Ça absorbe TOUS les offsets constants
  //    (paddings, margins, gaps) sans les deviner.
  for (let i = 0; i < 2; i++) {
    const rendered = layout.getBoundingClientRect().height;
    const delta = rendered - target;
    if (Math.abs(delta) <= 2) break;
    lockH = Math.max(1, Math.round(lockH - delta / scale));
    applyFitZoom(layout, scale, lockH);
  }
}

// Applique le verrouillage de hauteur + le zoom (CSS `zoom` ou repli transform).
function applyFitZoom(layout, scale, lockH) {
  if (zoomSupported()) {
    layout.style.height = lockH + "px";
    layout.style.zoom = String(scale);
  } else {
    layout.style.height = lockH + "px";
    layout.style.transformOrigin = "top center";
    layout.style.transform = "scale(" + scale + ")";
    layout.style.width = (100 / scale) + "%";
  }
}

// Réinitialise les styles inline de hauteur/zoom posés par fitZoom (quand la
// classe .fit-vp est retirée, ex. passage sous 1024 px) → retour au scroll
// horizontal normal sans traîner une hauteur verrouillée invalide.
function clearFitZoom() {
  const layout = document.querySelector(".layout");
  if (!layout) return;
  layout.style.zoom = "";
  layout.style.transform = "";
  layout.style.width = "";
  layout.style.height = "";
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
  initRing();
  _initComboboxEvents();
  initLoad();
  connectWS();

  // Zoom global auto : le mode « fit-vp » n'existe que sur DESKTOP (≥1024 px).
  // Sur mobile/tablette on garde le scroll normal (le CSS est lui-même borné
  // par @media). On bascule la classe via matchMedia et on repère le passage
  // de la bordure au resize pour ajouter/retirer la classe + recalculer ; si
  // la classe est retirée on purge les styles inline posés par fitZoom. Le
  // zoom ne se recale QUE sur first-run / resize / fonts.ready (jamais sur un
  // changement de contenu) → la hauteur de référence est re-mesurée ici.
  const fitVpMq = FIT_VP_MQ;
  const applyFitVp = () => {
    const on = fitVpMq.matches;
    document.body.classList.toggle("fit-vp", on);
    if (!on) {
      clearFitZoom();
      invalidateFitBase();            // reset à la sortie de fit-vp
      return;
    }
    scheduleFitZoom(true);            // (re-)mesure + applique le zoom
  };
  applyFitVp();
  if (typeof fitVpMq.addEventListener === "function") {
    fitVpMq.addEventListener("change", applyFitVp);
  } else if (typeof fitVpMq.addListener === "function") {
    fitVpMq.addListener(applyFitVp);   // anciens navigateurs (Safari < 14)
  }
  // Resize : re-base la hauteur de référence puis recalcule le zoom.
  window.addEventListener("resize", () => scheduleFitZoom(true));
  scheduleFitZoom(true);

  // Premier fitZoom différé : attend le premier renderConfig (mesure avec le
  // VRAI contenu) OU 300 ms max, selon ce qui vient en premier — évite le
  // flash initial en scale 1.6 sur une colonne encore vide.
  setTimeout(() => {
    if (fitFirstPending) {
      fitFirstPending = false;
      scheduleFitZoom(true);
    }
  }, 300);

  // MutationObserver debouncé (200ms) sur .col-side : détecte les mutations du
  // CONTENU (config rendue, liste registry, feedbacks, résultat de test
  // serveur…) que le ResizeObserver ne voyait PAS quand la boîte est plafonnée
  // par max-height (seul le contenu déborde, la taille de boîte ne change pas).
  // On compare la hauteur NATURELLE (mesurée déverrouillée) à la base : si elle
  // change de plus de 8px, on invalide la base et on re-déclenche fitZoom.
  // Aucune boucle : fitZoom ne mute que des styles inline sur .layout, jamais
  // le sous-arbre de .col-side observé ici.
  const colSide = document.querySelector(".col-side");
  if (colSide && typeof MutationObserver === "function") {
    let lastSideH = measureColSideNatural();
    let moTimer = null;
    const mo = new MutationObserver(() => {
      if (moTimer) clearTimeout(moTimer);
      moTimer = setTimeout(() => {
        const h = measureColSideNatural();
        if (Math.abs(h - lastSideH) > 8) {
          lastSideH = h;
          invalidateFitBase();
          scheduleFitZoom(true);
        }
      }, 200);
    });
    mo.observe(colSide, { childList: true, subtree: true, characterData: true });
  }

  // Une fois les polices web chargées la hauteur naturelle peut changer : on
  // re-base le zoom une seule fois (rare). Géré en optionnel (vieillissement
  // navigateur) sans jamais faire planter le reste.
  if (document.fonts && typeof document.fonts.ready === "object") {
    document.fonts.ready.then(() => {
      if (!document.body.classList.contains("fit-vp")) return;
      scheduleFitZoom(true);
    }).catch(() => {});
  }

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
