from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from threading import Lock

from PIL import Image, ImageOps

_lock = Lock()


@lru_cache(maxsize=1)
def _engine():
    from rapidocr import RapidOCR
    return RapidOCR()


def read_image(content: bytes) -> tuple[str, list[str]]:
    """Run actual CPU OCR. No flyer or image is sent to an AI service."""
    import numpy as np

    if len(content) > 20 * 1024 * 1024:
        raise ValueError("Image exceeds the 20 MB limit.")
    with Image.open(BytesIO(content)) as source:
        if source.width * source.height > 30_000_000:
            raise ValueError("Image exceeds 30 megapixels. Resize it before uploading.")
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((2400, 2400))
        # RapidOCR consumes OpenCV's BGR channel order.
        pixels = np.asarray(image)[:, :, ::-1].copy()
    with _lock:
        result = _engine()(pixels)
    texts = result.txts or ()
    scores = result.scores if result.scores is not None else ()
    warnings = []
    if not texts:
        warnings.append("No text detected in this image. Check the caption or enter the event manually.")
    elif any(float(score) < 0.75 for score in scores):
        warnings.append("Some flyer text was difficult to read. Compare dates and times with the original.")
    return "\n".join(texts), warnings
