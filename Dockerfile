FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH

RUN groupadd --gid 1000 mailvault \
    && useradd --uid 1000 --gid 1000 --create-home mailvault \
    && python -m venv /opt/venv \
    && mkdir -p /workspace /data

WORKDIR /workspace
COPY requirements.txt /workspace/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /workspace/requirements.txt
COPY app /workspace/app
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/switch-user.py /usr/local/bin/switch-user.py
RUN chmod 755 /entrypoint.sh /usr/local/bin/switch-user.py

# Deliberately no `USER` here: the entrypoint starts as root just long enough to take on the
# ownership of the mounted data folder — which on a NAS belongs to an account this image cannot
# know in advance — and then drops to that uid/gid before running anything. Set PUID/PGID to pin
# it, or a `user:` in compose to skip the root phase entirely.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=8s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"]
ENTRYPOINT ["/entrypoint.sh"]
