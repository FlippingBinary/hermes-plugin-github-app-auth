"""Tests for github_auth.py — token expiry, host normalization, client cleanup."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from github_app_auth.github_auth import (
    AuthenticatedState,
    GitHubApiError,
    GitHubAppAuth,
    GitHubHostConfig,
    _normalize_hostname,
    resolve_host,
)


class TestAuthenticatedStateIsExpired:
    def test_future_expiry_not_expired(self) -> None:
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=future,
        )
        assert state.is_expired() is False

    def test_past_expiry_expired(self) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=past,
        )
        assert state.is_expired() is True

    def test_z_suffix_handled(self) -> None:
        future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=future,
        )
        assert state.is_expired() is False

    def test_malformed_expiry_expired(self) -> None:
        state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at="not-a-date",
        )
        assert state.is_expired() is True

    def test_empty_expiry_expired(self) -> None:
        state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at="",
        )
        assert state.is_expired() is True


class TestNormalizeHostname:
    def test_none_returns_none(self) -> None:
        assert _normalize_hostname(None) is None

    def test_empty_returns_none(self) -> None:
        assert _normalize_hostname("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _normalize_hostname("   ") is None

    def test_bare_hostname(self) -> None:
        assert _normalize_hostname("github.com") == "github.com"

    def test_uppercase_lowered(self) -> None:
        assert _normalize_hostname("GitHub.COM") == "github.com"

    def test_with_scheme(self) -> None:
        assert _normalize_hostname("https://github.com") == "github.com"

    def test_trailing_dot_stripped(self) -> None:
        assert _normalize_hostname("github.com.") == "github.com"

    def test_enterprise_host(self) -> None:
        assert _normalize_hostname("git.enterprise.example.com") == (
            "git.enterprise.example.com"
        )


class TestResolveHost:
    def test_none_defaults_to_github(self) -> None:
        config = resolve_host(None)
        assert config.hostname == "github.com"
        assert config.api_base == "https://api.github.com"

    def test_github_com_uses_known_api(self) -> None:
        config = resolve_host("github.com")
        assert config.hostname == "github.com"
        assert config.api_base == "https://api.github.com"

    def test_enterprise_host_probes_api(self) -> None:
        config = resolve_host("git.enterprise.example.com")
        assert config.hostname == "git.enterprise.example.com"
        assert config.api_base is None

    def test_invalid_host_falls_back_to_github(self) -> None:
        config = resolve_host("")
        assert config.hostname == "github.com"
        assert config.api_base == "https://api.github.com"


class TestGitHubHostConfig:
    def test_frozen_dataclass(self) -> None:
        config = GitHubHostConfig("github.com", "https://api.github.com")
        with pytest.raises(AttributeError):
            config.hostname = "other.com"  # type: ignore[misc]


class TestGitHubAppAuthClose:
    def test_close_calls_client_close(self) -> None:
        host_config = GitHubHostConfig("github.com", "https://api.github.com")
        auth = GitHubAppAuth("client_id", "fake-key", host_config)
        auth._client = MagicMock()
        auth.close()
        auth._client.close.assert_called_once()


class TestRevokeIat:
    def test_returns_false_on_api_base_resolution_failure(self) -> None:
        host_config = GitHubHostConfig("bad.invalid", "https://api.bad.invalid")
        auth = GitHubAppAuth("client_id", "fake-key", host_config)
        with patch.object(
            auth, "_resolve_api_base", side_effect=GitHubApiError("nope")
        ):
            result = auth.revoke_iat("ghs_token")
        assert result is False
