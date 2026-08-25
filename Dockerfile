FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH

RUN groupadd --gid 1000 mailvault \
    && useradd --uid 1000 --gid 1000 --create-home mailvault \
    && python -m venv /opt/venv \
    && mkdir -p /workspace /data \
    && chown -R mailvault:mailvault /workspace /data

WORKDIR /workspace
COPY --chown=mailvault:mailvault requirements.txt /workspace/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /workspace/requirements.txt
COPY --chown=mailvault:mailvault app /workspace/app
COPY --chown=mailvault:mailvault docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

USER mailvault
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=8s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"]
ENTRYPOINT ["/entrypoint.sh"]
