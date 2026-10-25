"""Streamlit library lifecycle and optional Snowflake setup UI."""
from __future__ import annotations

import os
from dataclasses import fields
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import streamlit as st

from .models import merge_candidates
from .storage import LibraryStore, SnowflakeSettings, SnowflakeSetupError, StorageError, content_hash, encode, now, open_local, open_snowflake, snapshot

ROOT = Path(__file__).resolve().parents[1]


@st.cache_resource
def local_store(path: str):
    return open_local(Path(path))


def initialize_library():
    if "library_store" in st.session_state:
        return
    state = st.session_state
    path = Path(os.getenv("CLUBCAL_DATA_DIR", str(ROOT / ".clubcal"))) / "clubcal.sqlite3"
    state.local_store = local_store(str(path))
    state.library_store = state.local_store
    saved, skipped = state.library_store.load_library()
    state.library_baseline = snapshot(saved, skipped)
    state.candidates, _ = merge_candidates(state.get("candidates", []), saved)
    state.skipped_recaps, _ = merge_candidates(state.get("skipped_recaps", []), skipped)
    state.import_history = state.library_store.load_history()
    state.history_baseline = {item["id"]: encode(item) for item in state.import_history}
    state.removed_event_ids = state.library_store.deleted_ids()
    if not state.get("collected_posts"):
        state.collected_posts = state.library_store.collected_posts()
    state.post_queue = {}
    state.post_memo = {}
    state.storage_error = ""
    state.last_saved = ""


def flush_library() -> bool:
    state = st.session_state
    current = snapshot(state.candidates, state.skipped_recaps)
    history = [item for item in state.import_history if state.history_baseline.get(item["id"]) != encode(item)]
    posts = list(state.post_queue.values())
    if current == state.library_baseline and not history and not posts:
        if state.storage_error:
            try:
                state.library_store.load_library()
            except StorageError:
                return False
            state.storage_error = ""
        return True
    try:
        state.library_store.save_changes(current, state.library_baseline, history, posts)
    except StorageError as exc:
        state.storage_error = str(exc)
        return False
    state.removed_event_ids.update(set(state.library_baseline) - set(current))
    state.library_baseline = current
    state.history_baseline = {item["id"]: encode(item) for item in state.import_history}
    state.post_queue = {}
    state.storage_error = ""
    state.last_saved = now()
    return True


def saved_rerun():
    flush_library()
    st.rerun()


def begin_import(source: str, accounts=()) -> str:
    identity = uuid4().hex
    st.session_state.import_history.insert(0, {
        "id": identity, "source": source, "accounts": list(dict.fromkeys(accounts)),
        "started_at": now(), "finished_at": "", "status": "running",
        "posts": 0, "events": 0, "cached": 0, "skipped": 0, "error": "",
    })
    st.session_state.import_history = st.session_state.import_history[:100]
    # Record collection attempts before making a potentially billable request.
    flush_library()
    return identity


def finish_import(identity: str, *, posts=0, events=0, cached=0, skipped=0, error="",
                  detected=None, existing=0, deleted=0, non_events=0):
    item = next(item for item in st.session_state.import_history if item["id"] == identity)
    item.update(posts=posts, events=events, cached=cached, skipped=skipped,
                error=error, status="failed" if error else "succeeded", finished_at=now())
    if detected is not None:
        item.update(detected=detected, existing=existing, deleted=deleted, non_events=non_events)
    flush_library()


def restore_imported_events(identities: list[str]):
    """Keep storage and session deletion decisions in sync after an explicit restore."""
    if not flush_library():
        return None
    state = st.session_state
    try:
        # A running Streamlit session can retain a store created before this method existed.
        restored = LibraryStore.restore_deleted(state.library_store, identities)
    except StorageError as exc:
        state.storage_error = str(exc)
        return None
    existing = {item.id for item in state.candidates}
    state.candidates.extend(item for item in restored if item.id not in existing)
    state.removed_event_ids.difference_update(item.id for item in restored)
    state.library_baseline.update(snapshot(restored, []))
    state.last_saved = now()
    return restored


def _delete_confirmation_key(store):
    return f"confirm-delete-saved-{id(store)}-{st.session_state.get('saved_data_scope_version', 0)}"


def delete_saved_data(store, confirmation_key):
    """Run before rendering so old forms and pending writes cannot restore cleared data."""
    state = st.session_state
    if state.library_store is not store or confirmation_key != _delete_confirmation_key(store) or not state.get(confirmation_key):
        return
    try:
        # Support store objects retained across a Streamlit code reload.
        LibraryStore.clear_workspace(store)
    except StorageError:
        state.saved_data_delete_error = "Saved data could not be deleted. Your library is unchanged. Check the storage connection and try again."
        return
    state.candidates, state.skipped_recaps, state.collected_posts = [], [], []
    state.import_history, state.removed_event_ids = [], set()
    state.library_baseline, state.history_baseline = {}, {}
    state.post_queue, state.post_memo = {}, {}
    state.storage_error, state.last_saved = "", ""
    for name in ("extraction_summary", "batch_approval_notice", "batch_delete_notice", "saved_data_delete_error"):
        state.pop(name, None)
    for key in list(state):
        if key.startswith(("sd-", "st-", "ed-", "et-", "evidence-", "select-event-", "skipped-", "confirm-delete-saved-")):
            state.pop(key, None)
    state.saved_data_deleted_notice = f"Deleted all saved data from {store.kind} workspace ‘{store.workspace}’."


def render_delete_saved_data():
    state = st.session_state
    notice = state.pop("saved_data_deleted_notice", None)
    if notice:
        st.success(notice)
    with st.expander("Delete saved data"):
        store = state.library_store
        st.write(f"Permanently clear **{store.kind} · {store.workspace}**.")
        st.caption("Deletes events, edits and approvals, skipped recaps, collected posts, cached extraction results, import history, and previous deletion records. This cannot be undone in ClubCal.")
        if store.kind == "Snowflake":
            st.caption("Only this Snowflake workspace is cleared. Your local library and other workspaces are kept.")
        st.caption("API keys, connection settings, exported calendars, and downloaded backup files are kept.")
        confirmation_key = _delete_confirmation_key(store)
        confirmed = st.checkbox("I understand this permanently deletes this workspace's saved data", key=confirmation_key)
        if state.get("saved_data_delete_error"):
            st.error(state.saved_data_delete_error)
        st.button("Delete all saved data", type="primary", key="delete-all-saved-data", disabled=not confirmed,
                  on_click=delete_saved_data, args=(store, confirmation_key))


def processing_key(post, timezone, ai, use_ocr, max_images) -> str:
    from .refresh import PARSER_VERSION
    from datetime import datetime
    from zoneinfo import ZoneInfo
    value = {"version": PARSER_VERSION, "timezone": timezone, "ocr": use_ocr, "slides": max_images,
             "model": ai.model if ai else "rules", "endpoint": ai.base_url if ai else "",
             "reference_day": datetime.now(ZoneInfo(timezone)).date().isoformat() if not post.timestamp else ""}
    return sha256(encode(value).encode()).hexdigest()


def cached_result(post, key, images=None):
    identity = (post.id, key, content_hash(post, images))
    if identity in st.session_state.post_memo:
        return st.session_state.post_memo[identity]
    try:
        result = st.session_state.library_store.cached_post(post, key, images)
    except StorageError as exc:
        st.session_state.storage_error = str(exc)
        return None
    if result is not None:
        st.session_state.post_memo[identity] = result
    return result


def queue_post(post, key, items, warnings, images=None, failed=False):
    digest = content_hash(post, images)
    valid = not failed and not any("could not be read" in warning or "No text detected" in warning or "No image URLs" in warning for warning in warnings)
    st.session_state.post_queue[(post.id, key)] = {
        "post": post, "key": key, "hash": digest, "items": items, "warnings": warnings, "valid": valid,
    }
    if valid:
        st.session_state.post_memo[(post.id, key, digest)] = (items, warnings)


def render_storage_status():
    state = st.session_state
    kind = state.library_store.kind
    if state.storage_error:
        st.warning("Changes are waiting to be saved.")
        st.caption(state.storage_error)
        if st.button("Retry saving"):
            if flush_library():
                st.rerun()
    else:
        st.caption(f"💾 {kind} storage · {state.library_store.workspace}")
        if state.last_saved:
            st.caption("Events and corrections saved.")


def _switch_store(store):
    saved, skipped = store.load_library()
    history = store.load_history()
    deleted = store.deleted_ids()
    collected = store.collected_posts()
    state = st.session_state
    state.library_store = store
    state.candidates, state.skipped_recaps = saved, skipped
    state.library_baseline = snapshot(saved, skipped)
    state.import_history = history
    state.history_baseline = {item["id"]: encode(item) for item in history}
    state.removed_event_ids = deleted
    state.collected_posts = collected
    state.post_queue, state.post_memo = {}, {}
    state.storage_error = ""
    state.parser_version = 0
    state.pop("extraction_summary", None)
    state.saved_data_scope_version = state.get("saved_data_scope_version", 0) + 1
    state.pop("saved_data_delete_error", None)
    state.pop("saved_data_deleted_notice", None)
    pending = state.pop("snowflake_pending_setup", None)
    if pending and pending["store"] is not store:
        pending["store"].close()
    # Existing widgets must show the newly loaded library's saved values.
    for identity in state.library_baseline:
        for prefix in ("sd", "st", "ed", "et", "evidence"):
            state.pop(f"{prefix}-{identity}", None)


def _configured_settings() -> dict:
    names = {item.name for item in fields(SnowflakeSettings)}
    config = {}
    # Only read the project's ignored secrets file; tests can select a separate path.
    secret_path = Path(os.getenv("CLUBCAL_SECRETS_PATH", str(ROOT / ".streamlit" / "secrets.toml")))
    if secret_path.exists():
        import tomllib
        try:
            with secret_path.open("rb") as file:
                values = tomllib.load(file)
            config = values.get("snowflake", values.get("connections", {}).get("snowflake", {}))
        except (OSError, ValueError):
            st.warning("Could not read the local Snowflake secrets file. Check its TOML format.")
    return {name: str(os.getenv("SNOWFLAKE_" + name.upper(), config.get(name, ""))) for name in names}


def render_pending_snowflake_setup():
    state = st.session_state
    pending = state.get("snowflake_pending_setup")
    if not pending:
        return
    st.info("Snowflake sign-in succeeded. Complete table setup below to load your cloud library.")
    st.error(pending["error"])
    if st.button("Check Snowflake setup"):
        from .snowflake_auth import connection_error, inspect_resources
        try:
            pending["resource_checks"] = inspect_resources(pending["store"], pending["settings"])
        except Exception as exc:
            pending["check_error"] = connection_error(exc)
        st.rerun()
    if pending.get("check_error"):
        st.error(pending["check_error"])
    for check in pending.get("resource_checks", []):
        if check["available"] is None:
            st.caption(f"{check['resource']}: check after the database is available.")
        elif check["available"]:
            st.caption(f"{check['resource']}: available.")
        else:
            st.warning(f"{check['resource']}: missing or not accessible to role {pending['settings'].role}.")
    if any(check["available"] is False and check["resource"].startswith(("Database ", "Schema ")) for check in pending.get("resource_checks", [])):
        from .snowflake_auth import resource_setup_sql
        st.markdown("**Create the missing database and schema**")
        st.caption(f"Run these statements in account {pending['settings'].account}, using your signed-in role {pending['settings'].role}. Existing objects are kept. A warehouse is not created.")
        st.code(";\n".join(resource_setup_sql(pending["settings"])) + ";", language="sql")
        confirmed = st.checkbox("I approve creating this database and schema in my Snowflake account", key=f"confirm-snowflake-resources-{id(pending['store'])}")
        if st.button("Create database and schema", disabled=not confirmed):
            from .snowflake_auth import connection_error, initialize_database_schema
            try:
                initialize_database_schema(pending["store"], pending["settings"])
                LibraryStore.ensure_schema(pending["store"])
                _switch_store(pending["store"])
                state.snowflake_settings = pending["settings"]
                st.rerun()
            except Exception as exc:
                pending["error"] = str(exc) if isinstance(exc, StorageError) else connection_error(exc)
                st.rerun()
    retry, cancel = st.columns(2)
    if retry.button("Retry Snowflake table setup"):
        try:
            with st.spinner("Preparing Snowflake tables using your signed-in session…"):
                LibraryStore.ensure_schema(pending["store"])
                _switch_store(pending["store"])
                state.snowflake_settings = pending["settings"]
            st.rerun()
        except StorageError as exc:
            pending["error"] = str(exc)
            st.rerun()
    if cancel.button("Cancel Snowflake setup"):
        pending["store"].close()
        state.pop("snowflake_pending_setup", None)
        st.rerun()


def render_saved_data():
    state = st.session_state
    st.subheader("Your saved event library")
    st.write(f"Storage: **{state.library_store.kind}** · Workspace: **{state.library_store.workspace}**")
    if state.storage_error:
        st.error(state.storage_error)
        st.caption("Your current edits are still in this browser session. Use Retry saving or download a session backup.")
    elif state.library_store.kind == "Local":
        st.info("Your events, reviews, skipped recaps, and import history are saved on this computer and restored after a reload.")
    else:
        st.success("Connected to Snowflake. Event changes and review decisions save automatically.")
        st.caption("Reconnect to this same account and workspace after opening a new app session to load your cloud library.")
        if st.button("Reconnect & retry saving"):
            try:
                replacement = open_snowflake(state.snowflake_settings)
                old = state.library_store
                state.library_store = replacement
                old.close()
                state.storage_error = ""
                saved_rerun()
            except StorageError as exc:
                st.error(str(exc))
        if st.button("Use local library") and flush_library():
            old = state.library_store
            _switch_store(state.local_store)
            old.close()
            st.rerun()
        if st.button("Copy local library into Snowflake"):
            try:
                if flush_library():
                    state.library_store.copy_from(state.local_store)
                    _switch_store(state.library_store)
                    st.rerun()
            except StorageError as exc:
                st.error(str(exc))
        st.caption("Copying keeps existing Snowflake records and their reviews when an event is already saved.")

    render_delete_saved_data()

    if state.candidates:
        organizations = sorted({item.organization for item in state.candidates})
        selected = st.multiselect("Filter saved events by organization", organizations)
        st.dataframe([{"Event": item.title, "Organization": item.organization,
                       "Date": item.start_date or "Unknown", "Time": "All-day" if item.all_day else item.start_time or "Unknown",
                       "Status": "Approved" if item.reviewed else "Needs review"}
                      for item in state.candidates if not selected or item.organization in selected], hide_index=True, width="stretch")

    st.markdown("**Import history**")
    if state.import_history:
        st.dataframe([{"Started (UTC)": item["started_at"], "Source": item["source"],
                       "Accounts": ", ".join(item["accounts"]), "Status": item["status"],
                       "Posts": item["posts"], "Detected events": item.get("detected", item["events"]),
                       "New events": item["events"], "Already saved": item.get("existing", 0),
                       "Previously deleted": item.get("deleted", 0), "Reused results": item["cached"],
                       "Skipped recaps": item["skipped"], "Error": item["error"]}
                      for item in state.import_history], hide_index=True, width="stretch")
    else:
        st.caption("Collection and extraction attempts will appear here, including failures.")

    with st.expander("Connect Snowflake", expanded=False):
        st.caption("This local app uses one private workspace. Sign in on your computer; no password or API key is stored with event records.")
        pending_slot = st.container()
        defaults = _configured_settings()
        with st.form("snowflake-connect"):
            account = st.text_input("Snowflake account identifier", defaults.get("account", ""), placeholder="ORG-ACCOUNT")
            st.caption("Use the client identifier ORG-ACCOUNT. Snowflake account URLs and ORG.ACCOUNT values are converted automatically; legacy regional identifiers are preserved.")
            user = st.text_input("Snowflake username", defaults.get("user", ""))
            col1, col2 = st.columns(2)
            warehouse = col1.text_input("Warehouse", defaults.get("warehouse") or "CLUBCAL_WH")
            role = col2.text_input("Role", defaults.get("role") or "CLUBCAL_APP")
            database = col1.text_input("Database", defaults.get("database") or "CLUBCAL")
            schema = col2.text_input("Schema", defaults.get("schema") or "APP")
            workspace = st.text_input("Saved workspace", defaults.get("workspace") or "personal")
            auth_methods = {"Browser sign-in": "oauth_authorization_code", "Organization SSO (SAML)": "externalbrowser",
                            "Key pair from local secrets": "snowflake_jwt"}
            options = list(auth_methods)
            default_auth = defaults.get("authenticator") or "oauth_authorization_code"
            auth = st.selectbox("Snowflake authentication", options, index=next((index for index, option in enumerate(options) if auth_methods[option] == default_auth), 0))
            st.caption("Browser sign-in opens Snowflake's own OAuth login; complete login and MFA in your browser. Organization SSO requires your account's SAML provider. Key-pair mode uses your local secrets file.")
            connect = st.form_submit_button("Connect & load Snowflake library", type="primary", disabled=state.library_store.kind == "Snowflake")
        if connect and flush_library():
            settings = SnowflakeSettings(account, user, warehouse, database, schema, role, workspace,
                auth_methods[auth],
                defaults.get("private_key_file", ""), defaults.get("private_key_file_pwd", ""))
            try:
                settings.validate()
                pending = state.pop("snowflake_pending_setup", None)
                if pending:
                    pending["store"].close()
                if settings.account != account.strip():
                    st.info(f"Using Snowflake account identifier: {settings.account}")
                with st.spinner("Connecting to Snowflake. Complete browser sign-in if prompted…"):
                    store = open_snowflake(settings)
                    try:
                        _switch_store(store)
                        state.snowflake_settings = settings
                    except Exception:
                        store.close()
                        raise
                st.rerun()
            except SnowflakeSetupError as exc:
                state.snowflake_pending_setup = {"store": exc.store, "settings": settings, "error": str(exc)}
            except StorageError as exc:
                st.error(str(exc))
        with pending_slot:
            render_pending_snowflake_setup()
        st.caption("Your local library stays on this computer when you connect. Copy it into Snowflake with the button shown after connection.")
        st.markdown("Setup: install `requirements-snowflake.txt`, run `scripts/snowflake_setup.sql` in your Snowflake account, and optionally configure `.streamlit/secrets.toml` using the example file.")
