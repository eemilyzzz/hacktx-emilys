"""Regression cases based on the captions and imports reported in the browser."""
from dataclasses import asdict, make_dataclass, replace
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
from icalendar import Calendar
from streamlit.testing.v1 import AppTest

from clubcal.export import export_ics
from clubcal.extract import AISettings, extract_ai, extract_rules
from clubcal.ingest import normalize_posts
from clubcal.models import Candidate, Post
from clubcal.refresh import name_unnamed_imports, refresh_drafts

SPEAKER = """SPEAKER SPOTLIGHT: Meet Gina Cano
On October 8, SWE Austin is excited to welcome Gina Cano.
📅 October 8 | 5:30–7:30 p.m.
📍 Dell Technologies
🎤 Gina's talk begins at 6:00 p.m."""

CISCO = """The SWE Austin Section is excited to partner with Cisco.
🗓 Thursday, November 19, 2026
🕑 5:30-7:00 PM
📍 Cisco Campus | 12515 Research Blvd.
Register by November 18 at the link in bio"""

COFFEE = """Another wonderful SWE Austin coffee chat!
Last Saturday we gathered for coffee. Save the date for our next coffee meet up on November 7th (location TBA)!"""


def extract(caption):
    return extract_rules(Post("post", "SWE", caption, timestamp="2026-10-24T15:00:00Z"), "", "America/Chicago")[0]


def test_speaker_time_does_not_override_event_window():
    item = extract(SPEAKER)
    assert (item.start_date, item.start_time, item.end_time) == ("2026-10-08", "17:30", "19:30")
    assert not item.all_day
    assert not any("Conflicting" in warning for warning in item.warnings)


def test_event_date_wins_over_registration_deadline():
    item = extract(CISCO)
    assert (item.start_date, item.start_time, item.end_time) == ("2026-11-19", "17:30", "19:00")
    assert not any("Several dates" in warning for warning in item.warnings)


@pytest.mark.parametrize("deadline", ["Register by November 18", "RSVP before November 18", "Registration deadline: November 18", "Sign up by November 18", "Applications close November 18"])
def test_common_deadline_wording_is_not_an_event_date(deadline):
    assert extract(f"Workshop\nNovember 19, 2026 at 6 PM\n{deadline}").start_date == "2026-11-19"


def test_registration_invitation_without_cutoff_keeps_event_date():
    assert extract("Register for our workshop November 19, 2026 at 6 PM").start_date == "2026-11-19"


def test_no_start_time_defaults_to_all_day_and_exports_as_date():
    item = extract(COFFEE)
    assert item.start_date == "2026-11-07"
    assert item.start_time is None and item.all_day
    item.reviewed = True
    result = Calendar.from_ical(export_ics([item])).walk("VEVENT")[0]
    assert result.decoded("dtstart") == date(2026, 11, 7)
    assert result.decoded("dtend") == date(2026, 11, 8)


@pytest.mark.parametrize("clock,start,end", [
    ("5:30–7:30 p.m.", "17:30", "19:30"),
    ("5:30 – 7:30 p. m.", "17:30", "19:30"),
    ("5.30 to 7.30 PM", "17:30", "19:30"),
    ("17:30–19:30", "17:30", "19:30"),
    ("at 17:30", "17:30", None),
    ("at 6 p.m.", "18:00", None),
    ("11–1 PM", "11:00", "13:00"),
])
def test_caption_time_formats(clock, start, end):
    item = extract(f"Workshop\nNovember 19, 2026 {clock}")
    assert (item.start_time, item.end_time) == (start, end)


@pytest.mark.parametrize("written", ["November 19th, 2026", "19th November 2026", "Nov.19, 2026", "11/19/2026", "2026-11-19", "11-19-2026", "11.19.2026"])
def test_common_caption_dates(written):
    assert extract(f"Workshop {written} at 6 PM").start_date == "2026-11-19"


@pytest.mark.parametrize("metadata", [
    {"owner": {"full_name": "SWE", "username": "sweaustin"}},
    {"user": {"name": "SWE"}}, {"account": "SWE"},
    {"account_name": "SWE"}, {"organization": "SWE"},
    {"profile": {"name": "SWE"}}, {"ownerFullName": "SWE"},
])
def test_json_account_names_are_normalized(metadata):
    item = normalize_posts([{"id": "post", "caption": SPEAKER, **metadata}])[0]
    assert item.username == "SWE"


def test_profile_exports_inherit_account_and_alternate_publication_timestamp():
    items = normalize_posts([{"fullName": "SWE", "username": "sweaustin", "latestPosts": [
        {"id": "one", "caption": SPEAKER, "published_at": "2026-10-24T15:00:00Z"},
        {"id": "two", "caption": COFFEE},
    ]}])
    assert all(item.username == "SWE" for item in items)
    assert items[0].timestamp == "2026-10-24T15:00:00Z"
    assert normalize_posts({"account": {"name": "SWE"}, "posts": [{"id": "one"}]} )[0].username == "SWE"


def test_json_without_metadata_accepts_explicit_organization():
    assert normalize_posts([{"id": "one"}], "SWE")[0].username == "SWE"
    assert normalize_posts([{"id": "one", "ownerUsername": "anotherclub"}], "SWE")[0].username == "anotherclub"


def test_ai_without_start_time_also_defaults_to_all_day(monkeypatch):
    response = Mock(ok=True)
    response.json.return_value = {"choices": [{"message": {"content": '{"events":[{"title":"Coffee chat","start_date":"2026-11-07","start_time":null,"all_day":false}]}'}}]}
    monkeypatch.setattr("clubcal.extract.requests.post", Mock(return_value=response))
    item = extract_ai(Post("one", "SWE", COFFEE), "", "UTC", AISettings("secret", "https://api.meta.ai/v1", "test"))[0]
    assert item.all_day


def test_existing_drafts_are_repaired_without_losing_edits_or_approval():
    old = Candidate("existing-id", "My corrected title", "Imported club", "2026-10-08", evidence=SPEAKER)
    approved = replace(old, id="approved", start_time="17:00", reviewed=True)
    coffee = Candidate("coffee", "Coffee", "Imported club", "2026-11-07", evidence=COFFEE)
    updated, changed = refresh_drafts([old, approved, coffee])
    assert updated[0].id == old.id and updated[0].title == "My corrected title"
    assert updated[0].start_time == "17:30"
    assert updated[1] == approved
    assert updated[2].all_day
    assert changed == {"existing-id", "coffee"}
    renamed = name_unnamed_imports(updated, "SWE")
    assert all(item.organization == "SWE" for item in renamed)
    assert renamed[1].reviewed


def test_hot_reload_migrates_old_dataclass_with_no_new_fields():
    values = asdict(Candidate("old", "Speaker", "Imported club", evidence=SPEAKER))
    values.pop("source_timestamp")
    LegacyCandidate = make_dataclass("Candidate", [(name, object) for name in values])
    legacy = LegacyCandidate(**values)
    updated, _ = refresh_drafts([legacy])
    assert updated[0].source_timestamp == ""
    assert updated[0].start_time == "17:30"


def test_app_upgrades_existing_session_and_approves_event_without_time():
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "main.py", default_timeout=30)
    app.session_state["candidates"] = [Candidate("old-coffee", "Coffee chat", "Imported club", "2026-11-07", evidence=COFFEE)]
    app.run()
    assert not app.exception
    assert app.session_state["candidates"][0].all_day
    next(widget for widget in app.text_input if widget.label == "Organization name for unnamed imports").input("SWE")
    next(widget for widget in app.button if widget.label == "Apply organization name").click().run()
    assert not app.exception
    next(widget for widget in app.button if widget.label == "Save & approve").click().run()
    assert not app.exception
    item = app.session_state["candidates"][0]
    assert item.reviewed and item.all_day and item.organization == "SWE"
