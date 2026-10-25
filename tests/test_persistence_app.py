from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from clubcal.models import Candidate
from clubcal.storage import StorageError, open_local

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def app_with_event():
    app = AppTest.from_file(MAIN, default_timeout=30)
    app.session_state["candidates"] = [Candidate("saved", "Saved workshop", "SWE", "2026-11-19", "17:30", end_time="19:00")]
    return app.run()


def test_review_survives_fresh_browser_session():
    app = app_with_event()
    next(field for field in app.text_input if field.label == "Event title").input("Corrected workshop")
    next(button for button in app.button if button.label == "Save & approve").click().run()
    assert not app.exception
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    assert not reopened.exception
    item = reopened.session_state["candidates"][0]
    assert item.title == "Corrected workshop" and item.reviewed


def test_processing_cache_and_history_survive_reload():
    app = AppTest.from_file(MAIN, default_timeout=30).run()
    next(widget for widget in app.checkbox if widget.label == "Read text from images").uncheck().run()
    next(button for button in app.button if button.label == "Try fictional demo posts").click().run()
    assert not app.exception
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    assert len(reopened.session_state["candidates"]) == 2
    assert reopened.session_state["import_history"][0]["source"] == "Demo"
    next(widget for widget in reopened.checkbox if widget.label == "Read text from images").uncheck().run()
    next(button for button in reopened.button if button.label == "Try fictional demo posts").click().run()
    assert not reopened.exception
    assert reopened.session_state["import_history"][0]["cached"] == 3
    assert len(reopened.session_state["candidates"]) == 2


def test_collected_posts_and_failed_history_survive_reload_without_saving_token(monkeypatch):
    from clubcal.models import Post
    post = Post("collected", "swetexas", "Workshop November 19, 2026 at 6 PM")
    fetch = Mock(return_value=[post])
    monkeypatch.setattr("clubcal.ingest.fetch_apify", fetch)
    app = AppTest.from_file(MAIN, default_timeout=30).run()
    next(field for field in app.text_area if field.label == "Instagram usernames or profile URLs, one per line").input("@swetexas")
    next(field for field in app.text_input if field.label == "Apify token").input("do-not-store-this-key")
    next(button for button in app.button if button.label == "Fetch public posts").click().run()
    assert not app.exception
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    assert not reopened.exception
    assert reopened.session_state["collected_posts"] == [post]
    assert "do-not-store-this-key" not in str(reopened.session_state["import_history"])
    assert all("do-not-store-this-key" not in row[0] for row in reopened.session_state["library_store"].connection.execute("SELECT PAYLOAD FROM CC_POSTS"))
    fetch.side_effect = ValueError("Network unavailable")
    next(field for field in reopened.text_area if field.label == "Instagram usernames or profile URLs, one per line").input("@swetexas")
    next(button for button in reopened.button if button.label == "Fetch public posts").click().run()
    again = AppTest.from_file(MAIN, default_timeout=30).run()
    assert again.session_state["import_history"][0]["status"] == "failed"
    assert again.session_state["import_history"][0]["error"] == "Network unavailable"


def test_save_failure_keeps_edits_and_retry_persists_them(monkeypatch):
    app = app_with_event()
    store = app.session_state["library_store"]
    original = store.save_changes
    monkeypatch.setattr(store, "save_changes", Mock(side_effect=StorageError("Connection unavailable. Retry saving.")))
    next(button for button in app.button if button.label == "Save & approve").click().run()
    assert not app.exception
    assert app.session_state["candidates"][0].reviewed
    assert app.session_state["storage_error"]
    monkeypatch.setattr(store, "save_changes", original)
    next(button for button in app.button if button.label == "Retry saving").click().run()
    assert not app.exception
    assert app.session_state["storage_error"] == ""
    assert AppTest.from_file(MAIN, default_timeout=30).run().session_state["candidates"][0].reviewed


def test_cloud_switch_preserves_local_library_and_copies_without_overwriting(tmp_path, monkeypatch):
    app = app_with_event()
    remote = open_local(tmp_path / "remote.sqlite3")
    remote.kind = "Snowflake"  # Exercise the full UI against a real separate durable store.
    monkeypatch.setattr("clubcal.persistence.open_snowflake", Mock(return_value=remote))
    next(field for field in app.text_input if field.label == "Snowflake account identifier").input("ORG-ACCOUNT")
    next(field for field in app.text_input if field.label == "Snowflake username").input("emily")
    next(button for button in app.button if button.label == "Connect & load Snowflake library").click().run()
    assert not app.exception
    assert app.session_state["library_store"].kind == "Snowflake"
    assert app.session_state["candidates"] == []
    assert len(app.session_state["local_store"].load_library()[0]) == 1
    next(button for button in app.button if button.label == "Copy local library into Snowflake").click().run()
    assert not app.exception
    assert len(remote.load_library()[0]) == 1
    next(field for field in app.text_input if field.label == "Event title").input("Cloud correction")
    next(button for button in app.button if button.label == "Save & approve").click().run()
    next(button for button in app.button if button.label == "Copy local library into Snowflake").click().run()
    assert app.session_state["candidates"][0].title == "Cloud correction"
    next(button for button in app.button if button.label == "Use local library").click().run()
    assert not app.exception
    assert app.session_state["candidates"][0].title == "Saved workshop"
