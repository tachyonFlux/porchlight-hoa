from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.core import audit
from apps.core.utils import client_ip
from apps.meetings.services import can_see

from .models import Document, ShareLink, ShareLinkAccess


def visible_documents(user):
    qs = Document.objects.select_related("category")
    if user.is_board:
        return qs
    if can_see(user, "residents"):
        return qs.filter(visibility=Document.Visibility.RESIDENTS)
    return qs.none()


def can_download(user, document):
    return can_see(user, document.visibility)


@transaction.atomic
def create_share_link(user, label, documents, expires_at, max_views=None, request=None):
    board_only = [d for d in documents if d.visibility == Document.Visibility.BOARD]
    if board_only and not user.is_admin:
        raise ValueError("Only an admin can share board-only documents outside the neighborhood.")
    raw, digest = ShareLink.new_token()
    link = ShareLink.objects.create(
        label=label,
        token_hash=digest,
        expires_at=expires_at,
        max_views=max_views,
        created_by=user,
        allow_board_only=bool(board_only),
    )
    link.documents.set(documents)
    audit.record(
        request or user,
        "documents.share_link_created",
        link,
        {
            "documents": [f"{d.title} ({d.get_visibility_display()})" for d in documents],
            "expires": expires_at.isoformat(),
            "max": max_views,
        },
    )
    return link, raw


def revoke(link, user, request=None):
    if link.revoked_at is None:
        link.revoked_at = timezone.now()
        link.save(update_fields=["revoked_at"])
        audit.record(request or user, "documents.share_link_revoked", link)


def log_access(request, link, document=None, allowed=True):
    ShareLinkAccess.objects.create(
        link=link,
        document=document,
        document_title=document.title[:150] if document else "",
        ip_address=client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        allowed=allowed,
    )


def shareable(link, document):
    """A document that became board-only after the link was made isn't served, unless an
    admin made the link knowing it included board-only documents."""
    return document.visibility != Document.Visibility.BOARD or link.allow_board_only


ACCESS_LOG_RETENTION_DAYS = 90


def purge_access_logs(now):
    """Drop visitor IPs and user agents some time after a link stops working."""
    cutoff = now - timedelta(days=ACCESS_LOG_RETENTION_DAYS)
    expired = ShareLink.objects.filter(Q(expires_at__lt=cutoff) | Q(revoked_at__lt=cutoff))
    return ShareLinkAccess.objects.filter(link__in=expired).delete()[0]


def claim_download(link):
    """Count one download against the link's limit. False if the link can't be used.

    The conditional UPDATE makes the limit exact even with simultaneous downloads.
    """
    now = timezone.now()
    qs = ShareLink.objects.filter(pk=link.pk, revoked_at__isnull=True, expires_at__gt=now).filter(
        Q(max_views__isnull=True) | Q(view_count__lt=F("max_views"))
    )
    return qs.update(view_count=F("view_count") + 1, last_viewed_at=now) == 1
