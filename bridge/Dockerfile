# syntax=docker/dockerfile:1
# Build wheels (compilers live only in this stage) ...
FROM python:3.12-slim AS wheels

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /wheels
COPY requirements.txt /wheels/requirements.txt
RUN pip wheel --wheel-dir /wheels -r /wheels/requirements.txt

# ... and install them into a clean runtime image with no build tools.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY --from=wheels /wheels /wheels
RUN pip install --no-index --find-links=/wheels -r /wheels/requirements.txt \
 && rm -rf /wheels

RUN groupadd --gid 10001 bridge \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin bridge

COPY relay /app/relay

USER 10001:10001
ENTRYPOINT ["python", "-m", "relay"]
