#!/bin/sh
# Porchlight container entrypoint.
#   web        run migrations, then the web server (default)
#   scheduler  wait for migrations, then run daily tasks once a day
#   manage ... run a Django management command, e.g. `manage backup`
set -eu

DATA_DIR="${PORCHLIGHT_DATA_DIR:-/data}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

HEARTBEAT="${PORCHLIGHT_HEARTBEAT_FILE:-/tmp/porchlight-scheduler-heartbeat}"
ROLE_FILE=/tmp/porchlight-role

# Start as root only long enough to make the data folder writable, then drop privileges.
if [ "$(id -u)" = "0" ]; then
    if [ "$PUID" = "0" ]; then
        echo "Warning: PUID=0 runs Porchlight as root. Set PUID and PGID to a regular user's IDs." >&2
    else
        mkdir -p "$DATA_DIR"
        # Fix anything owned by someone else (e.g. files created from a root terminal or
        # restored with sudo), and keep the data private to that user.
        if [ -n "$(find "$DATA_DIR" \( ! -user "$PUID" -o ! -group "$PGID" -o -perm /077 \) -print -quit)" ]; then
            echo "Setting owner of $DATA_DIR to $PUID:$PGID and making it private"
            chown -R "$PUID:$PGID" "$DATA_DIR" || echo "Warning: couldn't change owner of $DATA_DIR" >&2
            chmod -R go-rwx "$DATA_DIR" || echo "Warning: couldn't change permissions of $DATA_DIR" >&2
        fi
        exec gosu "$PUID:$PGID" "$0" "$@"
    fi
fi

# Residents' data: readable only by the owner.
umask 077

wait_for_database() {
    tries=0
    until python -c "import django; django.setup(); from django.db import connection; connection.ensure_connection()" 2>/dev/null; do
        tries=$((tries + 1))
        if [ "$tries" -ge 60 ]; then
            echo "Can't reach the database; check DATABASE_URL." >&2
            exit 1
        fi
        sleep 2
    done
}

role="${1:-web}"
[ "$#" -gt 0 ] && shift
# The health check reads this to know what to check.
echo "$role" > "$ROLE_FILE" 2>/dev/null || true

case "$role" in
    web)
        echo "Starting Porchlight ${PORCHLIGHT_VERSION:-dev}"
        wait_for_database
        python manage.py migrate --noinput
        python manage.py show_setup_token
        exec gunicorn porchlight.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "${PORCHLIGHT_WEB_WORKERS:-2}" \
            --threads "${PORCHLIGHT_WEB_THREADS:-4}" \
            --timeout 120 \
            --max-requests 2000 --max-requests-jitter 200 \
            --access-logfile - \
            --logger-class apps.core.logredact.RedactingLogger \
            "$@"
        ;;
    scheduler)
        touch "$HEARTBEAT" 2>/dev/null || true
        wait_for_database
        # The web container runs migrations; wait until they're done.
        tries=0
        until python manage.py migrate --check >/dev/null 2>&1; do
            touch "$HEARTBEAT" 2>/dev/null || true
            tries=$((tries + 1))
            if [ "$tries" -ge 150 ]; then
                echo "Database migrations haven't finished; is the web container running?" >&2
                exit 1
            fi
            sleep 4
        done
        exec python manage.py scheduler
        ;;
    manage)
        exec python manage.py "$@"
        ;;
    *)
        exec "$role" "$@"
        ;;
esac
