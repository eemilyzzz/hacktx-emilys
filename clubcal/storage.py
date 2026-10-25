"""Durable library storage. Credentials never enter library or cache records."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

from .models import Candidate, Post
from .snowflake_auth import connection_error, normalize_account


class StorageError(Exception):
    pass


class SnowflakeSetupError(StorageError):
    """Authentication succeeded; retain the session so schema setup can be retried."""
    def __init__(self, message, store):
        super().__init__(message)
        self.store = store


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_candidate(payload: str) -> Candidate:
    allowed = {item.name for item in fields(Candidate)}
    return Candidate(**{key: value for key, value in json.loads(payload).items() if key in allowed})


def snapshot(candidates: list[Candidate], skipped: list[Candidate]) -> dict:
    return {item.id: {"payload": item.to_dict(), "state": state}
            for items, state in ((candidates, "active"), (skipped, "skipped")) for item in items}


def content_hash(post: Post, images: list[bytes] | None = None) -> str:
    data = asdict(post)
    # Instagram refreshes signed URLs without changing the underlying slide.
    data["image_urls"] = [urlsplit(url).path for url in post.image_urls]
    data["local_image_hashes"] = [sha256(image).hexdigest() for image in images or []]
    return sha256(encode(data).encode()).hexdigest()


@dataclass(repr=False)
class SnowflakeSettings:
    account: str
    user: str
    warehouse: str
    database: str
    schema: str = "APP"
    role: str = "CLUBCAL_APP"
    workspace: str = "personal"
    authenticator: str = "oauth_authorization_code"
    private_key_file: str = ""
    private_key_file_pwd: str = field(default="", repr=False)

    def validate(self):
        try:
            self.account = normalize_account(self.account)
        except ValueError as exc:
            raise StorageError(str(exc)) from None
        self.user = self.user.strip()
        if not self.user.strip():
            raise StorageError("Enter your Snowflake username.")
        for name in ("warehouse", "database", "schema", "role"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,254}", getattr(self, name)):
                raise StorageError(f"Use a standard Snowflake identifier for {name} (letters, numbers, underscores).")
        if not self.workspace.strip() or len(self.workspace) > 128:
            raise StorageError("Enter a workspace name of 1–128 characters.")
        if self.authenticator not in {"oauth_authorization_code", "externalbrowser", "snowflake_jwt"}:
            raise StorageError("Choose browser sign-in, organization SSO, or key-pair authentication.")
        if self.authenticator == "snowflake_jwt" and not self.private_key_file:
            raise StorageError("Configure a private_key_file in your local Snowflake secrets.")


class LibraryStore:
    """A private workspace with extraction records and separate review overrides."""
    def __init__(self, connection, workspace="personal", dialect="sqlite", prefix=""):
        self.connection = connection
        self.workspace = workspace
        self.dialect = dialect
        self.prefix = prefix
        self.lock = RLock()
        self.kind = "Snowflake" if dialect == "snowflake" else "Local"

    def _table(self, name):
        return self.prefix + "CC_" + name

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.dialect == "snowflake" else sql

    def _execute(self, cursor, sql, params=()):
        return cursor.execute(self._sql(sql), tuple(params))

    @contextmanager
    def transaction(self):
        with self.lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
                self.connection.commit()
            except Exception:
                try:
                    self.connection.rollback()
                except Exception:
                    pass
                raise StorageError(f"Could not save to {self.kind}. Your changes remain in this session; retry saving.") from None
            finally:
                cursor.close()

    def ensure_schema(self):
        definitions = {
            "EVENTS": "WORKSPACE VARCHAR NOT NULL, EVENT_ID VARCHAR NOT NULL, PAYLOAD VARCHAR NOT NULL, STATE VARCHAR NOT NULL, CREATED_AT VARCHAR NOT NULL, UPDATED_AT VARCHAR NOT NULL, PRIMARY KEY (WORKSPACE, EVENT_ID)",
            "REVIEWS": "WORKSPACE VARCHAR NOT NULL, EVENT_ID VARCHAR NOT NULL, PAYLOAD VARCHAR NOT NULL, STATE VARCHAR NOT NULL, UPDATED_AT VARCHAR NOT NULL, PRIMARY KEY (WORKSPACE, EVENT_ID)",
            "POSTS": "WORKSPACE VARCHAR NOT NULL, POST_ID VARCHAR NOT NULL, PROCESSING_KEY VARCHAR NOT NULL, CONTENT_HASH VARCHAR NOT NULL, PAYLOAD VARCHAR NOT NULL, RESULT VARCHAR NOT NULL, WARNINGS VARCHAR NOT NULL, CACHE_VALID BOOLEAN NOT NULL, UPDATED_AT VARCHAR NOT NULL, PRIMARY KEY (WORKSPACE, POST_ID, PROCESSING_KEY)",
            "IMPORTS": "WORKSPACE VARCHAR NOT NULL, IMPORT_ID VARCHAR NOT NULL, PAYLOAD VARCHAR NOT NULL, STARTED_AT VARCHAR NOT NULL, PRIMARY KEY (WORKSPACE, IMPORT_ID)",
        }
        with self.lock:
            cursor = self.connection.cursor()
            try:
                for name, definition in definitions.items():
                    cursor.execute(f"CREATE TABLE IF NOT EXISTS {self._table(name)} ({definition})")
                self.connection.commit()
            except Exception as exc:
                if self.dialect == "snowflake":
                    raise StorageError("Snowflake sign-in succeeded, but table setup failed. " + connection_error(exc)) from None
                raise StorageError(f"Could not prepare {self.kind} tables. Check the database, schema, role, and CREATE TABLE permission.") from None
            finally:
                cursor.close()

    def _upsert(self, cursor, table: str, values: dict, keys: tuple[str, ...], insert_only=False):
        columns = list(values)
        target = self._table(table)
        if self.dialect == "sqlite":
            sql = f"INSERT INTO {target} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT ({', '.join(keys)}) "
            sql += "DO NOTHING" if insert_only else "DO UPDATE SET " + ", ".join(f"{column} = excluded.{column}" for column in columns if column not in keys)
        else:
            source = ", ".join(f"? AS {column}" for column in columns)
            join = " AND ".join(f"target.{key} = source.{key}" for key in keys)
            sql = f"MERGE INTO {target} target USING (SELECT {source}) source ON {join} "
            if not insert_only:
                sql += "WHEN MATCHED THEN UPDATE SET " + ", ".join(f"target.{column} = source.{column}" for column in columns if column not in keys) + " "
            sql += f"WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) VALUES ({', '.join('source.' + column for column in columns)})"
        self._execute(cursor, sql, list(values.values()))

    def load_library(self) -> tuple[list[Candidate], list[Candidate]]:
        sql = f"SELECT e.PAYLOAD, e.STATE, r.PAYLOAD, r.STATE FROM {self._table('EVENTS')} e LEFT JOIN {self._table('REVIEWS')} r ON e.WORKSPACE = r.WORKSPACE AND e.EVENT_ID = r.EVENT_ID WHERE e.WORKSPACE = ? ORDER BY e.CREATED_AT, e.EVENT_ID"
        with self.lock:
            cursor = self.connection.cursor()
            try:
                rows = self._execute(cursor, sql, [self.workspace]).fetchall()
                active, skipped = [], []
                for base, base_state, review, review_state in rows:
                    state = review_state if review is not None else base_state
                    if state == "deleted":
                        continue
                    item = decode_candidate(review if review is not None else base)
                    (skipped if state == "skipped" else active).append(item)
                return active, skipped
            except Exception:
                raise StorageError(f"Could not load the {self.kind} event library. Check the connection and table permissions.") from None
            finally:
                cursor.close()

    def load_history(self) -> list[dict]:
        with self.lock:
            cursor = self.connection.cursor()
            try:
                rows = self._execute(cursor, f"SELECT PAYLOAD FROM {self._table('IMPORTS')} WHERE WORKSPACE = ? ORDER BY STARTED_AT DESC LIMIT 100", [self.workspace]).fetchall()
                return [json.loads(row[0]) for row in rows]
            except Exception:
                raise StorageError(f"Could not load {self.kind} import history.") from None
            finally:
                cursor.close()

    def deleted_ids(self) -> set[str]:
        with self.lock:
            cursor = self.connection.cursor()
            try:
                return {row[0] for row in self._execute(cursor, f"SELECT EVENT_ID FROM {self._table('REVIEWS')} WHERE WORKSPACE = ? AND STATE = ?", [self.workspace, "deleted"]).fetchall()}
            except Exception:
                raise StorageError(f"Could not load removed-event records from {self.kind}.") from None
            finally:
                cursor.close()

    def restore_deleted(self, identities: list[str]) -> list[Candidate]:
        """Restore only requested tombstones, preserving edits and requiring review."""
        identities = sorted(set(identities))
        if not identities:
            return []
        restored = []
        with self.transaction() as cursor:
            placeholders = ", ".join("?" for _ in identities)
            rows = self._execute(cursor,
                f"SELECT PAYLOAD FROM {self._table('REVIEWS')} WHERE WORKSPACE = ? AND STATE = ? AND EVENT_ID IN ({placeholders})",
                [self.workspace, "deleted", *identities]).fetchall()
            for row in rows:
                item = replace(decode_candidate(row[0]), reviewed=False)
                self._upsert(cursor, "REVIEWS", {
                    "WORKSPACE": self.workspace, "EVENT_ID": item.id,
                    "PAYLOAD": encode(item.to_dict()), "STATE": "active", "UPDATED_AT": now(),
                }, ("WORKSPACE", "EVENT_ID"))
                restored.append(item)
        return restored

    def clear_workspace(self) -> None:
        """Delete this workspace's library, history, and caches atomically."""
        with self.transaction() as cursor:
            for name in ("REVIEWS", "EVENTS", "POSTS", "IMPORTS"):
                self._execute(cursor, f"DELETE FROM {self._table(name)} WHERE WORKSPACE = ?", [self.workspace])

    def collected_posts(self) -> list[Post]:
        with self.lock:
            cursor = self.connection.cursor()
            try:
                rows = self._execute(cursor, f"SELECT PAYLOAD FROM {self._table('POSTS')} WHERE WORKSPACE = ? AND PROCESSING_KEY = ? ORDER BY UPDATED_AT DESC LIMIT 300", [self.workspace, "collected"]).fetchall()
                return [Post(**json.loads(row[0])) for row in rows]
            except Exception:
                raise StorageError(f"Could not load collected posts from {self.kind}.") from None
            finally:
                cursor.close()

    def _rows(self, table: str, columns: list[str]) -> list[dict]:
        with self.lock:
            cursor = self.connection.cursor()
            try:
                rows = self._execute(cursor, f"SELECT {', '.join(columns)} FROM {self._table(table)} WHERE WORKSPACE = ?", [self.workspace]).fetchall()
                return [dict(zip(columns, row)) for row in rows]
            except Exception:
                raise StorageError(f"Could not read saved {table.lower()} from {self.kind}.") from None
            finally:
                cursor.close()

    def copy_from(self, source: LibraryStore):
        """Copy a library while retaining original extractions and existing cloud edits."""
        definitions = {
            "EVENTS": (["EVENT_ID", "PAYLOAD", "STATE", "CREATED_AT", "UPDATED_AT"], ("WORKSPACE", "EVENT_ID")),
            "REVIEWS": (["EVENT_ID", "PAYLOAD", "STATE", "UPDATED_AT"], ("WORKSPACE", "EVENT_ID")),
            "POSTS": (["POST_ID", "PROCESSING_KEY", "CONTENT_HASH", "PAYLOAD", "RESULT", "WARNINGS", "CACHE_VALID", "UPDATED_AT"], ("WORKSPACE", "POST_ID", "PROCESSING_KEY")),
            "IMPORTS": (["IMPORT_ID", "PAYLOAD", "STARTED_AT"], ("WORKSPACE", "IMPORT_ID")),
        }
        rows = {table: source._rows(table, columns) for table, (columns, _) in definitions.items()}
        existing_ids = {row["EVENT_ID"] for row in self._rows("EVENTS", ["EVENT_ID"])}
        with self.transaction() as cursor:
            for table, (_, keys) in definitions.items():
                for row in rows[table]:
                    if table in {"EVENTS", "REVIEWS"} and row["EVENT_ID"] in existing_ids:
                        continue
                    self._upsert(cursor, table, {"WORKSPACE": self.workspace, **row}, keys, insert_only=True)

    def cached_post(self, post: Post, processing_key: str, images=None):
        with self.lock:
            cursor = self.connection.cursor()
            try:
                row = self._execute(cursor, f"SELECT RESULT, WARNINGS FROM {self._table('POSTS')} WHERE WORKSPACE = ? AND POST_ID = ? AND PROCESSING_KEY = ? AND CONTENT_HASH = ? AND CACHE_VALID = ?", [self.workspace, post.id, processing_key, content_hash(post, images), True]).fetchone()
                if not row:
                    return None
                return [decode_candidate(encode(item)) for item in json.loads(row[0])], json.loads(row[1])
            except Exception:
                raise StorageError(f"Could not read the {self.kind} post cache.") from None
            finally:
                cursor.close()

    def save_changes(self, current: dict, baseline: dict, history=(), posts=()):
        """Only write this session's changes; other sessions' new rows are untouched."""
        changed = {key: value for key, value in current.items() if baseline.get(key) != value}
        deleted = set(baseline) - set(current)
        if not changed and not deleted and not history and not posts:
            return
        with self.transaction() as cursor:
            timestamp = now()
            for identity, entry in changed.items():
                payload = encode(entry["payload"])
                base = {"WORKSPACE": self.workspace, "EVENT_ID": identity, "PAYLOAD": payload, "STATE": entry["state"], "CREATED_AT": timestamp, "UPDATED_AT": timestamp}
                self._upsert(cursor, "EVENTS", base, ("WORKSPACE", "EVENT_ID"), insert_only=True)
                # A baseline entry means an edit; reviewed imports also need a review record.
                if identity in baseline or entry["payload"].get("reviewed"):
                    self._upsert(cursor, "REVIEWS", {key: value for key, value in base.items() if key != "CREATED_AT"}, ("WORKSPACE", "EVENT_ID"))
            for identity in deleted:
                self._upsert(cursor, "REVIEWS", {"WORKSPACE": self.workspace, "EVENT_ID": identity, "PAYLOAD": encode(baseline[identity]["payload"]), "STATE": "deleted", "UPDATED_AT": timestamp}, ("WORKSPACE", "EVENT_ID"))
            for item in history:
                self._upsert(cursor, "IMPORTS", {"WORKSPACE": self.workspace, "IMPORT_ID": item["id"], "PAYLOAD": encode(item), "STARTED_AT": item["started_at"]}, ("WORKSPACE", "IMPORT_ID"))
            for item in posts:
                post = item["post"]
                self._upsert(cursor, "POSTS", {
                    "WORKSPACE": self.workspace, "POST_ID": post.id, "PROCESSING_KEY": item["key"],
                    "CONTENT_HASH": item["hash"], "PAYLOAD": encode(asdict(post)),
                    "RESULT": encode([candidate.to_dict() for candidate in item["items"]]),
                    "WARNINGS": encode(item["warnings"]), "CACHE_VALID": item["valid"], "UPDATED_AT": timestamp,
                }, ("WORKSPACE", "POST_ID", "PROCESSING_KEY"))

    def close(self):
        try:
            self.connection.close()
        except Exception:
            pass


def open_local(path: Path, workspace="personal") -> LibraryStore:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=15)
    connection.execute("PRAGMA journal_mode=WAL")
    store = LibraryStore(connection, workspace)
    store.ensure_schema()
    return store


def open_snowflake(settings: SnowflakeSettings) -> LibraryStore:
    settings.validate()
    try:
        import snowflake.connector
    except ImportError:
        raise StorageError("Install the optional Snowflake dependencies: python -m pip install -r requirements-snowflake.txt") from None
    kwargs = {name: getattr(settings, name) for name in ("account", "user", "warehouse", "database", "schema", "role", "authenticator")}
    kwargs["authenticator"] = {"snowflake_jwt": "SNOWFLAKE_JWT", "externalbrowser": "EXTERNALBROWSER",
                               "oauth_authorization_code": "OAUTH_AUTHORIZATION_CODE"}[settings.authenticator]
    kwargs.update(login_timeout=300, authentication_timeout=300, request_timeout=30, query_timeout=30,
                  connect_timeout=15, retry_timeout=30, client_session_keep_alive=False,
                  client_store_temporary_credential=False, autocommit=False)
    if settings.private_key_file:
        kwargs["private_key_file"] = settings.private_key_file
        if settings.private_key_file_pwd:
            kwargs["private_key_password"] = settings.private_key_file_pwd
    try:
        connection = snowflake.connector.connect(**kwargs)
    except Exception as exc:
        raise StorageError(connection_error(exc)) from None
    prefix = f'"{settings.database.upper()}"."{settings.schema.upper()}".'
    store = LibraryStore(connection, settings.workspace, "snowflake", prefix)
    try:
        store.ensure_schema()
    except StorageError as exc:
        raise SnowflakeSetupError(str(exc), store) from None
    return store
