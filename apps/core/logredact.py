"""Keep secret tokens in URLs out of the logs.

Sign-in links, claim links, shared-document links, invoice links and calendar feeds carry a
token in the path. Anyone who can read the container logs could replay them, so the access
log shows them as "…" instead.
"""

import logging
import re

from gunicorn.glogging import Logger

# Unanchored so the full request line and the Referer header are covered too. Over-matching
# (hiding part of an ordinary URL) is harmless in a log.
_TOKEN_PATH = re.compile(r"/(login/link|claim/confirm|claim|i|s|calendar)/[^/?\s\"]+")


def redact(text):
    return _TOKEN_PATH.sub(lambda m: f"/{m.group(1)}/…", text or "")


class RedactingLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        return {k: redact(v) if isinstance(v, str) else v for k, v in atoms.items()}


class RedactTokensFilter(logging.Filter):
    """For Django's own log lines, e.g. "Not Found: /s/<token>/"."""

    def filter(self, record):
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True
