"""Tests for tools.py — repo parsing, login/logout, middleware, hooks, guidance."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import jwt
import pytest

from github_app_auth.github_auth import AuthenticatedState, GitHubApiError
from github_app_auth.tools import (
    GitHubAppAuthPlugin,
    ToolArgs,
    _parse_repo_input,
)

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

    from github_app_auth.github_auth import AppIdentity

HOST = "github.com"


def _make_identity() -> AppIdentity:
    return {
        "id": 123,
        "slug": "my-app",
        "name": "My App",
        "bot_user_id": 67890,
    }


def _make_plugin(auth: Any = None, host: str = HOST) -> GitHubAppAuthPlugin:
    ctx = cast(
        "PluginContext", SimpleNamespace(get_config=MagicMock(return_value=None))
    )
    return GitHubAppAuthPlugin(ctx, auth, host)


def _future_iso():
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _past_iso():
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


# -- _parse_repo_input ----------------------------------------------------


class TestParseRepoInput:
    def test_simple_slug(self) -> None:
        owner, repo = _parse_repo_input("octocat/Hello-World", HOST)
        assert owner == "octocat"
        assert repo == "Hello-World"

    def test_slug_with_dots(self) -> None:
        owner, repo = _parse_repo_input("octocat/my.repo.name", HOST)
        assert owner == "octocat"
        assert repo == "my.repo.name"

    def test_https_url(self) -> None:
        owner, repo = _parse_repo_input("https://github.com/octocat/Hello-World", HOST)
        assert owner == "octocat"
        assert repo == "Hello-World"

    def test_https_url_with_git_suffix(self) -> None:
        owner, repo = _parse_repo_input(
            "https://github.com/octocat/Hello-World.git", HOST
        )
        assert owner == "octocat"
        assert repo == "Hello-World"

    def test_url_without_scheme(self) -> None:
        owner, repo = _parse_repo_input("github.com/octocat/Hello-World", HOST)
        assert owner == "octocat"
        assert repo == "Hello-World"

    def test_enterprise_url(self) -> None:
        owner, repo = _parse_repo_input(
            "https://git.enterprise.example.com/octocat/Hello-World",
            "git.enterprise.example.com",
        )
        assert owner == "octocat"
        assert repo == "Hello-World"

    def test_wrong_host_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"Cannot login to gitlab.com"):
            _parse_repo_input("https://gitlab.com/octocat/Hello-World", HOST)

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="owner/repo"):
            _parse_repo_input("", HOST)

    def test_missing_repo_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"Cannot login to octocat"):
            _parse_repo_input("octocat/", HOST)

    def test_missing_owner_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"owner/repo"):
            _parse_repo_input("/Hello-World", HOST)

    def test_owner_with_hyphen(self) -> None:
        owner, repo = _parse_repo_input("my-org/Hello-World", HOST)
        assert owner == "my-org"
        assert repo == "Hello-World"

    def test_strips_whitespace(self) -> None:
        owner, repo = _parse_repo_input("  octocat/Hello-World  ", HOST)
        assert owner == "octocat"
        assert repo == "Hello-World"


# -- login ----------------------------------------------------------------


class TestLogin:
    _INSTALLATION_ID = 12345

    def test_not_configured(self) -> None:
        plugin = _make_plugin(auth=None)
        result = json.loads(plugin.login({"repo": "octocat/Hello-World"}))
        assert result["status"] == "error"
        assert "not configured" in result["message"].lower()

    def test_invalid_repo_format(self) -> None:
        auth = MagicMock()
        plugin = _make_plugin(auth=auth)
        result = json.loads(plugin.login({"repo": "invalid"}))
        assert result["status"] == "error"
        assert "Cannot login" in result["message"]

    def test_success(self) -> None:
        auth = MagicMock()
        auth.get_installation_id.return_value = self._INSTALLATION_ID
        expires = _future_iso()
        auth.create_iat.return_value = ("ghs_token", expires)
        plugin = _make_plugin(auth=auth)

        result = json.loads(plugin.login({"repo": "octocat/Hello-World"}))
        assert result["status"] == "authenticated"
        assert result["repo"] == "octocat/Hello-World"
        assert result["installation_id"] == self._INSTALLATION_ID
        assert result["expires_at"] == expires

    def test_github_api_error(self) -> None:
        auth = MagicMock()
        auth.get_installation_id.side_effect = GitHubApiError("Not found")
        plugin = _make_plugin(auth=auth)

        result = json.loads(plugin.login({"repo": "octocat/Hello-World"}))
        assert result["status"] == "error"
        assert "Not found" in result["message"]

    def test_jwt_invalid_key_error(self) -> None:
        auth = MagicMock()
        auth.get_installation_id.side_effect = jwt.InvalidKeyError("bad key")
        plugin = _make_plugin(auth=auth)

        result = json.loads(plugin.login({"repo": "octocat/Hello-World"}))
        assert result["status"] == "error"
        assert "bad key" in result["message"]

    def test_url_input_accepted(self) -> None:
        auth = MagicMock()
        auth.get_installation_id.return_value = self._INSTALLATION_ID
        auth.create_iat.return_value = ("ghs_token", _future_iso())
        plugin = _make_plugin(auth=auth)

        result = json.loads(
            plugin.login({"repo": "https://github.com/octocat/Hello-World"})
        )
        assert result["status"] == "authenticated"
        assert result["repo"] == "octocat/Hello-World"

    def test_sets_auth_state(self) -> None:
        auth = MagicMock()
        auth.get_installation_id.return_value = 12345
        auth.create_iat.return_value = ("ghs_token", _future_iso())
        plugin = _make_plugin(auth=auth)

        plugin.login({"repo": "octocat/Hello-World"})
        state, expired = plugin._get_auth_state_and_expiry()
        assert state is not None
        assert state.repo == "octocat/Hello-World"
        assert expired is False


# -- logout ---------------------------------------------------------------


class TestLogout:
    def test_no_session(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        result = json.loads(plugin.logout({}))
        assert result["status"] == "already_logged_out"

    def test_success(self) -> None:
        auth = MagicMock()
        auth.revoke_iat.return_value = True
        plugin = _make_plugin(auth=auth)
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=_future_iso(),
        )

        result = json.loads(plugin.logout({}))
        assert result["status"] == "logged_out"
        assert result["revoked"] is True

    def test_clears_auth_state(self) -> None:
        auth = MagicMock()
        auth.revoke_iat.return_value = True
        plugin = _make_plugin(auth=auth)
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=_future_iso(),
        )

        plugin.logout({})
        assert plugin._auth_state is None

    def test_api_error_on_revoke(self) -> None:
        auth = MagicMock()
        auth.revoke_iat.side_effect = GitHubApiError("revoke failed")
        plugin = _make_plugin(auth=auth)
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=_future_iso(),
        )

        result = json.loads(plugin.logout({}))
        assert result["status"] == "error"
        assert "revoke failed" in result["message"]


# -- terminal_env_middleware ----------------------------------------------


class TestTerminalEnvMiddleware:
    def test_non_terminal_passthrough(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        result = plugin.terminal_env_middleware(
            "read_file", {"path": "foo.txt"}, {"path": "foo.txt"}
        )
        assert result is None

    def test_non_dict_args_passthrough(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        bad_args = cast("ToolArgs", "not-a-dict")
        result = plugin.terminal_env_middleware("terminal", bad_args, bad_args)
        assert result is None

    def test_prefixes_command_when_authenticated(self) -> None:
        auth = MagicMock()
        plugin = _make_plugin(auth=auth)
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=_future_iso(),
        )

        result = plugin.terminal_env_middleware(
            "terminal", {"command": "git status"}, {"command": "git status"}
        )
        assert result is not None
        assert "GH_TOKEN=ghs_token" in result["args"]["command"]

    def test_empty_token_when_expired(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_old_token",
            repo="octocat/Hello-World",
            expires_at=_past_iso(),
        )

        result = plugin.terminal_env_middleware(
            "terminal", {"command": "git status"}, {"command": "git status"}
        )
        assert result is not None
        assert "ghs_old_token" not in result["args"]["command"]
        assert "GH_TOKEN=" in result["args"]["command"]

    def test_empty_token_when_unauthenticated(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        result = plugin.terminal_env_middleware(
            "terminal", {"command": "git status"}, {"command": "git status"}
        )
        assert result is not None
        assert "GH_TOKEN=" in result["args"]["command"]

    def test_code_field_prefixed(self) -> None:
        auth = MagicMock()
        plugin = _make_plugin(auth=auth)
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=_future_iso(),
        )

        result = plugin.terminal_env_middleware(
            "terminal", {"code": "echo hello"}, {"code": "echo hello"}
        )
        assert result is not None
        assert "GH_TOKEN=ghs_token" in result["args"]["code"]

    def test_empty_command_passthrough(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        result = plugin.terminal_env_middleware(
            "terminal", {"command": ""}, {"command": ""}
        )
        assert result is None

    def test_non_string_command_passthrough(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        result = plugin.terminal_env_middleware(
            "terminal", {"command": 123}, {"command": 123}
        )
        assert result is None


# -- pre_llm_call ---------------------------------------------------------


class TestPreLlmCall:
    def test_no_context_returns_none(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        assert plugin.pre_llm_call(is_first_turn=False) is None

    def test_first_turn_drains_announcement(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        plugin._set_pending_announcement("[GitHub App Auth] Something went wrong")
        result = plugin.pre_llm_call(is_first_turn=True)
        assert result is not None
        assert "Something went wrong" in result["context"]

    def test_announcement_drained_once(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        plugin._set_pending_announcement("test announcement")
        plugin.pre_llm_call(is_first_turn=True)
        result = plugin.pre_llm_call(is_first_turn=False)
        assert result is None

    def test_expired_token_swept(self) -> None:
        auth = MagicMock()
        auth.revoke_iat.return_value = True
        plugin = _make_plugin(auth=auth)
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_old_token",
            repo="octocat/Hello-World",
            expires_at=_past_iso(),
        )

        result = plugin.pre_llm_call(is_first_turn=False)
        assert result is not None
        assert "expired" in result["context"].lower()

    def test_non_expired_token_not_swept(self) -> None:
        auth = MagicMock()
        plugin = _make_plugin(auth=auth)
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=_future_iso(),
        )

        result = plugin.pre_llm_call(is_first_turn=False)
        assert result is None


# -- build_guidance_text ---------------------------------------------------


class TestBuildGuidanceText:
    def test_contains_host(self) -> None:
        plugin = _make_plugin(auth=MagicMock(), host="git.enterprise.example.com")
        text = plugin.build_guidance_text()
        assert "git.enterprise.example.com" in text

    def test_mentions_login(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        text = plugin.build_guidance_text()
        assert "github_app_login" in text

    def test_mentions_logout(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        text = plugin.build_guidance_text()
        assert "github_app_logout" in text

    def test_mentions_other_hosts(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        text = plugin.build_guidance_text()
        assert "other host" in text.lower() or "another host" in text.lower()

    def test_mentions_token_expiry(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        text = plugin.build_guidance_text()
        assert "expired" in text.lower() or "1-hour" in text.lower()


# -- _get_auth_state_and_expiry -------------------------------------------


class TestGetAuthStateAndExpiry:
    def test_no_state(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        state, expired = plugin._get_auth_state_and_expiry()
        assert state is None
        assert expired is True

    def test_valid_state(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_token",
            repo="octocat/Hello-World",
            expires_at=_future_iso(),
        )
        state, expired = plugin._get_auth_state_and_expiry()
        assert state is not None
        assert state.repo == "octocat/Hello-World"
        assert expired is False

    def test_expired_state(self) -> None:
        plugin = _make_plugin(auth=MagicMock())
        plugin._auth_state = AuthenticatedState(
            installation_id=123,
            iat="ghs_old_token",
            repo="octocat/Hello-World",
            expires_at=_past_iso(),
        )
        state, expired = plugin._get_auth_state_and_expiry()
        assert state is not None
        assert expired is True
