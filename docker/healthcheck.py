"""Container health check: the web server answers /healthz; the scheduler's heartbeat is fresh."""

import os
import sys
import time
import urllib.request

HEARTBEAT = os.environ.get("PORCHLIGHT_HEARTBEAT_FILE", "/tmp/porchlight-scheduler-heartbeat")  # noqa: S108
ROLE_FILE = "/tmp/porchlight-role"  # noqa: S108 - written by entrypoint.sh
MAX_HEARTBEAT_AGE = 600


def role():
    try:
        with open(ROLE_FILE) as f:
            return f.read().strip()
    except OSError:
        return "web"


def main():
    if role() == "scheduler":
        try:
            return 0 if time.time() - os.path.getmtime(HEARTBEAT) < MAX_HEARTBEAT_AGE else 1
        except OSError:
            return 1
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/healthz", headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310 - fixed local URL
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
