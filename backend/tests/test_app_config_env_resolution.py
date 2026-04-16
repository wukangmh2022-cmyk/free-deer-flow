from __future__ import annotations

import pytest

from deerflow.config.app_config import AppConfig


def test_resolve_env_variables_required_dollar_var(monkeypatch):
    monkeypatch.setenv("TEST_REQUIRED_VAR", "ok")
    assert AppConfig.resolve_env_variables("$TEST_REQUIRED_VAR") == "ok"


def test_resolve_env_variables_required_braced_var(monkeypatch):
    monkeypatch.setenv("TEST_REQUIRED_BRACED", "braced")
    assert AppConfig.resolve_env_variables("${TEST_REQUIRED_BRACED}") == "braced"


def test_resolve_env_variables_braced_default_when_missing(monkeypatch):
    monkeypatch.delenv("TEST_MISSING_WITH_DEFAULT", raising=False)
    assert AppConfig.resolve_env_variables("${TEST_MISSING_WITH_DEFAULT:-~/Downloads}") == "~/Downloads"


def test_resolve_env_variables_braced_default_can_chain_env(monkeypatch):
    monkeypatch.delenv("TEST_PRIMARY_ENV", raising=False)
    monkeypatch.setenv("TEST_FALLBACK_ENV", "/tmp/fallback")
    assert AppConfig.resolve_env_variables("${TEST_PRIMARY_ENV:-$TEST_FALLBACK_ENV}") == "/tmp/fallback"


def test_resolve_env_variables_required_missing_raises(monkeypatch):
    monkeypatch.delenv("TEST_REQUIRED_MISSING", raising=False)
    with pytest.raises(ValueError, match="TEST_REQUIRED_MISSING"):
        AppConfig.resolve_env_variables("${TEST_REQUIRED_MISSING}")


def test_resolve_env_variables_invalid_expression_raises():
    with pytest.raises(ValueError):
        AppConfig.resolve_env_variables("${1BAD_NAME}")
