import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from clubcal.models import Candidate, Post
from clubcal.storage import LibraryStore, SnowflakeSettings, StorageError, content_hash, open_local, open_snowflake, snapshot


def candidate(**kwargs):
    return replace(Candidate("one", "Workshop", "SWE", "2026-11-19", "17:30", end_time="19:00"), **kwargs)


def test_library_and_reviews_survive_closing_database(tmp_path):
    path = tmp_path / "library.sqlite3"
    store = open_local(path)
    base = candidate()
    before = snapshot([base], [])
    store.save_changes(before, {})
    reviewed = replace(base, title="My corrected title", start_time="18:00", reviewed=True)
    store.save_changes(snapshot([reviewed], []), before)
    store.close()
    reopened = open_local(path)
    active, skipped = reopened.load_library()
    assert active == [reviewed] and skipped == []
    extraction = json.loads(reopened.connection.execute("SELECT PAYLOAD FROM CC_EVENTS").fetchone()[0])
    assert extraction["title"] == "Workshop"
    assert extraction["start_time"] == "17:30"
    assert not extraction["reviewed"]
    reopened.close()


def test_reimport_keeps_review_and_removed_event_stays_removed(tmp_path):
    store = open_local(tmp_path / "library.sqlite3")
    base = snapshot([candidate()], [])
    store.save_changes(base, {})
    reviewed = snapshot([candidate(title="Reviewed", reviewed=True)], [])
    store.save_changes(reviewed, base)
    store.save_changes(base, {})  # Fresh extraction cannot replace the separate review row.
    assert store.load_library()[0][0].title == "Reviewed"
    store.save_changes({}, reviewed)
    store.save_changes(base, {})
    assert store.load_library() == ([], [])
    assert store.deleted_ids() == {"one"}


def test_workspace_boundaries_and_stale_sessions_do_not_delete_new_rows(tmp_path):
    path = tmp_path / "library.sqlite3"
    alice = open_local(path, "alice")
    bob = open_local(path, "bob")
    first = snapshot([candidate()], [])
    alice.save_changes(first, {})
    bob.save_changes(snapshot([candidate(title="Bob's private choice")], []), {})
    added_by_another_session = candidate(id="two", title="Another event")
    alice.save_changes(snapshot([added_by_another_session], []), {})
    alice.save_changes(snapshot([candidate(reviewed=True)], []), first)
    assert len(alice.load_library()[0]) == 2
    assert bob.load_library()[0][0].title == "Bob's private choice"


def test_skipped_recaps_and_import_failures_are_durable(tmp_path):
    path = tmp_path / "library.sqlite3"
    store = open_local(path)
    history = [{"id": "attempt", "source": "Apify collection", "accounts": ["swetexas"], "started_at": "2026-10-24T12:00:00Z", "status": "failed", "error": "ValueError"}]
    store.save_changes(snapshot([], [candidate()]), {}, history)
    store.close()
    reopened = open_local(path)
    assert len(reopened.load_library()[1]) == 1
    assert reopened.load_history() == history


def test_content_cache_ignores_signature_refresh_but_invalidates_caption_and_settings(tmp_path):
    store = open_local(tmp_path / "library.sqlite3")
    post = Post("post", "SWE", "Workshop", image_urls=["https://a.cdninstagram.com/flyer.jpg?signature=old"])
    entry = {"post": post, "key": "rules-v3", "hash": content_hash(post), "items": [candidate()], "warnings": [], "valid": True}
    store.save_changes({}, {}, posts=[entry])
    refreshed = replace(post, image_urls=["https://b.cdninstagram.com/flyer.jpg?signature=new"])
    assert store.cached_post(refreshed, "rules-v3")[0] == [candidate()]
    assert store.cached_post(replace(post, caption="Changed date"), "rules-v3") is None
    assert store.cached_post(post, "other-parser") is None
    assert content_hash(post, [b"image-1"]) != content_hash(post, [b"image-2"])


def test_failed_ocr_is_not_reused_and_cache_preserves_empty_results(tmp_path):
    store = open_local(tmp_path / "library.sqlite3")
    post = Post("post", "SWE", "Recap")
    row = {"post": post, "key": "rules", "hash": content_hash(post), "items": [], "warnings": ["Skipped recap"], "valid": False}
    store.save_changes({}, {}, posts=[row])
    assert store.cached_post(post, "rules") is None
    row["valid"] = True
    store.save_changes({}, {}, posts=[row])
    assert store.cached_post(post, "rules") == ([], ["Skipped recap"])


def test_sql_parameters_store_caption_and_title_as_data(tmp_path):
    store = open_local(tmp_path / "library.sqlite3")
    dangerous = "'); DROP TABLE CC_EVENTS; --"
    store.save_changes(snapshot([candidate(title=dangerous)], []), {})
    assert store.load_library()[0][0].title == dangerous


def test_transaction_failure_rolls_back_review_and_history_together(tmp_path, monkeypatch):
    store = open_local(tmp_path / "library.sqlite3")
    original = store._upsert
    def fail(cursor, table, *args, **kwargs):
        if table == "IMPORTS":
            raise RuntimeError("network failure with private details")
        return original(cursor, table, *args, **kwargs)
    monkeypatch.setattr(store, "_upsert", fail)
    with pytest.raises(StorageError, match="changes remain"):
        store.save_changes(snapshot([candidate()], []), {}, [{"id": "one", "started_at": "now"}])
    assert store.load_library() == ([], [])


def test_snowflake_merge_binds_values_and_preserves_extraction_rows():
    connection = Mock()
    cursor = connection.cursor.return_value
    store = LibraryStore(connection, workspace="personal", dialect="snowflake", prefix='"CLUBCAL"."APP".')
    dangerous = "private ' caption; DROP DATABASE anything"
    store.save_changes(snapshot([candidate(title=dangerous, reviewed=True)], []), {})
    statements = [call.args for call in cursor.execute.call_args_list if len(call.args) == 2]
    merges = [(sql, params) for sql, params in statements if sql.startswith("MERGE")]
    assert len(merges) == 2
    assert '"CLUBCAL"."APP".CC_EVENTS' in merges[0][0]
    assert "WHEN MATCHED" not in merges[0][0]
    assert "WHEN MATCHED" in merges[1][0]
    assert all(dangerous not in sql for sql, _ in merges)
    assert any(dangerous in value for value in merges[0][1] if isinstance(value, str))
    connection.commit.assert_called_once()


@pytest.mark.parametrize("field,value", [("database", 'X"; DROP DATABASE Y'), ("schema", "bad name"), ("role", "ADMIN;"), ("account", "https://example.com")])
def test_snowflake_identifiers_are_validated_before_connection(field, value):
    settings = SnowflakeSettings("ORG-ACCOUNT", "emily", "WH", "DB")
    setattr(settings, field, value)
    with pytest.raises(StorageError):
        settings.validate()


def test_official_connector_uses_local_auth_and_does_not_echo_auth_errors(monkeypatch):
    connector = pytest.importorskip("snowflake.connector")
    connect = Mock(side_effect=RuntimeError("secret-key-passphrase"))
    monkeypatch.setattr(connector, "connect", connect)
    settings = SnowflakeSettings("ORG-ACCOUNT", "emily", "WH", "DB", authenticator="snowflake_jwt", private_key_file="local-key.p8", private_key_file_pwd="secret-key-passphrase")
    with pytest.raises(StorageError) as error:
        open_snowflake(settings)
    assert "secret-key-passphrase" not in str(error.value)
    assert "secret-key-passphrase" not in repr(settings)
    assert connect.call_args.kwargs["private_key_password"] == "secret-key-passphrase"
    assert connect.call_args.kwargs["authenticator"] == "SNOWFLAKE_JWT"
    assert "password" not in connect.call_args.kwargs


def test_copy_preserves_original_review_cache_history_and_target_edits(tmp_path):
    source = open_local(tmp_path / "source.sqlite3")
    target = open_local(tmp_path / "target.sqlite3", "cloud-workspace")
    base = snapshot([candidate()], [])
    post = Post("post", "SWE", "Workshop")
    cached = {"post": post, "key": "rules", "hash": content_hash(post), "items": [candidate()], "warnings": [], "valid": True}
    history = [{"id": "import", "started_at": "2026-10-24T10:00:00Z", "status": "succeeded"}]
    source.save_changes(base, {}, history, [cached])
    source.save_changes(snapshot([candidate(title="Local correction", reviewed=True)], []), base)
    target.copy_from(source)
    assert target.load_library()[0][0].title == "Local correction"
    assert target.load_history() == history
    assert target.cached_post(post, "rules")[0][0].title == "Workshop"
    assert json.loads(target.connection.execute("SELECT PAYLOAD FROM CC_EVENTS").fetchone()[0])["title"] == "Workshop"
    cloud_before = snapshot(*target.load_library())
    target.save_changes(snapshot([candidate(title="Cloud correction", reviewed=True)], []), cloud_before)
    target.copy_from(source)
    assert target.load_library()[0][0].title == "Cloud correction"
