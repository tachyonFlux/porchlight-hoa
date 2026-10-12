"""Upload validation and private file serving.

Uploads live under MEDIA_ROOT and are never served by URL directly; views check
permissions and then call `serve_private`.
"""

import mimetypes
import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404
from django.utils.deconstruct import deconstructible

# extension -> (content type, magic-byte prefixes that must match; empty = text, checked below)
ALLOWED = {
    "pdf": ("application/pdf", (b"%PDF-",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", (b"RIFF",)),
    "heic": ("image/heic", ()),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", (b"PK\x03\x04",)),
    "txt": ("text/plain", ()),
    "md": ("text/markdown", ()),
}
DOCUMENT_TYPES = ("pdf", "docx", "xlsx", "txt", "md", "png", "jpg", "jpeg")
IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp", "heic")


def validate_upload(f, allowed=DOCUMENT_TYPES, max_mb=None):
    """Check extension, size and leading bytes. Raises ValidationError."""
    max_mb = max_mb or settings.MAX_UPLOAD_MB
    ext = os.path.splitext(f.name)[1].lower().lstrip(".")
    if ext not in allowed:
        raise ValidationError(f"Please upload one of: {', '.join(sorted(set(allowed)))}.")
    if f.size > max_mb * 1024 * 1024:
        raise ValidationError(f"That file is too large ({max_mb} MB max).")
    head = f.read(16)
    f.seek(0)
    _, magics = ALLOWED[ext]
    if magics and not any(head.startswith(m) for m in magics):
        raise ValidationError("That file's contents don't match its type.")
    if ext == "heic" and head[4:8] != b"ftyp":
        raise ValidationError("That file's contents don't match its type.")
    if ext in ("txt", "md") and b"\x00" in head:
        raise ValidationError("That doesn't look like a text file.")
    return ext


@deconstructible
class private_upload_path:  # noqa: N801 - used like a function in model fields
    """upload_to callable: random names under private/<prefix>/ so paths reveal nothing."""

    def __init__(self, prefix):
        self.prefix = prefix

    def __call__(self, instance, filename):
        ext = os.path.splitext(filename)[1].lower()
        return f"private/{self.prefix}/{uuid.uuid4().hex}{ext}"

    def __eq__(self, other):
        return isinstance(other, private_upload_path) and other.prefix == self.prefix


def serve_private(field_file, download_name=None, inline=True):
    if not field_file:
        raise Http404
    try:
        handle = field_file.open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    name = download_name or os.path.basename(field_file.name)
    ext = os.path.splitext(field_file.name)[1].lower().lstrip(".")
    content_type = ALLOWED.get(ext, (mimetypes.guess_type(name)[0] or "application/octet-stream",))[0]
    # Only PDFs and images are shown inline; everything else downloads.
    as_attachment = not inline or not (ext == "pdf" or ext in IMAGE_TYPES)
    response = FileResponse(handle, as_attachment=as_attachment, filename=name, content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    response["Cache-Control"] = "private, no-store"
    return response
