"""The file checks and the disk storage behind support attachments (BE-33). No database."""

import uuid
from pathlib import Path

import pytest

from app.common.errors import InvalidInputError, PayloadTooLargeError
from app.modules.support.attachments import (
    MAX_BYTES,
    MAX_FILENAME,
    MAX_FILES,
    IncomingFile,
    check_files,
    clean_filename,
    content_disposition,
    detect_type,
)
from app.modules.support.storage import LocalDiskStorage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.4\n%%EOF\n"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (PDF, "application/pdf"),
        (b"MZ\x90\x00" + b"\x00" * 64, None),  # a Windows program
        (b"GIF89a" + b"\x00" * 16, None),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", None),
        (b"<html><script>alert(1)</script></html>", None),
        (b"plain text", None),
        (b"", None),
        (b"  %PDF-1.4", None),  # the signature must be the very first bytes
    ],
)
def test_the_type_comes_from_the_first_bytes(data: bytes, expected: str | None) -> None:
    assert detect_type(data) == expected


def test_good_files_are_returned_with_the_detected_type() -> None:
    checked = check_files(
        [IncomingFile("a.png", PNG), IncomingFile("b.jpg", JPEG), IncomingFile("c.pdf", PDF)]
    )

    assert [(f.filename, f.content_type) for f in checked] == [
        ("a.png", "image/png"),
        ("b.jpg", "image/jpeg"),
        ("c.pdf", "application/pdf"),
    ]


def test_the_name_does_not_decide_the_type() -> None:
    [renamed_pdf] = check_files([IncomingFile("photo.png", PDF)])
    assert renamed_pdf.content_type == "application/pdf"

    with pytest.raises(InvalidInputError):
        check_files([IncomingFile("photo.png", b"MZ\x90\x00" + b"\x00" * 64)])


def test_no_files_is_fine_and_too_many_is_not() -> None:
    assert check_files([]) == []
    check_files([IncomingFile(f"{i}.png", PNG) for i in range(MAX_FILES)])
    with pytest.raises(InvalidInputError):
        check_files([IncomingFile(f"{i}.png", PNG) for i in range(MAX_FILES + 1)])


def test_size_limit_is_exact() -> None:
    at_limit = PNG + b"\x00" * (MAX_BYTES - len(PNG))
    check_files([IncomingFile("big.png", at_limit)])

    with pytest.raises(PayloadTooLargeError):
        check_files([IncomingFile("big.png", at_limit + b"\x00")])


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(InvalidInputError):
        check_files([IncomingFile("empty.png", b"")])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd.png", "passwd.png"),
        ("C:\\Users\\me\\scan.png", "scan.png"),
        ("a\r\nb.png", "ab.png"),
        ('we"ird.png', 'we"ird.png'),  # kept: the download header escapes it
        ("  spaced.png  ", "spaced.png"),
        ("...hidden", "hidden"),
        ("чек.pdf", "чек.pdf"),
        ("", "file.png"),
        (None, "file.png"),
        ("../", "file.png"),
        ("\x00\x01", "file.png"),
    ],
)
def test_clean_filename(name: str | None, expected: str) -> None:
    assert clean_filename(name, "image/png") == expected


def test_a_long_name_is_cut_but_keeps_its_extension() -> None:
    name = clean_filename("x" * 500 + ".pdf", "application/pdf")

    assert len(name) == MAX_FILENAME and name.endswith(".pdf")


def test_the_default_name_has_the_right_extension() -> None:
    assert clean_filename(None, "application/pdf") == "file.pdf"
    assert clean_filename(None, "image/jpeg") == "file.jpg"


def test_content_disposition_is_a_download_with_both_names() -> None:
    header = content_disposition("чек за билет.pdf")

    assert header.startswith("attachment;")
    assert 'filename="' in header
    assert (
        "filename*=UTF-8''%D1%87%D0%B5%D0%BA%20%D0%B7%D0%B0%20%D0%B1%D0%B8%D0%BB%D0%B5%D1%82.pdf"
        in header
    )
    # The plain name has nothing that could end the header value or add a header.
    plain = header.split('filename="', 1)[1].split('"', 1)[0]
    assert plain.isascii() and not set(plain) & set('";\r\n/\\')


def test_content_disposition_with_quotes_and_nothing_ascii() -> None:
    assert 'filename="file"' in content_disposition("чек")
    plain = content_disposition('a"b;c.png').split('filename="', 1)[1].split('"', 1)[0]
    assert plain == "a_b_c.png"


# --- the disk storage ---


@pytest.fixture
def disk(tmp_path: Path) -> LocalDiskStorage:
    return LocalDiskStorage(tmp_path / "files")


def test_save_read_delete(disk: LocalDiskStorage) -> None:
    key = str(uuid.uuid4())

    disk.save(key, b"hello")

    assert disk.read(key) == b"hello"
    assert [p.name for p in disk.root.iterdir()] == [key]  # named by the uuid, nothing else
    disk.delete(key)
    with pytest.raises(FileNotFoundError):
        disk.read(key)


def test_saving_again_replaces_the_file_and_leaves_no_temp_file(disk: LocalDiskStorage) -> None:
    key = str(uuid.uuid4())
    disk.save(key, b"one")
    disk.save(key, b"two")

    assert disk.read(key) == b"two"
    assert [p.name for p in disk.root.iterdir()] == [key]


def test_deleting_a_missing_file_is_fine(disk: LocalDiskStorage) -> None:
    disk.delete(str(uuid.uuid4()))


@pytest.mark.parametrize("key", ["../x", "a/b", "..", "", "not-a-uuid", "/etc/passwd"])
def test_only_a_uuid_is_a_key(disk: LocalDiskStorage, key: str) -> None:
    with pytest.raises(ValueError):
        disk.save(key, b"x")
    with pytest.raises(ValueError):
        disk.read(key)
    with pytest.raises(ValueError):
        disk.delete(key)
    assert not disk.root.exists() or list(disk.root.iterdir()) == []
