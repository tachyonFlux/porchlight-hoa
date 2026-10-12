import logging
import threading
from functools import lru_cache

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template import Context, Engine, TemplateDoesNotExist
from django.template.loader import render_to_string

from .models import HOASettings

log = logging.getLogger("porchlight.email")

BULK_CHUNK = 50  # messages per SMTP connection; many relays cap a connection at ~100


@lru_cache(maxsize=1)
def _text_engine():
    """Same template search path, but without HTML autoescaping: plain-text emails should
    say "Tom & Jerry's", not "Tom &amp; Jerry&#x27;s". (The optional HTML part keeps it.)"""
    return Engine(
        dirs=[str(settings.BASE_DIR / "templates")],
        app_dirs=True,
        autoescape=False,
        libraries={"porchlight": "apps.core.templatetags.porchlight"},
    )


def render_text(name, ctx):
    return _text_engine().get_template(name).render(Context(ctx, autoescape=False))


def _render(template, context, reply_to):
    hoa = HOASettings.load()
    ctx = {"hoa": hoa, "base_url": settings.BASE_URL, **context}
    subject = " ".join(render_text(f"{template}_subject.txt", ctx).split())
    body = render_text(f"{template}_body.txt", ctx)
    try:
        html = render_to_string(f"{template}_body.html", ctx)
    except TemplateDoesNotExist:
        html = None  # HTML part is optional
    if not reply_to and hoa.contact_email:
        reply_to = [hoa.contact_email]
    return subject, body, html, reply_to


def _message(subject, body, html, to, reply_to):
    msg = EmailMultiAlternatives(subject, body, settings.DEFAULT_FROM_EMAIL, to, reply_to=reply_to)
    if html:
        msg.attach_alternative(html, "text/html")
    return msg


def _send_quietly(msg):
    try:
        msg.send()
    except Exception:
        log.exception("Background email to %s failed", msg.to)


def send_templated(to, template, context, reply_to=None, background=False):
    """Render templates/<template>_subject.txt, _body.txt and (optional) _body.html, then send.

    `to` may be a single address or a list. Returns the number of messages sent.
    With background=True (and EMAIL_ASYNC on), SMTP happens in a thread so the response
    time doesn't depend on whether an email was sent.
    """
    subject, body, html, reply_to = _render(template, context, reply_to)
    msg = _message(subject, body, html, [to] if isinstance(to, str) else list(to), reply_to)
    if background and settings.EMAIL_ASYNC:
        threading.Thread(target=_send_quietly, args=(msg,), daemon=True).start()
        return 1
    return msg.send()


def _send_batch(messages):
    """Send each message on its own so one refused address can't stop the rest."""
    for start in range(0, len(messages), BULK_CHUNK):
        chunk = messages[start : start + BULK_CHUNK]
        try:
            connection = get_connection()
            connection.open()
        except Exception:
            log.exception("Couldn't connect to send %d emails", len(chunk))
            continue
        try:
            for msg in chunk:
                try:
                    connection.send_messages([msg])
                except Exception:
                    log.exception("Email to %s failed", msg.to)
                    try:  # the connection may be unusable after an error
                        connection.close()
                        connection.open()
                    except Exception:
                        log.exception("Couldn't reconnect; skipping rest of batch")
                        break
        finally:
            try:
                connection.close()
            except Exception:  # noqa: S110 - closing a broken connection
                pass


def send_bulk(recipients, template, context, reply_to=None):
    """Send one copy per recipient (nobody sees anyone else's address).

    Runs in a background thread when EMAIL_ASYNC is on, so big sends don't block the page.
    Returns the number of messages queued.
    """
    subject, body, html, reply_to = _render(template, context, reply_to)
    messages = [_message(subject, body, html, [a], reply_to) for a in sorted(set(recipients)) if a]
    if not messages:
        return 0
    if settings.EMAIL_ASYNC:
        threading.Thread(target=_send_batch, args=(messages,), daemon=True).start()
    else:
        _send_batch(messages)
    return len(messages)


BULK_EMAILS_PER_DAY = 10


def bulk_email_allowed(user):
    """Backstop: each board member can send at most 10 mass emails a day."""
    from .ratelimit import hit

    return hit("bulk-email", user.pk, limit=BULK_EMAILS_PER_DAY, period=86400)
