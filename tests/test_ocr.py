from pathlib import Path

from clubcal.models import Post
from clubcal.ocr import read_image
from clubcal.pipeline import process_post


def test_real_ocr_reads_image_only_flyer_and_extracts_event():
    content = Path("examples/demo_flyer.png").read_bytes()
    text, warnings = read_image(content)
    assert "November 12, 2026" in text
    assert "7:00 PM" in text
    events, _ = process_post(Post("image-test", "photo_club", timestamp="2026-10-24"), "America/Chicago", local_images=[content])
    assert len(events) == 1
    assert events[0].start_date == "2026-11-12"
    assert events[0].start_time == "19:00"
    assert events[0].end_time == "21:00"
