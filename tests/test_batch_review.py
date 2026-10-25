from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from clubcal.models import Candidate
from clubcal.review import approve_selected
from clubcal.storage import StorageError

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def events():
    base = Candidate("one", "First workshop", "SWE", "2026-11-19", "17:30", end_time="19:00")
    return [base, replace(base, id="two", title="Second workshop"),
            replace(base, id="missing", title="Missing date", start_date=None),
            replace(base, id="approved", title="Previously approved", reviewed=True)]


def app_with_events():
    app = AppTest.from_file(MAIN, default_timeout=30)
    app.session_state["candidates"] = events()
    return app.run()


def test_batch_only_approves_selected_valid_events_and_preserves_existing_reviews():
    original = events()
    updated, approved, already, problems = approve_selected(original, {"one", "missing", "approved", "stale-id"})
    assert approved == ["one"] and already == ["approved"]
    assert list(problems) == ["missing"]
    assert "Choose an event date." in problems["missing"]["errors"]
    assert [item.reviewed for item in updated] == [True, False, False, True]
    assert not original[0].reviewed
    assert updated[3] == original[3]


def test_batch_uses_all_day_default_and_rejects_invalid_times():
    no_time = replace(events()[0], start_time=None, end_time=None)
    overnight = replace(events()[1], start_time="23:00", end_time="01:00")
    updated, approved, _, problems = approve_selected([no_time, overnight], {"one", "two"})
    assert approved == ["one"]
    assert updated[0].all_day and updated[0].reviewed
    assert "two" in problems and not updated[1].reviewed


def test_individual_selection_approves_and_persists_without_touching_other_events():
    app = app_with_events()
    assert not app.exception
    assert next(button for button in app.button if button.label == "Approve selected (0)").disabled
    app.checkbox(key="select-event-two").check().run()
    next(button for button in app.button if button.label == "Approve selected (1)").click().run()
    assert not app.exception
    assert [item.reviewed for item in app.session_state["candidates"]] == [False, True, False, True]
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    assert {item.id: item.reviewed for item in reopened.session_state["candidates"]} == {
        "one": False, "two": True, "missing": False, "approved": True,
    }


def test_select_all_skips_invalid_events_and_keeps_them_selected_for_correction():
    app = app_with_events()
    next(button for button in app.button if button.label == "Select all").click().run()
    assert not app.exception
    assert all(app.checkbox(key=f"select-event-{item.id}").value for item in events())
    next(button for button in app.button if button.label == "Approve selected (4)").click().run()
    assert not app.exception
    assert [item.reviewed for item in app.session_state["candidates"]] == [True, True, False, True]
    assert app.checkbox(key="select-event-missing").value
    assert not app.checkbox(key="select-event-one").value
    assert any("Missing date" in error.value for error in app.error)


def test_clear_selection_does_not_approve_events():
    app = app_with_events()
    next(button for button in app.button if button.label == "Select all").click().run()
    next(button for button in app.button if button.label == "Clear selection").click().run()
    assert not app.exception
    assert not any(app.checkbox(key=f"select-event-{item.id}").value for item in events())
    assert next(button for button in app.button if button.label == "Delete selected (0)").disabled
    assert [item.reviewed for item in app.session_state["candidates"]] == [False, False, False, True]


def test_delete_selected_removes_only_checked_events_and_persists():
    app = app_with_events()
    assert next(button for button in app.button if button.label == "Delete selected (0)").disabled
    app.checkbox(key="select-event-two").check().run()
    app.checkbox(key="select-event-approved").check().run()
    next(button for button in app.button if button.label == "Delete selected (2)").click().run()
    assert not app.exception
    assert {item.id for item in app.session_state["candidates"]} == {"one", "missing"}
    assert app.session_state["removed_event_ids"] == {"two", "approved"}
    assert any("Deleted 2" in message.value for message in app.success)
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    assert not reopened.exception
    assert {item.id for item in reopened.session_state["candidates"]} == {"one", "missing"}
    assert all(button.disabled for button in reopened.get("download_button")[:2])


def test_select_all_then_delete_clears_active_list_and_keeps_skipped_posts():
    app = AppTest.from_file(MAIN, default_timeout=30)
    app.session_state["candidates"] = events()
    app.session_state["skipped_recaps"] = [replace(events()[0], id="skipped", title="Past recap")]
    app.run()
    next(button for button in app.button if button.label == "Select all").click().run()
    next(button for button in app.button if button.label == "Delete selected (4)").click().run()
    assert not app.exception
    assert app.session_state["candidates"] == []
    assert len(app.session_state["skipped_recaps"]) == 1
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    assert reopened.session_state["candidates"] == []
    assert len(reopened.session_state["skipped_recaps"]) == 1


def test_failed_batch_delete_is_reported_and_can_be_retried(monkeypatch):
    app = app_with_events()
    store = app.session_state["library_store"]
    original = store.save_changes
    monkeypatch.setattr(store, "save_changes", Mock(side_effect=StorageError("Connection unavailable. Retry saving.")))
    app.checkbox(key="select-event-two").check().run()
    next(button for button in app.button if button.label == "Delete selected (1)").click().run()
    assert not app.exception
    assert app.session_state["storage_error"]
    assert "two" not in {item.id for item in app.session_state["candidates"]}
    assert "two" in {item.id for item in store.load_library()[0]}
    monkeypatch.setattr(store, "save_changes", original)
    next(button for button in app.button if button.label == "Retry saving").click().run()
    assert not app.exception
    assert "two" not in {item.id for item in store.load_library()[0]}
