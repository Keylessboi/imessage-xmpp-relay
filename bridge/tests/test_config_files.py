"""``*_FILE`` secrets: read from a mounted file, never from an environment variable."""
from __future__ import annotations

import pytest

from relay.config import Config, ConfigError


def _component(**overrides) -> dict:
    values = {
        "XMPP_MODE": "component",
        "COMPONENT_JID": "imessage.example.org",
        "COMPONENT_OWNER": "owner@example.org",
        "XMPP_TARGET": "owner@example.org",
    }
    values.update(overrides)
    return values


def test_component_secret_from_file(tmp_path):
    f = tmp_path / "secret"
    f.write_text("s3cret-from-file\n")
    cfg = Config.from_env(_component(COMPONENT_SECRET_FILE=str(f)))
    assert cfg.component_secret == "s3cret-from-file"


def test_xmpp_password_from_file(tmp_path):
    f = tmp_path / "pw"
    f.write_text("hunter2")
    cfg = Config.from_env(
        {
            "XMPP_JID": "bot@example.org",
            "XMPP_PASSWORD_FILE": str(f),
            "XMPP_TARGET": "pipe@example.org",
        }
    )
    assert cfg.password == "hunter2"


def test_file_and_variable_together_is_an_error(tmp_path):
    f = tmp_path / "secret"
    f.write_text("a")
    with pytest.raises(ConfigError, match="not both"):
        Config.from_env(_component(COMPONENT_SECRET="b", COMPONENT_SECRET_FILE=str(f)))


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(ConfigError, match="not readable"):
        Config.from_env(_component(COMPONENT_SECRET_FILE=str(tmp_path / "nope")))


def test_empty_file_fails_closed(tmp_path):
    f = tmp_path / "empty"
    f.write_text("\n")
    with pytest.raises(ConfigError, match="is empty"):
        Config.from_env(_component(COMPONENT_SECRET_FILE=str(f)))


def test_error_never_contains_the_secret(tmp_path):
    f = tmp_path / "secret"
    f.write_bytes(b"\xff\xfe-not-utf8-hunter2")
    with pytest.raises(ConfigError) as info:
        Config.from_env(_component(COMPONENT_SECRET_FILE=str(f)))
    assert "hunter2" not in str(info.value)
