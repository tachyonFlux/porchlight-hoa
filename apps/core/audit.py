from .models import AuditLog
from .utils import client_ip


def record(request_or_user, action, obj=None, changes=None):
    """Write an audit entry. Accepts a request (preferred) or a bare user/None."""
    actor, ip = None, None
    if hasattr(request_or_user, "META"):
        user = getattr(request_or_user, "user", None)
        actor = user if user is not None and user.is_authenticated else None
        ip = client_ip(request_or_user)
    elif request_or_user is not None:
        actor = request_or_user
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        object_type=obj._meta.label if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "",
        object_repr=str(obj)[:200] if obj is not None else "",
        changes=changes or {},
        ip_address=ip,
    )
