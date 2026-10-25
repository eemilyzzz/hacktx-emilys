from __future__ import annotations

from collections.abc import Callable

from .classify import recap_reason
from .extract import AISettings, extract_ai, extract_rules
from .ingest import download_instagram_image
from .models import Candidate, Post
from .ocr import read_image


def process_post(
    post: Post, timezone: str, ai: AISettings | None = None,
    local_images: list[bytes] | None = None, use_ocr: bool = True,
    max_images: int = 10, progress: Callable[[str], None] | None = None,
) -> tuple[list[Candidate], list[str]]:
    reason = recap_reason(post.caption)
    if reason:
        return [], [reason]
    texts, warnings = [], []
    if not use_ocr and (post.image_urls or local_images):
        warnings.append("OCR was disabled; this post was interpreted from its caption only.")
    if use_ocr:
        images = [("upload", content) for content in (local_images or [])]
        images += [(url, None) for url in post.image_urls]
        if len(images) > max_images:
            warnings.append(f"Only {max_images} carousel slides processed. Upload remaining flyers separately.")
        for index, (url, content) in enumerate(images[:max_images]):
            if progress:
                progress(f"Reading @{post.username} · image {index + 1}/{min(len(images), max_images)}")
            try:
                content = content if content is not None else download_instagram_image(url)
                text, image_warnings = read_image(content)
                texts.append(f"[Image {index + 1}]\n{text}")
                warnings.extend(image_warnings)
            except Exception as exc:
                # One expired/bad image must not discard other slides or the caption.
                warnings.append(f"Image {index + 1} could not be read ({type(exc).__name__}). Fetch fresh posts or upload it manually.")
    text = "\n\n".join(texts)
    if progress:
        progress(f"Finding events in @{post.username}'s post")
    candidates = extract_ai(post, text, timezone, ai) if ai else extract_rules(post, text, timezone)
    for candidate in candidates:
        candidate.warnings = list(dict.fromkeys(candidate.warnings + warnings))
    return candidates, warnings
