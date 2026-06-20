# Python 3.13 — cloakbrowser has no 3.14 build.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Pin the stealth Chromium under /opt so it's independent of $HOME, and
    # never re-download at runtime (the binary is baked into the image below).
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser \
    CLOAKBROWSER_AUTO_UPDATE=false \
    # Chromium can't use its sandbox inside a container — see browser._launch_args.
    CHROMIUM_NO_SANDBOX=true

WORKDIR /app

# Python deps + the OS libraries Chromium needs. cloakbrowser pulls in Playwright,
# so `playwright install-deps chromium` installs the exact runtime libraries.
COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && pip install -r requirements.txt \
 && python -m playwright install-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# Bake the ~200MB stealth Chromium into the image so startup needs no download.
RUN python -c "import cloakbrowser; print('chromium ->', cloakbrowser.ensure_binary())"

COPY . .

EXPOSE 5000

# Bind 0.0.0.0 (not the app.py __main__ default of 127.0.0.1) so the port is
# reachable from the host. Login still starts automatically on boot (lifespan).
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
