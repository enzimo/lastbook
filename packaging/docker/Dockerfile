# syntax=docker/dockerfile:1
# Lastbook as a web service: server.py only. No desktop shells (app/), no Linux package, no local AI (packaging/llm, llm_local.py).
# docs/WEB-SERVICE.md has the settings and a Docker Swarm example. Build from the repository root:
#
#   docker build --pull -t lastbook:latest -f packaging/docker/Dockerfile .
#
# The data volume is mounted at /data. The library is /data/LastbookData: setup downloads beside it and switches by renaming,
# so it must be a folder inside the volume, not the mount itself.
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
FROM ${PYTHON_IMAGE}

# ORT_DISABLE_TELEMETRY: retrieval.py sets it too, before importing onnxruntime; here it also covers anything else that imports it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORT_DISABLE_TELEMETRY=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY packaging/docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# A fixed user, so the data folders on the host can be owned by it (docs/WEB-SERVICE.md).
ARG UID=1000
ARG GID=1000
RUN groupadd --gid "$GID" lastbook && useradd --uid "$UID" --gid "$GID" --no-create-home --home-dir /data --shell /usr/sbin/nologin lastbook

WORKDIR /app
COPY server.py assistant.py retrieval.py LICENSE THIRD_PARTY_NOTICES.md ./
COPY engine engine
COPY web web
COPY content content

# The web service: setup through the browser for whoever holds the access key, the key cookie over HTTPS only, and setup's own
# files (job, staged switch, access key) beside the library. LASTBOOK_ALLOWED_HOSTS and the AI settings come from the deployment.
ENV LASTBOOK_REMOTE_SETUP=1 \
    LASTBOOK_COOKIE_SECURE=1 \
    LASTBOOK_SETTINGS=/data/settings

USER lastbook
EXPOSE 8890
# Loopback is always let in, so this needs no key. The first start reads the catalog before it answers: give it time.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10m --retries=5 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8890/', timeout=8)"]
ENTRYPOINT ["python3", "/app/server.py"]
CMD ["--data", "/data/LastbookData", "--port", "8890", "--lan"]
