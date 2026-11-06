#!/bin/sh
# Porchlight container entrypoint.
#   web        run migrations, then the web server (default)
#   scheduler  wait for migrations, then run daily tasks once a day
#   manage ... run a Django management command, e.g. `manage backup`
set -eu

DATA_DIR="${PORCHLIGHT_DATA_DIR:-/data}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Start as root only long enough to make the data folder writable, then drop privileges.
if [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u:%g "$DATA_DIR")" != "$PUID:$PGID" ]; then
        echo "Setting owner of $DATA_DIR to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$DATA_DIR" || echo "Warning: couldn't change owner of $DATA_DIR" >&2
    fi
    exec gosu "$PUID:$PGID" "$0" "$@"
fi

umask 027

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

case "$role" in
    web)
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
            "$@"
        ;;
    scheduler)
        wait_for_database
        # The web container runs migrations; wait until they're done.
        tries=0
        until python manage.py migrate --check >/dev/null 2>&1; do
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
