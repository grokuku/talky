# -*- coding: utf-8 -*-
"""
app/core/constants.py
=====================
Constantes globales partagées par le moteur, l'API et la configuration.
"""

# Fréquence d'échantillonnage native du modèle Whisper (16 kHz mono)
SAMPLING_RATE = 16000

# Port par défaut du WebSocket temps réel whisper-live (WhisperLive),
# distinct du port REST (8000) : le client stream l'audio PCM16 en direct.
WS_DEFAULT_PORT = 9090

# États possibles du moteur (transmis en temps réel au frontend)
STATE_IDLE = "idle"                 # moteur arrêté, aucune session
STATE_BOOTING = "booting"           # démarrage (micro, hotkeys, ping serveur)
STATE_READY = "ready"               # en attente (écoute du raccourci clavier)
STATE_RECORDING = "recording"       # enregistrement micro en cours
STATE_TRANSCRIBING = "transcribing" # transcription serveur en cours
STATE_SUCCESS = "success"           # transcription réussie (transitoire)
STATE_ERROR = "error"               # erreur (audio, serveur, hotkeys...)
STATE_STOPPING = "stopping"         # arrêt en cours

# Ensemble des états possibles (utile pour l'API / le frontend).
STATES = {
    STATE_IDLE, STATE_BOOTING, STATE_READY, STATE_RECORDING,
    STATE_TRANSCRIBING, STATE_SUCCESS, STATE_ERROR, STATE_STOPPING,
}

# Paramètres dont le changement impose un redémarrage du moteur :
# audio_device -> redémarrage du flux micro (sounddevice).
RELOAD_FIELDS = {"audio_device"}

# Paramètres appliqués "à chaud" sans redémarrage du moteur.
HOT_FIELDS = {
    "model", "language", "task", "vad_filter", "hotkey", "input_mode",
    "inject_text", "add_space", "keep_in_clipboard", "max_history",
    "server_url", "server_api_key", "server_timeout", "ws_port",
    "continuous_mode", "compute_type",
}

# Valeurs par défaut côté serveur whisper-live (affichées dans la
# section « Serveur » du frontend). device reste en lecture seule (imposé
# par le serveur CUDA) ; compute_type est désormais configurable via la
# config du client (voir DEFAULT_CONFIG).
SERVER_DEFAULTS = {
    "device": "cuda",
    "compute_type": "int8",
    "device_index": 0,
}

# Compute types supportés par faster-whisper (CTranslate2).
COMPUTE_TYPES = ["int8", "int8_float16", "float16", "float32"]
