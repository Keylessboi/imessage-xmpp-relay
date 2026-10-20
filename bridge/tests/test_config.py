"""Environment parsing, with an explicit eye on secrets and the empty allowlist."""
from __future__ import annotations

import logging

import pytest

from relay.config import Config, ConfigError, parse_contacts


def _env(**overrides) -> dict:
    env = {
        "XMPP_JID": "bot@example.org",
        "XMPP_PASSWORD": "hunter2",
        "XMPP_TARGET": "pipe@example.org",
    }
    env.update(overrides)
    return env


def test_defaults():
    cfg = Config.from_env(_env())
    assert cfg.api_url == "http://imessage-server:8790"
    assert cfg.port == 5222
    assert cfg.targets == ("pipe@example.org",)
    assert cfg.allowed == frozenset()
    assert cfg.server is None


def test_api_url_and_lists_are_parsed():
    cfg = Config.from_env(
        _env(
            IMESSAGE_API_URL="http://127.0.0.1:9000/",
            XMPP_TARGET="a@example.org, b@example.org c@example.org",
            XMPP_ALLOWED="Alice@Example.org, bob@example.org",
        )
    )
    assert cfg.api_url == "http://127.0.0.1:9000"
    assert cfg.targets == ("a@example.org", "b@example.org", "c@example.org")
    assert cfg.allowed == frozenset({"alice@example.org", "bob@example.org"})


def test_missing_required_variables_raise():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({"XMPP_TARGET": "pipe@example.org"})
    assert "XMPP_JID" in str(excinfo.value)
    assert "XMPP_PASSWORD" in str(excinfo.value)


def test_no_destination_at_all_is_an_error():
    with pytest.raises(ConfigError):
        Config.from_env(_env(XMPP_TARGET=""))


def test_empty_allowlist_warns_loudly(caplog):
    with caplog.at_level(logging.WARNING, logger="relay.config"):
        cfg = Config.from_env(_env())
    assert cfg.allowed == frozenset()
    assert any("DENY EVERY" in record.getMessage() for record in caplog.records)


def test_server_with_port_is_split():
    cfg = Config.from_env(_env(XMPP_SERVER="xmpp.example.org:5223"))
    assert cfg.server == "xmpp.example.org"
    assert cfg.port == 5223


def test_contacts_parse_and_reverse_lookup():
    cfg = Config.from_env(
        _env(
            XMPP_TARGET="",
            XMPP_CONTACTS="+1 (555) 123-4567=Alice@Example.org, me@example.org=+15550001111",
        )
    )
    assert cfg.contacts["+15551234567"] == "alice@example.org"
    assert cfg.handle_for_jid("alice@example.org/phone") == "+15551234567"
    assert cfg.handle_for_jid("nobody@example.org") is None


def test_bad_contacts_entry_raises():
    with pytest.raises(ConfigError):
        parse_contacts("not-a-pair")
    with pytest.raises(ConfigError):
        Config.from_env(_env(XMPP_TARGET="", XMPP_CONTACTS="handle="))


def test_password_never_leaks_into_logs_or_repr():
    cfg = Config.from_env(_env(XMPP_PASSWORD="hunter2"))
    assert "hunter2" not in cfg.describe()
    assert "hunter2" not in repr(cfg)
    assert "password" not in cfg.describe()
