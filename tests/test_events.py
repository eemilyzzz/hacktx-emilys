from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest
from icalendar import Calendar

from clubcal.export import export_csv, export_ics
from clubcal.extract import AISettings, extract_ai, extract_rules
from clubcal.ingest import download_instagram_image, fetch_apify, normalize_posts, username_from_input
from clubcal.models import Candidate, Post, merge_candidates, validate
from clubcal.pipeline import process_post


def event(**kwargs):
    return replace(Candidate("stable-id", "Creative mixer", "club", "2026-10-29", "18:00", end_time="20:00", reviewed=True), **kwargs)


def components(items):
    return Calendar.from_ical(export_ics(items)).walk("VEVENT")


def test_calendar_requires_review_and_real_date_and_time():
    assert components([event(reviewed=False), event(start_date=None), event(start_time=None)]) == []


def test_calendar_roundtrips_text_without_property_injection():
    title = "Mixer, snacks; hello\nLOCATION:wrong"
    result = components([event(title=title, location="Union, Room 1; Upstairs")])[0]
    assert str(result["summary"]) == title
    assert str(result["location"]) == "Union, Room 1; Upstairs"
    assert str(result["uid"]) == "stable-id@clubcal.local"


@pytest.mark.parametrize("day,hour", [("2026-10-29", 23), ("2026-11-12", 0)])
def test_calendar_uses_correct_dst_offset(day, hour):
    result = components([event(start_date=day)])[0]
    assert result.decoded("dtstart").hour == hour  # 6 PM Central is 23:00 or 00:00 UTC.


def test_all_day_end_is_exclusive_and_multiday_inclusive_ui():
    result = components([event(all_day=True, start_time=None, end_time=None, end_date="2026-10-31")])[0]
    assert result.decoded("dtstart") == date(2026, 10, 29)
    assert result.decoded("dtend") == date(2026, 11, 1)


def test_missing_end_does_not_invent_a_duration():
    assert "DTEND" not in components([event(end_time=None)])[0]


def test_overnight_needs_explicit_end_day():
    assert validate(event(start_time="23:00", end_time="01:00"))
    assert not validate(event(start_time="23:00", end_time="01:00", end_date="2026-10-30"))


@pytest.mark.parametrize("day,clock", [("2026-03-08", "02:30"), ("2026-11-01", "01:30")])
def test_dst_gap_and_repeated_hour_require_manual_resolution(day, clock):
    assert validate(event(start_date=day, start_time=clock, end_time=None))


def test_duplicate_import_preserves_saved_edits_and_uid():
    original = event()
    result, added = merge_candidates([original], [replace(original, title="Unedited extraction", reviewed=False)])
    assert added == 0
    assert result == [original]
    result, added = merge_candidates(result, [replace(original, id="repost")])
    assert added == 0


def test_relative_date_uses_post_timestamp_not_scrape_date():
    post = Post("one", "club", "Workshop tomorrow at 6 PM", timestamp="2026-10-24T15:00:00Z")
    candidate = extract_rules(post, "", "America/Chicago")[0]
    assert candidate.start_date == "2026-10-25"
    assert candidate.start_time == "18:00"
    assert not candidate.reviewed


def test_caption_range_and_location():
    post = Post("one", "club", "Creative mixer\nOctober 29, 2026, 6–8 PM\nLocation: Union, 2.102", timestamp="2026-10-24")
    candidate = extract_rules(post, "", "America/Chicago")[0]
    assert (candidate.start_date, candidate.start_time, candidate.end_time) == ("2026-10-29", "18:00", "20:00")
    assert candidate.location == "Union, 2.102"


def test_conflicting_dates_and_missing_times_stay_unknown():
    candidate = extract_rules(Post("one", "club", "Meeting October 29, 2026\nFlyer says November 1, 2026"), "", "UTC")[0]
    assert candidate.start_date is None
    assert candidate.start_time is None
    assert candidate.all_day
    assert any("Several dates" in warning for warning in candidate.warnings)


def test_non_event_is_skipped():
    assert extract_rules(Post("one", "club", "Beautiful campus sunset!"), "", "UTC") == []


def test_carousel_normalization_preserves_photos_and_video_cover_images():
    posts = normalize_posts([{"id": "one", "ownerUsername": "club", "childPosts": [
        {"type": "Image", "displayUrl": "photo-one"}, {"type": "Video", "displayUrl": "video"},
        {"type": "Image", "displayUrl": "photo-two"},
    ]}])
    assert posts[0].image_urls == ["photo-one", "video", "photo-two"]


@pytest.mark.parametrize("value", ["https://evil.example/club", "https://www.instagram.com/p/postid/", "club?a=1"])
def test_account_input_rejects_non_profiles(value):
    with pytest.raises(ValueError):
        username_from_input(value)


def test_account_url_and_at_name():
    assert username_from_input("@club.name") == "club.name"
    assert username_from_input("https://www.instagram.com/club.name/?utm_source=x") == "club.name"


@pytest.mark.parametrize("url", ["http://127.0.0.1/a.png", "https://example.com/a.png", "https://cdninstagram.com.evil.test/a.png"])
def test_remote_images_are_limited_to_instagram_cdns(url):
    with pytest.raises(ValueError):
        download_instagram_image(url)


def test_failed_slide_is_visible_and_caption_still_parsed(monkeypatch):
    def fail(url):
        raise ValueError("expired")
    monkeypatch.setattr("clubcal.pipeline.download_instagram_image", fail)
    post = Post("one", "club", "Meeting October 29, 2026 at 6 PM", image_urls=["expired"])
    items, warnings = process_post(post, "UTC")
    assert len(items) == 1
    assert items[0].start_date == "2026-10-29"
    assert warnings and any("could not be read" in warning for warning in items[0].warnings)


def test_apify_uses_correct_actor_input_and_auth_header(monkeypatch):
    posted = Mock(ok=True)
    posted.json.return_value = {"data": {"id": "run-one", "status": "SUCCEEDED", "defaultDatasetId": "dataset-one"}}
    fetched = Mock(ok=True)
    fetched.json.return_value = [{"id": "post-one", "ownerUsername": "club"}]
    start = Mock(return_value=posted)
    monkeypatch.setattr("clubcal.ingest.requests.post", start)
    monkeypatch.setattr("clubcal.ingest.requests.get", Mock(return_value=fetched))
    assert len(fetch_apify(["@club"], "secret", 5)) == 1
    assert start.call_args.kwargs["json"]["username"] == ["club"]
    assert start.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert "secret" not in start.call_args.args[0]


def test_ai_splits_events_and_does_not_trust_reviewed_field(monkeypatch):
    response = Mock(ok=True)
    response.json.return_value = {"choices": [{"message": {"content": '{"events":[{"title":"Talk","start_date":"2026-10-29","start_time":"18:00","reviewed":true},{"title":"Workshop","start_date":"bad","start_time":null}]}'}}]}
    monkeypatch.setattr("clubcal.extract.requests.post", Mock(return_value=response))
    items = extract_ai(Post("one", "club", "Two events"), "flyer text", "UTC", AISettings("secret", "https://api.meta.ai/v1", "test"))
    assert len(items) == 2 and all(not item.reviewed for item in items)
    assert items[1].start_date is None
    assert items[0].id != items[1].id


def test_csv_includes_offset_and_neutralizes_formula_text():
    csv = export_csv([event(title="=HYPERLINK(bad)")])
    assert "'=HYPERLINK(bad)" in csv
    assert "2026-10-29T18:00:00-05:00" in csv
