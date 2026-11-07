# syntax=docker/dockerfile:1
#
# Obraz dla Azure Container Apps Job na serverless GPU (Consumption-GPU-NC8as-T4).
# W jednym kontenerze: Whisper (PyTorch + CUDA) + lokalna Ollama + orchestrator.
#
# Budowanie (model LLM pobierany przy starcie joba):
#   docker build -t <user>/dnd-notes:0.1 .
# Budowanie z modelem LLM wbudowanym w obraz (obraz +20 GB, ale start bez pobierania):
#   docker build --build-arg OLLAMA_MODEL=gemma4:31b -t <user>/dnd-notes:0.1-gemma31b .

ARG OLLAMA_VERSION=0.40.15
ARG PYTHON_VERSION=3.12

# --- Binarka Ollamy z oficjalnego obrazu --------------------------------------
# Zostawiamy tylko backend CUDA 12 (działa na T4 i starszych sterownikach).
# Wycinamy ROCm (AMD), CUDA 13 i silnik MLX - oszczędza ~3 GB obrazu,
# czyli szybszy start joba.
FROM ollama/ollama:${OLLAMA_VERSION} AS ollama
RUN rm -rf /usr/lib/ollama/rocm* /usr/lib/ollama/cuda_v13 /usr/lib/ollama/mlx_cuda_v13

# --- Obraz docelowy -----------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG WHISPER_MODEL=large-v3
ARG OLLAMA_MODEL=

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # GPU (jak w oficjalnym obrazie Ollamy)
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    # Whisper
    WHISPER_MODEL=${WHISPER_MODEL} \
    WHISPER_CACHE_DIR=/models/whisper \
    # Ollama - serwer tylko lokalnie w kontenerze
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_MODELS=/models/ollama \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_KEEP_ALIVE=30m \
    DOWNLOAD_DIR=/tmp/recordings

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 app \
 && mkdir -p /models/whisper /models/ollama /app \
 && chown -R app:app /models /app

COPY --from=ollama /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama

WORKDIR /app

# Zależności Pythona. Torch z CUDA 12.6 zamiast domyślnego z PyPI (CUDA 13),
# bo CUDA 13 wymaga sterownika >= 580, a wersji sterownika na serverless GPU
# w Azure nie kontrolujemy.
COPY requirements.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cu126 \
 && pip install -r requirements.txt

USER app

# Wagi Whispera wbudowane w obraz (~3 GB dla large-v3), żeby nie pobierać ich
# przy każdym uruchomieniu joba.
RUN python -c "import os, whisper; m=os.environ['WHISPER_MODEL']; whisper._download(whisper._MODELS[m], os.environ['WHISPER_CACHE_DIR'], False)"

# Opcjonalnie: model LLM wbudowany w obraz.
RUN if [ -n "${OLLAMA_MODEL}" ]; then \
      (ollama serve >/tmp/ollama-build.log 2>&1 &) \
      && for i in $(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 1; done \
      && ollama pull "${OLLAMA_MODEL}" \
      && rm -f /tmp/ollama-build.log; \
    fi
ENV OLLAMA_MODEL_NAME=${OLLAMA_MODEL:-gemma4:31b}

COPY --chown=app:app entrypoint.sh orchestrator.py transcribe.py create_notes.py ./

ENTRYPOINT ["/app/entrypoint.sh"]
