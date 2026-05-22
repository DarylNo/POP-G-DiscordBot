FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# Install other deps first, then force CUDA torch last so openai-whisper
# can't pull in a CPU-only torch during dependency resolution
RUN pip install --no-cache-dir -r requirements.txt
# CUDA 11.8 — last version with Pascal (sm_61) support, required for Quadro P2000
RUN pip install --no-cache-dir --force-reinstall torch --extra-index-url https://download.pytorch.org/whl/cu118
COPY . .
CMD ["python3", "bot.py"]
