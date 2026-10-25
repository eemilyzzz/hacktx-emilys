from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from clubcal.classify import RECAP_REASON, recap_reason
from clubcal.extract import AISettings, extract_ai, extract_rules
from clubcal.models import Candidate, Post
from clubcal.pipeline import process_post
from clubcal.refresh import partition_recaps

REPORTED_RECAP = """What an incredible evening with SWE Austin, Dell Technologies, and Gina Cano!

Our Unapologetically You event brought together an amazing community for an evening filled with inspiration, authentic conversations, meaningful connections, and plenty of laughter!

A huge thank you to Gina Cano for an incredibly heartfelt, energetic, and candid conversation. Gina generously shared her remarkable journey and challenged us to stop holding ourselves back.

Special thanks to Dell Technologies for hosting and collaborating with SWE Austin.
Our amazing volunteers and everyone who joined us!
#SWEAustin #UnapologeticallyYou #WomeninEngineering #AustinTech"""


def extract(caption, image_text=""):
    return extract_rules(Post("post", "SWE", caption, timestamp="2026-10-24"), image_text, "America/Chicago")


def test_reported_swe_recap_is_not_an_event_candidate():
    assert recap_reason(REPORTED_RECAP) == RECAP_REASON
    assert extract(REPORTED_RECAP) == []


@pytest.mark.parametrize("caption", [
    "Thank you to everyone for attending our workshop on October 8, 2026!",
    "Thanks for joining us at our event last night!",
    "Our workshop was held on October 8, 2026 at 6 PM. Thanks to our volunteers!",
    "FreshStart was held on Oct 8th at the campus. Our volunteers hosted interactive workshops.",
    "Event recap: October 8, 2026. Here are highlights from our event.",
    "Last Saturday we gathered for coffee and connection.",
])
def test_past_dates_and_event_keywords_do_not_turn_recaps_into_announcements(caption):
    assert extract(caption) == []


def test_a_past_event_flyer_does_not_override_recap_caption():
    assert extract(REPORTED_RECAP, "Join us October 8, 2026 from 5:30–7:30 p.m.") == []


def test_mixed_coffee_recap_keeps_next_meetup():
    items = extract("Another wonderful SWE coffee chat!\nLast Saturday we gathered for coffee. Save the date for our next coffee meet up on November 7th (location TBA)!")
    assert len(items) == 1
    assert items[0].start_date == "2026-11-07"
    assert items[0].all_day


def test_mixed_post_uses_new_event_date_and_time_instead_of_old_ones():
    items = extract("Our workshop was held on October 8, 2026 at 6 PM.\nJoin us for our next workshop on November 19, 2026 from 5:30–7:00 PM.\nLocation: Campus Union")
    assert len(items) == 1
    item = items[0]
    assert (item.start_date, item.start_time, item.end_time) == ("2026-11-19", "17:30", "19:00")
    assert "October 8" in item.evidence
    assert item.location == "Campus Union"


def test_thank_you_with_future_invitation_remains_an_event():
    items = extract("Thanks for attending our last session! RSVP for our next event November 19, 2026 at 6 PM.")
    assert items[0].start_date == "2026-11-19"


def test_regular_announcements_and_image_only_flyers_still_work():
    assert extract("Join us for a workshop November 19, 2026 at 6 PM.")
    assert extract("", "Workshop\nNovember 19, 2026\n6–8 PM")


def test_pipeline_skips_recaps_before_ocr_or_ai_calls(monkeypatch):
    download = Mock(side_effect=AssertionError("A recap should not download images"))
    model = Mock(side_effect=AssertionError("A recap should not spend AI credits"))
    monkeypatch.setattr("clubcal.pipeline.download_instagram_image", download)
    monkeypatch.setattr("clubcal.pipeline.extract_ai", model)
    items, warnings = process_post(Post("one", "SWE", REPORTED_RECAP, image_urls=["image"]), "UTC", AISettings("key", "https://api.meta.ai/v1", "test"))
    assert items == [] and warnings == [RECAP_REASON]
    download.assert_not_called()
    model.assert_not_called()


def test_ai_entrypoint_also_filters_obvious_recaps(monkeypatch):
    network = Mock(side_effect=AssertionError("No model request needed"))
    monkeypatch.setattr("clubcal.extract.requests.post", network)
    assert extract_ai(Post("one", "SWE", REPORTED_RECAP), "", "UTC", AISettings("key", "https://api.meta.ai/v1", "test")) == []
    network.assert_not_called()


def test_existing_draft_recaps_move_out_of_active_list_without_touching_approvals():
    recap = Candidate("recap", "Past event", "SWE", evidence=REPORTED_RECAP, description=REPORTED_RECAP)
    approved = replace(recap, id="approved", reviewed=True)
    manual = replace(recap, id="manual", method="manual")
    future = Candidate("future", "Next coffee chat", "SWE", description="We gathered last Saturday. Save the date for our next coffee meetup November 7th!")
    active, skipped = partition_recaps([recap, approved, manual, future])
    assert active == [approved, manual, future]
    assert skipped == [recap]


def test_live_session_migration_and_restore_as_manual_event():
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "main.py", default_timeout=30)
    app.session_state["parser_version"] = 2
    app.session_state["candidates"] = [
        Candidate("reported", "What an incredible evening with SWE!", "SWE", evidence=REPORTED_RECAP, description=REPORTED_RECAP),
        Candidate("future", "Next meetup", "SWE", "2026-11-07", description="Save the date for our next coffee meetup November 7th!"),
    ]
    app.run()
    assert not app.exception
    assert len(app.session_state["candidates"]) == 1
    assert len(app.session_state["skipped_recaps"]) == 1
    assert next(metric for metric in app.metric if metric.label == "Skipped recaps").value == "1"
    next(button for button in app.button if button.label == "Review as event").click().run()
    assert not app.exception
    assert len(app.session_state["candidates"]) == 2
    assert app.session_state["skipped_recaps"] == []
    restored = next(item for item in app.session_state["candidates"] if item.id == "reported")
    assert restored.method == "manual" and not restored.reviewed
