from dataclasses import replace
from datetime import date, time
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from clubcal.extract import extract_rules
from clubcal.ingest import normalize_posts, recover_post_media
from clubcal.media import enrich_from_images
from clubcal.models import Candidate, Post, candidate_id
from clubcal.pipeline import MISSING_IMAGES, process_post


@pytest.mark.parametrize("media", [
    {"type": "Video", "displayUrl": "https://s.cdninstagram.com/cover.jpg"},
    {"media_type": "VIDEO", "thumbnail_url": "https://s.cdninstagram.com/cover.jpg"},
    {"display_url": "https://s.cdninstagram.com/cover.jpg"},
    {"image_versions2": {"candidates": [
        {"url": "small", "width": 100, "height": 100},
        {"url": "https://s.cdninstagram.com/cover.jpg", "width": 1080, "height": 1080},
    ]}},
])
def test_alternate_media_fields_and_reel_covers_reach_ocr(media):
    post = normalize_posts([{"id": "post", **media}])[0]
    assert post.image_urls == ["https://s.cdninstagram.com/cover.jpg"]


def test_nested_carousel_images_and_duplicate_sizes():
    row = {"id": "post", "edge_sidecar_to_children": {"edges": [
        {"node": {"display_url": "https://s.cdninstagram.com/one.jpg?size=large"}},
        {"node": {"media_type": "VIDEO", "thumbnail_url": "https://s.cdninstagram.com/two.jpg"}},
    ]}, "images": ["https://s.cdninstagram.com/one.jpg?size=small"]}
    assert normalize_posts([row])[0].image_urls == ["https://s.cdninstagram.com/one.jpg?size=large", "https://s.cdninstagram.com/two.jpg"]


def test_missing_images_are_reported_even_when_caption_contains_an_event():
    post = Post("post", "club", "Workshop November 19, 2026 at 6 PM", "https://www.instagram.com/p/one/")
    items, warnings = process_post(post, "UTC")
    assert MISSING_IMAGES in warnings and MISSING_IMAGES in items[0].warnings
    assert "[Image " not in items[0].evidence


def test_real_ocr_uses_imported_remote_image_bytes(monkeypatch):
    content = Path("examples/demo_flyer.png").read_bytes()
    download = Mock(return_value=content)
    monkeypatch.setattr("clubcal.pipeline.download_instagram_image", download)
    post = normalize_posts([{"id": "image-post", "ownerUsername": "club", "caption": "Join us for a walk!", "timestamp": "2026-10-24", "type": "Video", "displayUrl": "https://s.cdninstagram.com/flyer.jpg"}])[0]
    items, warnings = process_post(post, "America/Chicago")
    assert items[0].start_date == "2026-11-12"
    assert items[0].start_time == "19:00"
    assert "[Image 1]" in items[0].evidence
    assert "November 12, 2026" in items[0].evidence
    assert any(warning.startswith("Image OCR:") for warning in warnings)
    download.assert_called_once_with("https://s.cdninstagram.com/flyer.jpg")


def test_recovery_reads_existing_datasets_without_starting_an_actor(monkeypatch):
    runs = Mock(ok=True)
    runs.json.return_value = {"data": {"items": [{"status": "SUCCEEDED", "defaultDatasetId": "existing-dataset"}]}}
    dataset = Mock(ok=True)
    dataset.json.return_value = [{"id": "post", "type": "Video", "displayUrl": "https://s.cdninstagram.com/cover.jpg"}]
    monkeypatch.setattr("clubcal.ingest.requests.get", Mock(side_effect=[runs, dataset]))
    create = Mock(side_effect=AssertionError("Recovery must not start a paid Actor run"))
    monkeypatch.setattr("clubcal.ingest.requests.post", create)
    recovered = recover_post_media(Post("post", "club"), "secret")
    assert recovered.image_urls
    create.assert_not_called()


def test_this_thursday_uses_original_post_publication_date():
    post = Post("post", "utexascee", "Think you've got the moves? Get ready to dance with us this Thursday!", timestamp="2026-10-21T21:59:16.000Z")
    item = extract_rules(post, "", "America/Chicago")[0]
    assert item.start_date == "2026-10-22"


def test_image_enrichment_fills_unknown_fields_without_overwriting_corrections():
    current = Candidate("existing", "My edited title", "club", location="My edited venue")
    fresh = Candidate("fresh", "Extracted title", "club", "2026-11-12", "19:00", end_time="21:00", evidence="[Image 1]\nNovember 12, 2026")
    updated = enrich_from_images(current, fresh)
    assert updated.title == current.title and updated.location == current.location
    assert (updated.start_date, updated.start_time) == ("2026-11-12", "19:00")
    assert enrich_from_images(replace(current, reviewed=True), fresh).reviewed
    assert enrich_from_images(replace(current, reviewed=True), fresh).start_date is None


@pytest.mark.parametrize("media_state", ["missing", "available", "expired"])
def test_retry_updates_an_existing_event_and_is_saved(tmp_path, monkeypatch, media_state):
    post = Post("post", "club", "Join us for a workshop!", "https://www.instagram.com/p/example/", "2026-10-24")
    current = Candidate(candidate_id(post.id), "My title", "club", description=post.caption, source_url=post.url, source_timestamp=post.timestamp)
    recovered = replace(post, image_urls=["https://s.cdninstagram.com/flyer.jpg"])
    recovery = Mock(return_value=recovered)
    monkeypatch.setattr("clubcal.ingest.recover_post_media", recovery)
    content = Path("examples/demo_flyer.png").read_bytes()
    download = Mock(side_effect=[ValueError("Expired image"), content]) if media_state == "expired" else Mock(return_value=content)
    monkeypatch.setattr("clubcal.pipeline.download_instagram_image", download)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "main.py", default_timeout=60)
    app.session_state["candidates"] = [current]
    app.session_state["collected_posts"] = [post if media_state == "missing" else recovered]
    app.run()
    next(field for field in app.text_input if field.label == "Apify token").input("secret")
    next(button for button in app.button if button.label == "Retry image extraction").click().run()
    assert not app.exception
    saved = app.session_state["candidates"][0]
    assert saved.start_date == "2026-11-12" and saved.start_time == "19:00"
    assert saved.id == current.id and saved.title == "My title"
    assert "November 12, 2026" in saved.evidence
    assert app.date_input(key=f"sd-{saved.id}").value == date(2026, 11, 12)
    assert app.time_input(key=f"st-{saved.id}").value == time(19, 0)
    assert app.session_state["library_store"].load_library()[0][0] == saved
    assert recovery.call_count == int(media_state != "available")


def test_missing_image_results_are_reprocessed_instead_of_cached():
    post = Post("post", "club", "Workshop November 19, 2026 at 6 PM", "https://www.instagram.com/p/example/")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "main.py", default_timeout=30)
    app.session_state["collected_posts"] = [post]
    app.run()
    for _ in range(2):
        next(button for button in app.button if button.label == "Extract events from collected posts").click().run()
        assert not app.exception
        assert app.session_state["import_history"][0]["cached"] == 0
    rows = app.session_state["library_store"].connection.execute("SELECT CACHE_VALID FROM CC_POSTS WHERE PROCESSING_KEY != 'collected'").fetchall()
    assert rows and all(not row[0] for row in rows)
