"""Checks on the files sent with a support message (BE-33).

A file is accepted by what it is, not by what it is called: the type comes from its first
bytes (PNG, JPEG or PDF signature), and the name and the type the client declared are not
trusted. A .exe renamed to .png is refused. One bad file refuses the whole message, so
nothing is half saved.
"""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from app.common.errors import InvalidInputError, PayloadTooLargeError

MAX_FILES = 3
MAX_BYTES = 5 * 1024 * 1024  # per file
MAX_FILENAME = 120

PNG = "image/png"
JPEG = "image/jpeg"
PDF = "application/pdf"
ALLOWED_TYPES = (PNG, JPEG, PDF)
_EXTENSION = {PNG: ".png", JPEG: ".jpg", PDF: ".pdf"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class IncomingFile:
    """A file as the router read it: the name the client gave and the bytes (at most
    MAX_BYTES + 1 of them, so an oversized file is noticed without reading all of it)."""

    filename: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class CheckedFile:
    filename: str
    content_type: str
    data: bytes


def detect_type(data: bytes) -> str | None:
    """The content type from the file's signature, or None for anything we do not accept."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith(b"%PDF-"):
        return PDF
    return None


def clean_filename(name: str | None, content_type: str) -> str:
    """A safe name to show and to offer on download: no folders, no control characters."""
    name = unicodedata.normalize("NFC", name or "")
    name = _CONTROL.sub("", name.replace("\\", "/").rsplit("/", 1)[-1])
    name = name.strip(" .")
    if not name:
        return "file" + _EXTENSION[content_type]
    if len(name) > MAX_FILENAME:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: MAX_FILENAME - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_FILENAME]
    return name


def check_files(files: list[IncomingFile]) -> list[CheckedFile]:
    """The files to store, or an error for the first problem (413 too big, 422 otherwise)."""
    if len(files) > MAX_FILES:
        raise InvalidInputError(f"At most {MAX_FILES} files per message")
    checked = []
    for file in files:
        if len(file.data) > MAX_BYTES:
            raise PayloadTooLargeError(f"A file is larger than {MAX_BYTES // (1024 * 1024)} MB")
        if not file.data:
            raise InvalidInputError("A file is empty")
        content_type = detect_type(file.data)
        if content_type is None:
            raise InvalidInputError("Only PNG, JPEG and PDF files are accepted")
        checked.append(
            CheckedFile(clean_filename(file.filename, content_type), content_type, file.data)
        )
    return checked


def content_disposition(filename: str) -> str:
    """Header that makes the browser save the file instead of opening it. The plain name is
    ASCII only (for old clients); filename* carries the real one (RFC 5987)."""
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename.encode("ascii", "ignore").decode())
    return f"attachment; filename=\"{fallback or 'file'}\"; filename*=UTF-8''{quote(filename)}"
