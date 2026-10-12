"""Upload validation and private file serving.

Uploads live under MEDIA_ROOT and are never served by URL directly; views check
permissions and then call `serve_private`.
"""

import mimetypes
import os
import uuid
import zipfile

from django import forms
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
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"}
DOCUMENT_TYPES = ("pdf", "docx", "xlsx", "txt", "md", "png", "jpg", "jpeg")
IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp", "heic")


def _is_office_zip(f):
    """Word/Excel files are ZIPs that contain [Content_Types].xml."""
    try:
        with zipfile.ZipFile(f) as archive:
            return "[Content_Types].xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False
    finally:
        f.seek(0)


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
    if ext == "webp" and head[8:12] != b"WEBP":
        raise ValidationError("That file's contents don't match its type.")
    if ext == "heic" and (head[4:8] != b"ftyp" or head[8:12] not in HEIC_BRANDS):
        raise ValidationError("That file's contents don't match its type.")
    if ext in ("docx", "xlsx") and not _is_office_zip(f):
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


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    """Accepts several images in one input; validates each like validate_upload."""

    def __init__(self, *args, max_files=10, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": "image/*", "multiple": True}))
        kwargs.setdefault("required", False)
        self.max_files = max_files
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        if self.required and not files:
            raise ValidationError(self.error_messages["required"], code="required")
        if len(files) > self.max_files:
            raise ValidationError(f"Please add at most {self.max_files} photos.")
        cleaned = [super(MultipleImageField, self).clean(f, initial) for f in files]
        for f in cleaned:
            validate_upload(f, allowed=IMAGE_TYPES)
        return [strip_image_metadata(f) for f in cleaned]


# Formats Pillow can re-encode. Re-encoding drops EXIF (GPS location, device serial...)
# and anything appended to the file. HEIC can't be decoded without extra libraries, so
# those are stored as uploaded.
_REENCODE = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF"}
MAX_IMAGE_PIXELS = 50_000_000  # ~50 megapixels; bigger is rejected as a possible image bomb


def strip_image_metadata(f):
    """Return a cleaned copy of an uploaded image (same name), or the original if not re-encodable."""
    import io
    import warnings

    from django.core.files.base import ContentFile
    from PIL import Image, ImageOps

    ext = os.path.splitext(f.name)[1].lower().lstrip(".")
    fmt = _REENCODE.get(ext)
    if fmt is None:
        return f
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(f) as img:
                if fmt == "GIF" and getattr(img, "is_animated", False):
                    f.seek(0)
                    return f  # keep animation; GIFs don't carry EXIF location data
                img = ImageOps.exif_transpose(img)  # keep the photo the right way up
                if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                out = io.BytesIO()
                save_kwargs = {"quality": 90} if fmt in ("JPEG", "WEBP") else {}
                img.save(out, fmt, **save_kwargs)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError("That image is too large to process.") from exc
    except Exception as exc:  # truncated or corrupt image
        raise ValidationError("We couldn't read that image. Try a different file.") from exc
    return ContentFile(out.getvalue(), name=os.path.basename(f.name))
