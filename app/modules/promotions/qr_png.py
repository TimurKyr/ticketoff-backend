"""PNG picture of a campaign QR, drawn with the QR encoder that reportlab already ships.

No new dependency: reportlab gives the matrix of dark and light squares (the same encoder
that draws the QR on the ticket PDF), and the PNG file is written by hand (it is a small,
fixed format: signature, header, one compressed block of pixels, end marker).
"""

import struct
import zlib

from reportlab.graphics.barcode import qrencoder

# Pixels per square and the empty border around the code, in squares (the QR standard asks for 4).
MODULE_PX = 10
QUIET_ZONE = 4
# Level Q survives smudges and printing, like the ticket QR.
_LEVEL = qrencoder.QRErrorCorrectLevel.Q


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def render_qr_png(value: str) -> bytes:
    """A black-on-white PNG that encodes exactly `value`."""
    qr = qrencoder.QRCode(None, _LEVEL)
    qr.addData(value)
    qr.make()
    count = qr.getModuleCount()
    side = (count + 2 * QUIET_ZONE) * MODULE_PX

    white_row = b"\x00" + b"\xff" * side  # filter byte 0, then one grey byte per pixel
    border = [white_row] * (QUIET_ZONE * MODULE_PX)
    rows = list(border)
    for r in range(count):
        line = bytearray(b"\xff" * (QUIET_ZONE * MODULE_PX))
        for c in range(count):
            line += (b"\x00" if qr.isDark(r, c) else b"\xff") * MODULE_PX
        line += b"\xff" * (QUIET_ZONE * MODULE_PX)
        rows.extend([b"\x00" + bytes(line)] * MODULE_PX)
    rows.extend(border)

    header = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)  # 8-bit greyscale
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _chunk(b"IEND", b"")
    )
