# syntax note: two stages.
#
# The build stage keeps the toolchain (gcc, libpq-dev) that is needed to
# compile wheels. The runtime stage must NOT contain it: a C toolchain in a
# production image is attack surface and roughly 300MB of dead weight.
# psycopg[binary] ships prebuilt wheels, so even the PostgreSQL path does not
# need libpq in the final image.

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt is the single source of truth: alembic (needed by
# `alembic upgrade head` in the fly/render/railway start commands) used to be
# installed separately and unversioned, so the image could drift from what CI
# and the tests resolved.
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is required by HEALTHCHECK below. Nothing else: no gcc, no libpq-dev.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY requirements.txt .
COPY . .

# /data holds the SQLite database, the song store and, on Render/Fly, the
# persistent disk mount point. It must exist and be writable by the runtime
# user before the mount is attached (a bind/volume mount inherits the
# ownership of the image directory when the volume is created from it).
RUN mkdir -p /data /tmp/retro_radio_audio_cache \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app /data /tmp/retro_radio_audio_cache
USER appuser

EXPOSE 8501

# Declared volumes: without them a bare `docker run` silently writes the
# database and the TTS cache into the container layer, where they disappear
# with the container. The deploy targets bind real storage here:
#   fly.toml     -> [[mounts]] /data (fly volume retro_radio_data)
#   render.yaml  -> disk: mountPath /data
#   railway.json -> requiredMountPath /data (attach a volume in the dashboard)
# RETRO_RADIO_DATABASE_URL=sqlite:////data/retro_radio.db
# /tmp/retro_radio_audio_cache is the TTS cache
# (retro_radio/server.py: CACHE_DIR = tempfile.gettempdir() / ...).
VOLUME ["/data", "/tmp/retro_radio_audio_cache"]

# /health returns JSON from a real API endpoint (/ serves the SPA HTML, so it
# is not a valid health check).
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8501/health || exit 1

# Migrations run at startup (on a Machine/container that has the volume
# attached) rather than in a Fly release_command, which does not get volumes.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501"]
