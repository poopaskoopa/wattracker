# wattracker server: the storage and web UI half of a server/client install.
#
# The Zwift folders and the Bluetooth trainer are NOT in here - they are on the
# machine where Zwift runs, reached through a connector that dials out to this
# container. See README "Server and connector".
#
# Multi-arch: numpy/pandas/scipy all publish manylinux wheels for amd64 and
# arm64, so neither stage needs a compiler and a Raspberry Pi or an ARM NAS
# builds exactly like an x86 server.
#
#   docker buildx build --platform linux/amd64,linux/arm64 -t wattracker .

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Copy only what the build needs before the source, so a code change does not
# invalidate the dependency layer.
COPY pyproject.toml README.md ./
COPY wattracker ./wattracker
COPY wattracker_connector ./wattracker_connector

# Built into a venv that is copied wholesale into the runtime stage, which
# keeps pip, its caches and the build metadata out of the final image.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

# curl is for the healthcheck only. tini reaps zombies and turns SIGTERM into
# something uvicorn actually shuts down on, so `docker stop` is clean rather
# than a ten-second kill - which matters here, because an unclean stop can
# leave the SQLite WAL needing recovery.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root. The data directory is chowned at build time; the volume inherits
# that ownership when Docker creates it.
RUN useradd --create-home --uid 10001 wattracker \
    && mkdir -p /data \
    && chown -R wattracker:wattracker /data

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # The database, config.json, backups and the credential key all live here.
    # Mount a volume on it or the whole install is lost with the container.
    WATTRACKER_DATA_DIR=/data \
    # The Zwift folders and BLE hardware are on the connector's machine.
    WATTRACKER_MODE=server \
    # Bind every interface *inside the container's* network namespace - what
    # is actually published to the host is decided by -p / compose. Both
    # variables are required; see config.allow_non_loopback for why.
    WATTRACKER_HOST=0.0.0.0 \
    WATTRACKER_ALLOW_NON_LOOPBACK=1 \
    WATTRACKER_PORT=8000 \
    # There is no browser and no desktop keychain in here. credstore falls
    # back to its authenticated encrypted-file backend, whose key lives in
    # /data - so that key file is part of the backup set.
    WATTRACKER_OPEN_BROWSER=0 \
    WATTRACKER_KEYRING=0

USER wattracker
WORKDIR /home/wattracker
VOLUME ["/data"]
EXPOSE 8000

# /login is unauthenticated and renders the full template stack, so a 200 here
# means the app is genuinely serving rather than merely listening.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/login >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "wattracker"]
