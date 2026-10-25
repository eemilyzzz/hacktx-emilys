from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from clubcal.models import Candidate
from clubcal.snowflake_auth import connection_error, initialize_database_schema, inspect_resources, normalize_account, resource_setup_sql
from clubcal.storage import SnowflakeSettings, SnowflakeSetupError, StorageError, open_local, open_snowflake

MAIN = Path(__file__).resolve().parents[1] / "main.py"


@pytest.mark.parametrize("value,expected", [
    (" PASDZWR.WQ15369 ", "PASDZWR-WQ15369"),
    ("myorg.my_account", "myorg-my_account"),
    ("myorg-my-account", "myorg-my-account"),
    ("https://app.snowflake.com/myorg/myaccount/#/worksheets", "myorg-myaccount"),
    ("https://myorg-myaccount.snowflakecomputing.com/", "myorg-myaccount"),
    ("myorg-myaccount.snowflakecomputing.com", "myorg-myaccount"),
    ("xy12345.us-east-1", "xy12345.us-east-1"),
    ("xy12345.us-east-2.aws", "xy12345.us-east-2.aws"),
    ("xy12345.europe-west2.gcp", "xy12345.europe-west2.gcp"),
    ("myorg-myaccount.privatelink", "myorg-myaccount.privatelink"),
    ("xy12345.privatelink", "xy12345.privatelink"),
    ("xy12345.global", "xy12345.global"),
])
def test_account_formats_are_normalized_without_breaking_regional_addresses(value, expected):
    assert normalize_account(value) == expected


@pytest.mark.parametrize("value", ["", "org..account", "org-account.", "https://example.com", "https://app.snowflake.com/org", "https://user:secret@org-account.snowflakecomputing.com", "http://org-account.snowflakecomputing.com", "org/account", "org;account"])
def test_invalid_account_values_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_account(value)


class ConnectorFailure(Exception):
    errno = 250001
    sqlstate = "08001"


@pytest.mark.parametrize("private_detail,message", [
    ("[WinError 10013] forbidden", "blocked from connecting"),
    ("getaddrinfo failed", "could not be resolved"),
    ("The user you were trying to authenticate as differs from the user currently logged in at the IDP", "does not match"),
    ("Role CLUBCAL_APP is not granted to this user", "role is unavailable"),
    ("SQL access control error: Insufficient privileges to operate on schema", "lacks permission"),
    ("SQL compilation error: syntax error line 1 at position 55 unexpected 'STATE'", "Unexpected SQL token: STATE"),
    ("Requested warehouse CLUBCAL_WH does not exist or not authorized", "warehouse, database, or schema"),
    ("IP address is not allowed by network policy", "network policy rejected"),
    ("External browser SSO failed: authenticator request was unsuccessful. Error related to the SAML_IDENTITY_PROVIDER parameter", "local OAuth login"),
    ("TLS certificate verify failed", "secure connection could not be verified"),
    ("HTTP 404 account not found", "account endpoint"),
    ("Incorrect username or password", "rejected the sign-in"),
    ("Failed to open browser", "could not open"),
    ("Browser authentication timeout", "timed out"),
    ("JWT private key authentication failure", "key-pair authentication failed"),
    ("error sending request for url", "could not reach Snowflake"),
    ("unrecognized private response", "could not establish a session"),
])
def test_connection_failures_are_actionable_and_do_not_echo_private_payloads(private_detail, message):
    error = ConnectorFailure(private_detail + " SECRET-PASSPHRASE Bearer PRIVATE-TOKEN https://private-idp.example/token?token=HIDDEN")
    result = connection_error(error)
    assert message in result and "code 250001" in result and "SQLSTATE 08001" in result
    for secret in ("SECRET-PASSPHRASE", "PRIVATE-TOKEN", "private-idp", "HIDDEN"):
        assert secret not in result


def test_connection_failure_checks_nested_network_cause():
    error = ConnectorFailure("Failed to authenticate")
    error.__cause__ = OSError(10013, "Network blocked")
    assert "Windows error 10013" in connection_error(error)


def test_resource_checks_read_only_and_distinguish_missing_database():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.description = [("name",)]
    cursor.fetchall.side_effect = [[], [("CLUBCAL_WH",)]]
    store = Mock(connection=connection)
    settings = SnowflakeSettings("ORG-ACCOUNT", "user", "CLUBCAL_WH", "CLUBCAL")
    checks = inspect_resources(store, settings)
    assert [check["available"] for check in checks] == [False, None, True]
    assert all(call.args[1].startswith("SHOW ") for call in store._execute.call_args_list)
    assert store._execute.call_count == 2


def test_resource_initialization_only_creates_the_configured_database_and_schema():
    connection = Mock()
    store = Mock(connection=connection)
    settings = SnowflakeSettings("ORG-ACCOUNT", "user", "CLUBCAL_WH", "CLUBCAL")
    initialize_database_schema(store, settings)
    statements = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
    assert statements == ['CREATE DATABASE IF NOT EXISTS "CLUBCAL"', 'CREATE SCHEMA IF NOT EXISTS "CLUBCAL"."APP"']
    assert resource_setup_sql(settings) == statements
    assert all("WAREHOUSE" not in sql and "GRANT" not in sql and "DROP" not in sql for sql in statements)


def test_connector_receives_normalized_account_and_does_not_persist_auth_tokens(monkeypatch):
    connector = pytest.importorskip("snowflake.connector")
    connect = Mock(side_effect=ConnectorFailure("HTTP 404"))
    monkeypatch.setattr(connector, "connect", connect)
    settings = SnowflakeSettings("PASDZWR.WQ15369", " my-user ", "WH", "DB")
    with pytest.raises(StorageError, match="account endpoint"):
        open_snowflake(settings)
    kwargs = connect.call_args.kwargs
    assert kwargs["account"] == "PASDZWR-WQ15369" and kwargs["user"] == "my-user"
    assert kwargs["authenticator"] == "OAUTH_AUTHORIZATION_CODE"
    assert kwargs["client_store_temporary_credential"] is False
    assert kwargs["login_timeout"] >= 120


def test_organization_sso_remains_available_for_configured_accounts(monkeypatch):
    connector = pytest.importorskip("snowflake.connector")
    connect = Mock(side_effect=ConnectorFailure("test error"))
    monkeypatch.setattr(connector, "connect", connect)
    settings = SnowflakeSettings("ORG-ACCOUNT", "my-user", "WH", "DB", authenticator="externalbrowser")
    with pytest.raises(StorageError):
        open_snowflake(settings)
    assert connect.call_args.kwargs["authenticator"] == "EXTERNALBROWSER"


def test_table_setup_failure_retains_the_authenticated_connection(monkeypatch):
    connector = pytest.importorskip("snowflake.connector")
    connection = Mock()
    connection.cursor.return_value.execute.side_effect = ConnectorFailure("Insufficient privileges to operate on schema")
    monkeypatch.setattr(connector, "connect", Mock(return_value=connection))
    with pytest.raises(SnowflakeSetupError) as failure:
        open_snowflake(SnowflakeSettings("ORG-ACCOUNT", "my-user", "WH", "DB"))
    assert "sign-in succeeded" in str(failure.value)
    assert "lacks permission" in str(failure.value)
    assert failure.value.store.connection is connection
    connection.close.assert_not_called()
    failure.value.store.close()
    connection.close.assert_called_once()


@pytest.mark.parametrize("action", ["retry", "cancel"])
def test_setup_can_be_retried_or_cancelled_without_another_sign_in(tmp_path, monkeypatch, action):
    remote = open_local(tmp_path / "pending.sqlite3")
    remote.kind = "Snowflake"
    connect = Mock(side_effect=SnowflakeSetupError("Sign-in succeeded; table setup needs attention", remote))
    monkeypatch.setattr("clubcal.persistence.open_snowflake", connect)
    app = AppTest.from_file(MAIN, default_timeout=30)
    app.session_state["candidates"] = [Candidate("local", "My event", "Club", "2026-11-19", "18:00")]
    app.run()
    next(field for field in app.text_input if field.label == "Snowflake account identifier").input("ORG-ACCOUNT")
    next(field for field in app.text_input if field.label == "Snowflake username").input("my-user")
    next(button for button in app.button if button.label == "Connect & load Snowflake library").click().run()
    assert not app.exception
    assert app.session_state["library_store"].kind == "Local"
    assert app.session_state["snowflake_pending_setup"]["store"] is remote
    label = "Retry Snowflake table setup" if action == "retry" else "Cancel Snowflake setup"
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception
    assert "snowflake_pending_setup" not in app.session_state
    assert app.session_state["library_store"].kind == ("Snowflake" if action == "retry" else "Local")
    assert app.session_state["local_store"].load_library()[0][0].title == "My event"
    connect.assert_called_once()


def test_resource_creation_is_reviewable_and_requires_confirmation(tmp_path, monkeypatch):
    remote = open_local(tmp_path / "pending-create.sqlite3")
    remote.kind = "Snowflake"
    connect = Mock(side_effect=SnowflakeSetupError("Database missing", remote))
    monkeypatch.setattr("clubcal.persistence.open_snowflake", connect)
    create = Mock()
    monkeypatch.setattr("clubcal.snowflake_auth.initialize_database_schema", create)
    app = AppTest.from_file(MAIN, default_timeout=30).run()
    next(field for field in app.text_input if field.label == "Snowflake account identifier").input("ORG-ACCOUNT")
    next(field for field in app.text_input if field.label == "Snowflake username").input("my-user")
    next(button for button in app.button if button.label == "Connect & load Snowflake library").click().run()
    app.session_state["snowflake_pending_setup"]["resource_checks"] = [{"resource": "Database CLUBCAL", "available": False}]
    app.run()
    assert next(button for button in app.button if button.label == "Create database and schema").disabled
    assert any('CREATE DATABASE IF NOT EXISTS "CLUBCAL"' in block.value for block in app.code)
    create.assert_not_called()
    next(widget for widget in app.checkbox if widget.label == "I approve creating this database and schema in my Snowflake account").check().run()
    next(button for button in app.button if button.label == "Create database and schema").click().run()
    assert not app.exception
    create.assert_called_once()
    assert app.session_state["library_store"].kind == "Snowflake"
    connect.assert_called_once()


def test_ui_normalizes_account_before_connecting_and_preserves_local_data(tmp_path, monkeypatch):
    remote = open_local(tmp_path / "remote.sqlite3")
    remote.kind = "Snowflake"
    connect = Mock(return_value=remote)
    monkeypatch.setattr("clubcal.persistence.open_snowflake", connect)
    app = AppTest.from_file(MAIN, default_timeout=30)
    app.session_state["candidates"] = [Candidate("local", "My event", "Club", "2026-11-19", "18:00")]
    app.run()
    next(field for field in app.text_input if field.label == "Snowflake account identifier").input("PASDZWR.WQ15369")
    next(field for field in app.text_input if field.label == "Snowflake username").input("my-user")
    next(button for button in app.button if button.label == "Connect & load Snowflake library").click().run()
    assert not app.exception
    assert connect.call_args.args[0].account == "PASDZWR-WQ15369"
    assert connect.call_args.args[0].authenticator == "oauth_authorization_code"
    assert app.session_state["library_store"].kind == "Snowflake"
    assert app.session_state["local_store"].load_library()[0][0].title == "My event"


def test_ui_shows_specific_connection_failure_without_losing_local_library(monkeypatch):
    connector = pytest.importorskip("snowflake.connector")
    monkeypatch.setattr(connector, "connect", Mock(side_effect=ConnectorFailure("role CLUBCAL_APP is not granted PRIVATE-TOKEN")))
    app = AppTest.from_file(MAIN, default_timeout=30)
    app.session_state["candidates"] = [Candidate("local", "My event", "Club", "2026-11-19", "18:00")]
    app.run()
    next(field for field in app.text_input if field.label == "Snowflake account identifier").input("ORG-ACCOUNT")
    next(field for field in app.text_input if field.label == "Snowflake username").input("my-user")
    next(button for button in app.button if button.label == "Connect & load Snowflake library").click().run()
    assert not app.exception
    assert any("role is unavailable" in error.value for error in app.error)
    assert all("PRIVATE-TOKEN" not in error.value for error in app.error)
    assert app.session_state["library_store"].kind == "Local"
    assert app.session_state["candidates"][0].title == "My event"
