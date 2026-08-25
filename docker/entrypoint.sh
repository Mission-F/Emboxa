#!/bin/sh
set -eu

mkdir -p /data/db /data/archives /data/exports /data/imports /data/secrets /data/logs
chmod 700 /data/secrets 2>/dev/null || true

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
