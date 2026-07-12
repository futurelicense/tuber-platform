FROM python:3.12-slim

# ffmpeg: used by both vendored apps via subprocess (clip cutting, TTS/video assembly).
# nodejs: used by yt-dlp's JS-runtime bot-detection bypass (Clipper's _find_js_runtime()).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-clipper.txt requirements-ytproduction.txt ./
RUN pip install --no-cache-dir \
    -r requirements.txt \
    -r requirements-clipper.txt \
    -r requirements-ytproduction.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["gunicorn", "wsgi:application", \
     "--workers", "1", "--threads", "8", "--worker-class", "gthread", \
     "--timeout", "0", "--bind", "0.0.0.0:8000"]
