"""Where support attachment files live (BE-33).

The service talks to the AttachmentStorage interface only, so the disk can be replaced later
by an S3-compatible store without touching it. The database keeps the metadata (name, type,
size); the bytes live here, under a key that is the attachment's id.

LocalDiskStorage: one file per attachment in ATTACHMENTS_DIR, named by the id (a uuid), so
nothing a user typed ever reaches the file system.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

logger = logging.getLogger("app.support")


class AttachmentStorage(Protocol):
    def save(self, key: str, data: bytes) -> None:
        """Store the bytes under the key, all or nothing."""

    def read(self, key: str) -> bytes:
        """The bytes under the key; FileNotFoundError when there are none."""

    def delete(self, key: str) -> None:
        """Remove the bytes; nothing happens when there are none."""


class LocalDiskStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        # Only a uuid is a valid key: no "..", no slashes, whatever the caller passes.
        return self.root / str(uuid.UUID(key))

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        self.root.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed, so a reader never sees half a file.
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


_storage: AttachmentStorage | None = None


def get_storage() -> AttachmentStorage:
    """The storage from settings (ATTACHMENTS_DIR), created on first use."""
    global _storage
    if _storage is None:
        _storage = LocalDiskStorage(Path(get_settings().attachments_dir))
    return _storage


def set_storage(storage: AttachmentStorage | None) -> None:
    """Replace the storage (tests use a temporary folder); None goes back to settings."""
    global _storage
    _storage = storage


def delete_quietly(storage: AttachmentStorage, keys: list[str]) -> None:
    """Clean up after a failed request: a file that cannot be removed is logged, not raised,
    so the original error is the one the caller sees."""
    for key in keys:
        try:
            storage.delete(key)
        except OSError:
            logger.exception("could not remove attachment file %s", key)
