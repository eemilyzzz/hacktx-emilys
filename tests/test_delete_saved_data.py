from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from clubcal.models import Candidate, Post
from clubcal.storage import LibraryStore, StorageError, content_hash, open_local, snapshot

MAIN = Path(__file__).resolve().parents[1] / "main.py"
TABLES = ("EVENTS", "REVIEWS", "POSTS", "IMPORTS")
CONFIRM = "I understand this permanently deletes this workspace's saved data"


def seed(store):
    active = Candidate("active", "Saved workshop", "SWE", "2026-11-19", "18:00", reviewed=True)
    removed = replace(active, id="removed", title="Deleted workshop")
    recap = replace(active, id="recap", title="Past recap", reviewed=False)
    before = snapshot([active, removed], [recap])
    post = Post("post", "SWE", "Workshop November 19, 2026 at 6 PM")
    cached = {"post": post, "key": "rules", "hash": content_hash(post), "items": [active], "warnings": [], "valid": True}
    history = [{"id": "import", "source": "Apify extraction", "accounts": ["SWE"], "started_at": "2026-10-25T08:00:00Z",
                "status": "succeeded", "posts": 1, "events": 1, "cached": 0, "skipped": 0, "error": ""}]
    store.save_changes(before, {}, history, [cached, {**cached, "key": "collected", "valid": False}])
    store.save_changes(snapshot([active], [recap]), before)
    return post, cached


def counts(store):
    return {name: store.connection.execute(f"SELECT COUNT(*) FROM CC_{name} WHERE WORKSPACE = ?", (store.workspace,)).fetchone()[0] for name in TABLES}


def populated_app():
    initial = AppTest.from_file(MAIN, default_timeout=30).run()
    post, cached = seed(initial.session_state["library_store"])
    app = AppTest.from_file(MAIN, default_timeout=30).run()
    assert not app.exception
    return app, post, cached


def confirm(app):
    next(widget for widget in app.checkbox if widget.label == CONFIRM).check().run()
    assert not app.exception


def test_workspace_delete_is_durable_and_other_workspaces_are_untouched(tmp_path):
    path = tmp_path / "library.sqlite3"
    alice, bob = open_local(path, "alice"), open_local(path, "bob")
    seed(alice)
    seed(bob)
    before = counts(bob)
    alice.clear_workspace()
    assert counts(alice) == dict.fromkeys(TABLES, 0)
    assert counts(bob) == before
    reopened = open_local(path, "alice")
    assert reopened.load_library() == ([], [])
    assert reopened.collected_posts() == [] and reopened.load_history() == []
    assert reopened.deleted_ids() == set()
    # Old deletion records and reviews cannot hide a newly imported event.
    item = Candidate("removed", "New import", "SWE", "2026-11-19")
    reopened.save_changes(snapshot([item], []), {})
    assert reopened.load_library()[0] == [item]


def test_delete_rolls_back_all_tables_when_one_delete_fails(tmp_path):
    store = open_local(tmp_path / "library.sqlite3")
    seed(store)
    before = counts(store)
    store.connection.execute("CREATE TRIGGER prevent_post_delete BEFORE DELETE ON CC_POSTS BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    store.connection.commit()
    with pytest.raises(StorageError):
        store.clear_workspace()
    assert counts(store) == before
    assert len(store.load_library()[0]) == 1


def test_snowflake_delete_is_scoped_and_parameter_bound():
    connection = Mock()
    store = LibraryStore(connection, "private'workspace", "snowflake", '"CLUBCAL"."APP".')
    store.clear_workspace()
    statements = [call.args for call in connection.cursor.return_value.execute.call_args_list if call.args[0].startswith("DELETE")]
    assert len(statements) == 4
    for sql, parameters in statements:
        assert '"CLUBCAL"."APP".CC_' in sql and "WHERE WORKSPACE = %s" in sql
        assert store.workspace not in sql and parameters == (store.workspace,)
    connection.commit.assert_called_once()


def test_confirmation_delete_clears_session_pending_writes_and_reload():
    app, post, cached = populated_app()
    store = app.session_state["library_store"]
    before = counts(store)
    assert app.button(key="delete-all-saved-data").disabled
    app.run()
    assert counts(store) == before
    confirm(app)
    app.session_state["post_queue"] = {("post", "pending"): {**cached, "key": "pending"}}
    app.session_state["post_memo"] = {("post", "rules", content_hash(post)): ([cached["items"][0]], [])}
    app.session_state["batch_delete_notice"] = 1
    app.button(key="delete-all-saved-data").click().run()
    assert not app.exception
    for name in ("candidates", "skipped_recaps", "collected_posts", "import_history"):
        assert app.session_state[name] == []
    for name in ("post_queue", "post_memo", "library_baseline", "history_baseline"):
        assert app.session_state[name] == {}
    assert app.session_state["removed_event_ids"] == set()
    assert counts(store) == dict.fromkeys(TABLES, 0)
    assert any("Deleted all saved data" in message.value for message in app.success)
    assert app.button(key="delete-all-saved-data").disabled
    app.run()  # End-of-script saving must not repopulate anything.
    reopened = AppTest.from_file(MAIN, default_timeout=30).run()
    assert reopened.session_state["candidates"] == []
    assert reopened.session_state["collected_posts"] == []
    assert reopened.session_state["import_history"] == []


def test_delete_failure_keeps_library_and_retry_succeeds():
    app, _, _ = populated_app()
    store = app.session_state["library_store"]
    before = counts(store)
    store.connection.execute("CREATE TRIGGER prevent_post_delete BEFORE DELETE ON CC_POSTS BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    store.connection.commit()
    confirm(app)
    app.button(key="delete-all-saved-data").click().run()
    assert not app.exception
    assert counts(store) == before
    assert len(app.session_state["candidates"]) == 1
    assert any("could not be deleted" in error.value for error in app.error)
    store.connection.execute("DROP TRIGGER prevent_post_delete")
    store.connection.commit()
    app.button(key="delete-all-saved-data").click().run()
    assert not app.exception
    assert counts(store) == dict.fromkeys(TABLES, 0)


def test_cloud_delete_requires_new_confirmation_and_preserves_local_library(tmp_path, monkeypatch):
    app, _, _ = populated_app()
    local = app.session_state["library_store"]
    local_before = counts(local)
    remote = open_local(tmp_path / "remote.sqlite3", "cloud-workspace")
    remote.kind = "Snowflake"
    seed(remote)
    monkeypatch.setattr("clubcal.persistence.open_snowflake", Mock(return_value=remote))
    confirm(app)
    next(field for field in app.text_input if field.label == "Snowflake account identifier").input("ORG-ACCOUNT")
    next(field for field in app.text_input if field.label == "Snowflake username").input("emily")
    next(button for button in app.button if button.label == "Connect & load Snowflake library").click().run()
    assert not app.exception
    assert app.button(key="delete-all-saved-data").disabled
    confirm(app)
    app.button(key="delete-all-saved-data").click().run()
    assert not app.exception
    assert counts(remote) == dict.fromkeys(TABLES, 0)
    assert counts(local) == local_before
