"""Container health check: the web server answers /healthz; the scheduler's heartbeat is fresh."""

import os
import sys
import time
import urllib.request

HEARTBEAT = os.environ.get("PORCHLIGHT_HEARTBEAT_FILE", "/tmp/porchlight-scheduler-heartbeat")  # noqa: S108


def main():
    if os.path.exists(HEARTBEAT):  # this is the scheduler container
        return 0 if time.time() - os.path.getmtime(HEARTBEAT) < 600 else 1
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/healthz", headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310 - fixed local URL
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
