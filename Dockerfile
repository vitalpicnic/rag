FROM python:3.12.15-slim-bookworm@sha256:9901e0a8d75037d8242ed43155cbcb2d1f61be1356383d8054afb59fd50e39c4 AS base
ARG DEBIAN_SNAPSHOT=20261003T000000Z
RUN rm /etc/apt/sources.list.d/debian.sources && \
    printf 'deb https://snapshot.debian.org/archive/debian/%s bookworm main\ndeb https://snapshot.debian.org/archive/debian-security/%s bookworm-security main\n' "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y --no-install-recommends git libgomp1 && \
    rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH MPLCONFIGDIR=/tmp/matplotlib HF_HOME=/tmp/huggingface
WORKDIR /app

FROM base AS runtime-builder
COPY deploy/requirements-linux.lock /tmp/requirements.lock
RUN python -m venv /opt/venv && pip install --no-cache-dir --only-binary=:all: --require-hashes \
    --extra-index-url https://download.pytorch.org/whl/cpu -r /tmp/requirements.lock && pip check

FROM base AS adapter-builder
COPY deploy/requirements-adapters.lock /tmp/requirements.lock
RUN python -m venv /opt/venv && pip install --no-cache-dir --only-binary=:all: --require-hashes \
    -r /tmp/requirements.lock && pip check

FROM base AS runtime
COPY --from=runtime-builder /opt/venv /opt/venv
COPY --chown=10001:10001 . /app
USER 10001:10001
CMD ["python", "-m", "scripts.serve_runtime"]

FROM base AS adapter
COPY --from=adapter-builder /opt/venv /opt/venv
COPY --chown=10001:10001 . /app
USER 10001:10001
CMD ["python", "main.py"]
