#!/bin/sh
# Prepares /data, then hands over to uvicorn as an unprivileged user.
#
# The container is designed to be started by dropping a whole project folder onto a NAS: the data
# folder that arrives with it belongs to whatever account copied it (a Synology user is typically
# uid 1026+, not this image's 1000), so the app adopts that owner instead of forcing its own uid
# and dying with "Operation not permitted" on the first write. PUID/PGID override the detection.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
DATA_SUBDIRS="db archives exports imports local-imports local-exports secrets logs"
IS_ROOT=0
[ "$(id -u)" = "0" ] && IS_ROOT=1

if [ "$IS_ROOT" = "1" ]; then
  mkdir -p "$DATA_DIR" 2>/dev/null || true
  detected_uid="$(stat -c %u "$DATA_DIR" 2>/dev/null || echo 1000)"
  detected_gid="$(stat -c %g "$DATA_DIR" 2>/dev/null || echo 1000)"
  RUN_UID="${PUID:-$detected_uid}"
  RUN_GID="${PGID:-$detected_gid}"
else
  # Started with an explicit `user:` in compose; take what we were given.
  RUN_UID="$(id -u)"
  RUN_GID="$(id -g)"
fi

for dir in $DATA_SUBDIRS; do
  mkdir -p "$DATA_DIR/$dir" 2>/dev/null || true
done

if [ "$IS_ROOT" = "1" ]; then
  # Top level only. A recursive chown would rewrite every archived message on every start, which
  # on a mailbox archive of any real size turns a restart into a multi-minute operation.
  chown "$RUN_UID:$RUN_GID" "$DATA_DIR" 2>/dev/null || true
  for dir in $DATA_SUBDIRS; do
    chown "$RUN_UID:$RUN_GID" "$DATA_DIR/$dir" 2>/dev/null || true
  done
fi
chmod 700 "$DATA_DIR/secrets" 2>/dev/null || true

# Compose passes DATABASE_URL through even when nobody set it, so an empty value means "not
# configured" and we keep using whichever database this data folder already contains. Getting this
# wrong would silently open an empty database next to the real one and look like total data loss.
if [ -z "${DATABASE_URL:-}" ]; then
  if [ -f "$DATA_DIR/db/emboxa-web.db" ]; then
    DATABASE_URL="sqlite:///$DATA_DIR/db/emboxa-web.db"
  elif [ -f "$DATA_DIR/db/mailvault.db" ]; then
    DATABASE_URL="sqlite:///$DATA_DIR/db/mailvault.db"
  else
    DATABASE_URL="sqlite:///$DATA_DIR/db/emboxa-web.db"
  fi
  export DATABASE_URL
fi

echo "EMBOXA: data=$DATA_DIR user=$RUN_UID:$RUN_GID database=${DATABASE_URL##*/}"

set -- python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"

if [ "$IS_ROOT" = "1" ] && [ "$RUN_UID" != "0" ]; then
  exec python /usr/local/bin/switch-user.py "$RUN_UID" "$RUN_GID" "$@"
fi

exec "$@"
