import pytest


@pytest.fixture(autouse=True)
def isolated_library(tmp_path, monkeypatch):
    """App tests must never read or write the user's actual saved event library."""
    monkeypatch.setenv("CLUBCAL_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("CLUBCAL_SECRETS_PATH", str(tmp_path / "no-secrets.toml"))
    for name in ("ACCOUNT", "USER", "WAREHOUSE", "DATABASE", "SCHEMA", "ROLE", "WORKSPACE", "AUTHENTICATOR", "PRIVATE_KEY_FILE", "PRIVATE_KEY_FILE_PWD"):
        monkeypatch.delenv("SNOWFLAKE_" + name, raising=False)
