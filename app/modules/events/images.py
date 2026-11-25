"""The cover image of an event (R-12).

One image per event: PNG, JPEG or WebP, up to 5 MB. As with support attachments (BE-33) the
type comes from the file's first bytes, never from its name or the declared type. The bytes
live in the image storage (EVENT_IMAGES_DIR, a folder inside the attachments volume in
production), one file per image named by its uuid; the event row keeps only that uuid and
the content type.

A new upload gets a new uuid, so the image URL changes with the image and can be cached
forever.
"""

import uuid
from pathlib import Path

from app.common.errors import InvalidInputError, PayloadTooLargeError
from app.core.config import get_settings
from app.modules.support.storage import AttachmentStorage, LocalDiskStorage

MAX_BYTES = 5 * 1024 * 1024

PNG = "image/png"
JPEG = "image/jpeg"
WEBP = "image/webp"
ALLOWED_TYPES = (PNG, JPEG, WEBP)


def detect_type(data: bytes) -> str | None:
    """The content type from the file's signature, or None for anything we do not accept."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    return None


def check_image(data: bytes) -> str:
    """The content type of an acceptable image, or 413 (too big) / 422 (anything else).
    `data` is at most MAX_BYTES + 1 bytes: one byte over is enough to refuse it."""
    if len(data) > MAX_BYTES:
        raise PayloadTooLargeError(f"The image is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise InvalidInputError("The image is empty")
    content_type = detect_type(data)
    if content_type is None:
        raise InvalidInputError("Only PNG, JPEG and WebP images are accepted")
    return content_type


def image_url(event_id: uuid.UUID, image_id: uuid.UUID | None) -> str | None:
    """Absolute link to the image on the API (it changes when the image is replaced)."""
    if image_id is None:
        return None
    base = get_settings().public_api_url.rstrip("/")
    return f"{base}/events/{event_id}/image/{image_id}"


_storage: AttachmentStorage | None = None


def get_storage() -> AttachmentStorage:
    """The storage from settings (EVENT_IMAGES_DIR), created on first use."""
    global _storage
    if _storage is None:
        _storage = LocalDiskStorage(Path(get_settings().event_images_dir))
    return _storage


def set_storage(storage: AttachmentStorage | None) -> None:
    """Replace the storage (tests use a temporary folder); None goes back to settings."""
    global _storage
    _storage = storage
