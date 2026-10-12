from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from .models import HOASettings


def send_templated(to, template, context, reply_to=None):
    """Render templates/<template>_subject.txt, _body.txt and (optional) _body.html, then send.

    `to` may be a single address or a list. Returns the number of messages sent.
    """
    hoa = HOASettings.load()
    ctx = {"hoa": hoa, "base_url": settings.BASE_URL, **context}
    subject = " ".join(render_to_string(f"{template}_subject.txt", ctx).split())
    body = render_to_string(f"{template}_body.txt", ctx)
    recipients = [to] if isinstance(to, str) else list(to)
    if not reply_to and hoa.contact_email:
        reply_to = [hoa.contact_email]
    msg = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
        reply_to=reply_to,
    )
    try:
        msg.attach_alternative(render_to_string(f"{template}_body.html", ctx), "text/html")
    except Exception:  # noqa: S110 - HTML part is optional
        pass
    return msg.send()
