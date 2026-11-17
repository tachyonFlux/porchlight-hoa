import pytest

from apps.core.logredact import RedactingLogger, redact


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/login/link/abc123/", "/login/link/…/"),
        ("/claim/confirm/tok/", "/claim/confirm/…/"),
        ("/claim/CODE42/", "/claim/…/"),
        ("/i/tok/", "/i/…/"),
        ("/s/tok/12/", "/s/…/12/"),
        ("/calendar/tok.ics", "/calendar/…"),
        ("/dues/", "/dues/"),
        ("/invoices/", "/invoices/"),
    ],
)
def test_redact(path, expected):
    assert redact(path) == expected


def test_access_log_atoms_hide_tokens():
    from types import SimpleNamespace

    from gunicorn.config import Config

    logger = RedactingLogger(Config())
    environ = {
        "PATH_INFO": "/login/link/secret-token/",
        "QUERY_STRING": "",
        "REQUEST_METHOD": "GET",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "RAW_URI": "/login/link/secret-token/",
    }
    environ["HTTP_REFERER"] = "https://hoa.example.com/s/shared-token/"
    req = SimpleNamespace(
        headers=[("REFERER", "https://hoa.example.com/s/shared-token/")],
        method="GET",
        uri="/login/link/secret-token/",
        path="/login/link/secret-token/",
        query="",
        version=(1, 1),
    )
    resp = SimpleNamespace(status="200 OK", headers=[], sent=10, response_length=10)
    from datetime import timedelta

    atoms = logger.atoms(resp, req, environ, timedelta(seconds=0.1))
    logged = " ".join(str(v) for v in atoms.values())
    assert "secret-token" not in logged and "shared-token" not in logged


def test_django_log_filter_redacts():
    import logging

    from apps.core.logredact import RedactTokensFilter

    record = logging.LogRecord(
        "django.request", logging.WARNING, "", 0, "%s: %s", ("Gone", "/i/tok123/"), None
    )
    RedactTokensFilter().filter(record)
    assert record.getMessage() == "Gone: /i/…/"
