# Talky — serveur de transcription maison (faster-whisper, INT8, CUDA)
# =====================================================================
# Conteneur 100 % maîtrisé : REST OpenAI-compatible + WebSocket temps réel
# (protocole compatible client talky) + installation de modèles depuis le
# client. compute_type configurable via TALKY_COMPUTE_TYPE (défaut : int8).
#
# Build :  docker build -t talky-server .
# Run   :  voir docker-compose.yml (GPU requis, ports 8000 + 9090 -> 9090).
FROM python:3.11-slim

# ffmpeg n'est pas requis (le client envoie du WAV PCM16), mais libgomp1
# (OpenMP) l'est pour CTranslate2.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Torch CUDA installé AVANT les dépendances (gros wheel, cache pip dédié).
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cu128

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Cache des modèles HuggingFace (volume persistant).
ENV HF_HOME=/var/lib/whisper-live \
    HF_HUB_CACHE=/var/lib/whisper-live \
    TALKY_MODEL=large-v3-turbo \
    TALKY_COMPUTE_TYPE=int8
VOLUME ["/var/lib/whisper-live"]

# Un seul port interne sert REST + WebSocket ; le compose mappe 8000 et 9090
# (hôtes) vers ce port.
EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9090/docs', timeout=3)" || exit 1

CMD ["python", "server.py"]
