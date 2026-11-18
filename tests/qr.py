"""Reading QR images back in tests.

OpenCV's default QRCodeDetector misses some valid codes (about 5% of random promo links in
our tests: same picture, a different token), which made QR tests flaky. The ArUco-based
detector read all of 2000 random links; the default detector and other scales are only a
fallback, so one bad guess never decides a test.
"""

import cv2
import numpy as np


def decode_qr_png(png: bytes) -> str:
    """The text of the QR in a PNG, or "" if no detector could read it."""
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    for scale in (1.0, 0.5, 2.0):
        if scale == 1.0:
            scaled = image
        else:
            interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST
            scaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=interpolation)
        for detector in (cv2.QRCodeDetectorAruco(), cv2.QRCodeDetector()):
            text = detector.detectAndDecode(scaled)[0]
            if text:
                return text
    return ""
