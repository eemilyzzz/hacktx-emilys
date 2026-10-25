from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from streamlit.testing.v1 import AppTest

from clubcal.ingest import check_apify_connection, fetch_apify


def test_connection_check_is_read_only_and_uses_header_auth(monkeypatch):
    get = Mock(return_value=Mock(ok=True))
    post = Mock(side_effect=AssertionError("Connection check cannot start a run"))
    monkeypatch.setattr("clubcal.ingest.requests.get", get)
    monkeypatch.setattr("clubcal.ingest.requests.post", post)
    check_apify_connection("test-secret")
    assert get.call_args.args[0] == "https://api.apify.com/v2/users/me"
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer test-secret"
    post.assert_not_called()


@pytest.mark.parametrize("status,message", [(401, "rejected the token"), (403, "permissions"), (402, "balance"), (429, "request limit")])
def test_api_errors_distinguish_auth_permissions_and_balance(monkeypatch, status, message):
    response = Mock(ok=False, status_code=status)
    response.json.return_value = {"error": {"type": "test-error", "message": "test-secret must not be echoed"}}
    monkeypatch.setattr("clubcal.ingest.requests.get", Mock(return_value=response))
    with pytest.raises(ValueError) as error:
        check_apify_connection("test-secret")
    assert message in str(error.value)
    assert "test-secret" not in str(error.value)


@pytest.mark.parametrize("exc,message", [
    (requests.ConnectionError("[WinError 10013] forbidden test-secret"), "blocked from connecting"),
    (requests.ConnectionError("DNS failure test-secret"), "network access"),
    (requests.exceptions.SSLError("bad certificate test-secret"), "certificate"),
    (requests.ReadTimeout("test-secret"), "timed out"),
])
def test_network_errors_are_helpful_without_echoing_token(monkeypatch, exc, message):
    monkeypatch.setattr("clubcal.ingest.requests.get", Mock(side_effect=exc))
    with pytest.raises(ValueError) as error:
        check_apify_connection("test-secret")
    assert message in str(error.value)
    assert "test-secret" not in str(error.value)


def test_failed_run_creation_is_not_retried(monkeypatch):
    post = Mock(side_effect=requests.ReadTimeout("secret"))
    monkeypatch.setattr("clubcal.ingest.requests.post", post)
    with pytest.raises(ValueError, match="run may already exist"):
        fetch_apify(["swetexas"], "secret", 5)
    assert post.call_count == 1


def test_connection_button_shows_success_without_starting_scraper(monkeypatch):
    monkeypatch.setattr("clubcal.ingest.requests.get", Mock(return_value=Mock(ok=True)))
    post = Mock(side_effect=AssertionError("Must not create a run"))
    monkeypatch.setattr("clubcal.ingest.requests.post", post)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "main.py").run()
    next(field for field in app.text_input if field.label == "Apify token").input("test-secret")
    next(button for button in app.button if button.label == "Test Apify connection").click().run()
    assert not app.exception
    assert any("Token accepted" in message.value for message in app.success)
    post.assert_not_called()
