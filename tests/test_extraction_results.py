from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from clubcal.models import Candidate, Post, candidate_id
from clubcal.storage import LibraryStore, StorageError, open_local, snapshot

MAIN = Path(__file__).resolve().parents[1] / "main.py"
POST = Post("post", "SWE", "Workshop November 19, 2026 at 6 PM", timestamp="2026-10-25")
EXTRACTED = Candidate(candidate_id(POST.id), "Workshop", "SWE", "2026-11-19", "18:00", evidence=POST.caption)


def prepare_app(monkeypatch, *, deleted=True, empty=False):
    initial = AppTest.from_file(MAIN, default_timeout=30).run()
    store = initial.session_state["library_store"]
    edited = replace(EXTRACTED, title="My corrected title", location="My room", reviewed=True)
    unrelated = replace(EXTRACTED, id="unrelated", title="Another deleted event")
    baseline = snapshot([edited, unrelated], [])
    store.save_changes(baseline, {})
    store.save_changes(snapshot([] if deleted else [edited], []), baseline)
    process = Mock(return_value=([] if empty else [EXTRACTED], []))
    fetch = Mock(side_effect=AssertionError("Restoration must not start an Apify run"))
    monkeypatch.setattr("clubcal.pipeline.process_post", process)
    monkeypatch.setattr("clubcal.ingest.fetch_apify", fetch)
    app = AppTest.from_file(MAIN, default_timeout=30)
    app.session_state["collected_posts"] = [POST]
    app.run()
    return app, fetch


def extract(app):
    next(button for button in app.button if button.label == "Extract events from collected posts").click().run()
    assert not app.exception


def test_reimport_reports_deleted_events_and_explicit_restore_is_durable(monkeypatch):
    app, fetch = prepare_app(monkeypatch)
    extract(app)
    assert app.session_state["candidates"] == []
    assert any("1 detected event was previously deleted" in message.value for message in app.warning)
    history = app.session_state["import_history"][0]
    assert (history["detected"], history["events"], history["deleted"]) == (1, 0, 1)
    app.run()  # The restore action remains available after an unrelated rerun.
    app.button(key="restore-imported-events").click().run()
    assert not app.exception
    restored = app.session_state["candidates"][0]
    assert restored.title == "My corrected title" and restored.location == "My room"
    assert not restored.reviewed
    assert app.session_state["removed_event_ids"] == {"unrelated"}
    assert any("Restored 1 event as a draft" in message.value for message in app.success)
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    loaded = reopened.session_state["candidates"][0]
    assert replace(loaded, warnings=restored.warnings) == restored
    reopened.session_state["collected_posts"] = [POST]
    reopened.run()
    extract(reopened)
    assert reopened.session_state["import_history"][0]["existing"] == 1
    assert len(reopened.session_state["candidates"]) == 1
    fetch.assert_not_called()


def test_existing_events_are_counted_as_detected_without_losing_edits(monkeypatch):
    app, _ = prepare_app(monkeypatch, deleted=False)
    extract(app)
    assert any("Detected 1 event candidates · 0 new · 1 already saved" in message.value for message in app.info)
    assert not any(button.key == "restore-imported-events" for button in app.button)
    assert app.session_state["candidates"][0].title == "My corrected title"
    assert app.session_state["candidates"][0].reviewed
    assert app.session_state["import_history"][0]["existing"] == 1


def test_genuinely_empty_result_is_explained(monkeypatch):
    app, _ = prepare_app(monkeypatch, empty=True)
    extract(app)
    assert app.session_state["extraction_summary"]["non_events"] == 1
    assert any("No event announcements were detected" in message.value for message in app.warning)
    assert not any(button.key == "restore-imported-events" for button in app.button)


def test_failed_restore_keeps_deletions_and_can_be_retried(monkeypatch):
    app, _ = prepare_app(monkeypatch)
    extract(app)
    store = app.session_state["library_store"]
    original = store._upsert
    monkeypatch.setattr(store, "_upsert", Mock(side_effect=RuntimeError("Connection lost")))
    app.button(key="restore-imported-events").click().run()
    assert not app.exception
    assert app.session_state["candidates"] == []
    assert candidate_id(POST.id) in store.deleted_ids()
    assert any("Could not restore" in error.value for error in app.error)
    monkeypatch.setattr(store, "_upsert", original)
    app.button(key="restore-imported-events").click().run()
    assert not app.exception
    assert len(app.session_state["candidates"]) == 1


def test_restore_is_scoped_to_selected_workspace_and_rolls_back_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite3"
    alice, bob = open_local(path, "alice"), open_local(path, "bob")
    other = replace(EXTRACTED, id="two")
    baseline = snapshot([EXTRACTED, other], [])
    for store in (alice, bob):
        store.save_changes(baseline, {})
        store.save_changes({}, baseline)
    original = alice._upsert
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Connection lost")
        return original(*args, **kwargs)
    monkeypatch.setattr(alice, "_upsert", fail_second)
    with pytest.raises(StorageError):
        alice.restore_deleted([EXTRACTED.id, "two"])
    assert alice.load_library()[0] == []
    monkeypatch.setattr(alice, "_upsert", original)
    restored = alice.restore_deleted([EXTRACTED.id, "does-not-exist"])
    assert [item.id for item in restored] == [EXTRACTED.id]
    assert alice.deleted_ids() == {"two"}
    assert bob.deleted_ids() == {EXTRACTED.id, "two"}
    assert alice.restore_deleted([EXTRACTED.id]) == []


def test_snowflake_restore_binds_workspace_and_event_ids():
    connection = Mock()
    connection.cursor.return_value.execute.return_value.fetchall.return_value = []
    store = LibraryStore(connection, "private-workspace", "snowflake", '"CLUBCAL"."APP".')
    identity = "untrusted'event"
    assert store.restore_deleted([identity]) == []
    sql, values = next(call.args for call in connection.cursor.return_value.execute.call_args_list
                       if call.args[0].startswith("SELECT"))
    assert "CC_REVIEWS" in sql and "%s" in sql
    assert identity not in sql and values == ("private-workspace", "deleted", identity)
