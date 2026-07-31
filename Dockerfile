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

# Copies Render Secret Files (credentials.json, cookies.txt) from /etc/secrets
# into vendor/youtube-clipper/ where the Clipper actually reads them.
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# --timeout 3600, not 0: with gthread the heartbeat only stops if the whole
# worker wedges, so a large finite timeout never kills a long single request
# (SSE streams, slow assemblies) but does bound total wedge time instead of
# letting a fully-stuck worker hang forever.
CMD ["gunicorn", "wsgi:application", \
     "--workers", "1", "--threads", "8", "--worker-class", "gthread", \
     "--timeout", "3600", "--bind", "0.0.0.0:8000"]
