# syntax=docker/dockerfile:1
# Playwright's image: matching browser builds, fonts, Xvfb. Tag must equal the pinned playwright version.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH \
    TZ=Europe/Berlin \
    LANG=de_DE.UTF-8 \
    LANGUAGE=de_DE:de \
    HOME=/home/bot \
    STATE_DIR=/app/state

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Real Google Chrome (stable): a normal, non-"Chrome for Testing" fingerprint.
RUN playwright install --with-deps chrome \
 && (command -v xvfb-run || (apt-get update && apt-get install -y --no-install-recommends xvfb xauth)) \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /home/bot /app/state && chmod 1777 /home/bot /app/state

COPY src ./src
RUN uv sync --frozen --no-dev

ENV BROWSER_CHANNEL=chrome HEADLESS=false
ENTRYPOINT ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24 -nolisten tcp", "python", "-m", "alditalk"]
CMD ["run"]
