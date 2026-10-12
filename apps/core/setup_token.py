"""First-run setup token.

Until setup is finished, anyone who can reach the server could create the first admin.
To prevent that, the wizard asks for a token that only the server operator can see:
PORCHLIGHT_SETUP_TOKEN if set, otherwise a random one written to DATA_DIR/setup-token.txt
and printed to the logs.
"""

import logging
import os
import secrets

from django.conf import settings

log = logging.getLogger("porchlight.setup")
_logged = False


def _path():
    return settings.DATA_DIR / "setup-token.txt"


def required():
    return bool(settings.SETUP_TOKEN) or settings.SETUP_TOKEN_REQUIRED


def get():
    global _logged
    if settings.SETUP_TOKEN:
        return settings.SETUP_TOKEN
    path = _path()
    if not path.exists():
        try:
            # O_EXCL: concurrent workers don't overwrite each other. Mode 0600 from the start,
            # so there's no moment when other local users could read it.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_urlsafe(12))
    token = path.read_text().strip()
    if not _logged:
        log.warning(
            "Porchlight first-run setup token: %s (also in %s). Enter it on the setup page.",
            token,
            path,
        )
        _logged = True
    return token


def matches(value):
    return secrets.compare_digest((value or "").encode(), get().encode())


def discard():
    try:
        _path().unlink()
    except FileNotFoundError:
        pass
