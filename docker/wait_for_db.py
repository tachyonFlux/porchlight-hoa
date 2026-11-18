"""Wait for the database at container start, and explain clearly when it can't work.

Only a database server that isn't up yet (PostgreSQL starting) is worth waiting for. A
missing setting or a data folder Porchlight can't write to won't fix itself, so those
print the reason and stop straight away.
"""

import os
import sys
import time
from pathlib import Path

# Run as a script, so make the project importable (the folder above docker/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TRIES = 60
DELAY = 2


def fail(message):
    print(f"\nPorchlight can't start: {message}\n", file=sys.stderr, flush=True)
    sys.exit(1)


def describe_permissions(path):
    uid, gid = os.getuid(), os.getgid()
    folder = os.environ.get("PORCHLIGHT_DATA_DIR", "/data")
    try:
        st = os.stat(folder)
        owner = f"owned by {st.st_uid}:{st.st_gid}, mode {oct(st.st_mode & 0o777)[2:]}"
    except OSError:
        owner = "missing"
    return (
        f"it can't write to {path}.\n"
        f"  Porchlight runs as user {uid}:{gid} (PUID/PGID in .env); the data folder {folder} is {owner}.\n"
        "  Set PUID and PGID to the IDs of the user who owns the data folder (on a Synology,\n"
        "  run `id` over SSH; usually PUID=1026, PGID=100), or give that user read/write\n"
        "  permission on the folder, then start the project again."
    )


def main():
    try:
        import django

        django.setup()
    except PermissionError as e:
        fail(describe_permissions(e.filename or "the data folder"))
    except Exception as e:  # settings problems: missing secret key, bad DATABASE_URL, ...
        text = str(e)
        fail(text if ".env" in text else f"{text}\n  Check the settings in .env (see .env.example).")

    from django.db import connection
    from django.db.utils import OperationalError

    last = None
    for _ in range(TRIES):
        try:
            connection.ensure_connection()
            return
        except PermissionError as e:
            fail(describe_permissions(e.filename or "the database file"))
        except OperationalError as e:
            last = e
            if connection.vendor == "sqlite":
                # SQLite is a local file: waiting won't help.
                fail(describe_permissions(connection.settings_dict["NAME"]) + f"\n  (SQLite said: {e})")
            connection.close()
            time.sleep(DELAY)
        except Exception as e:
            fail(f"{type(e).__name__}: {e}\n  Check DATABASE_URL in .env.")
    fail(
        f"the database didn't answer after {TRIES * DELAY} seconds ({last}).\n"
        "  Check DATABASE_URL in .env, and that the db container is running "
        "(COMPOSE_PROFILES=postgres)."
    )


if __name__ == "__main__":
    main()
