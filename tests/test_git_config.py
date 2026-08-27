"""Tests for git_config.py — env prefix construction and identity resolution."""

from __future__ import annotations

import base64
import shlex
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

from github_app_auth.git_config import GitConfig

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

    from github_app_auth.github_auth import AppIdentity


def _make_ctx(get_config: Any = None) -> PluginContext:
    if get_config is None:
        get_config = MagicMock(return_value=None)
    return cast("PluginContext", SimpleNamespace(get_config=get_config))


class TestBuildNoreplyEmail:
    def test_format(self) -> None:
        identity: AppIdentity = {
            "id": 123,
            "slug": "my-app",
            "name": "My App",
            "bot_user_id": 67890,
        }
        email = GitConfig._build_noreply_email(identity)
        assert email == "67890+my-app[bot]@users.noreply.github.com"


class TestResolve:
    def test_uses_config_when_set(self) -> None:
        ctx = _make_ctx(MagicMock(return_value="Custom Name"))
        identity: AppIdentity = {
            "id": 123,
            "slug": "my-app",
            "name": "App Name",
            "bot_user_id": 67890,
        }
        config = GitConfig.resolve(ctx, "github.com", identity)
        assert config.author_name == "Custom Name"
        assert config.committer_name == "Custom Name"

    def test_falls_back_to_identity(self) -> None:
        ctx = _make_ctx()
        identity: AppIdentity = {
            "id": 123,
            "slug": "my-app",
            "name": "App Name",
            "bot_user_id": 67890,
        }
        config = GitConfig.resolve(ctx, "github.com", identity)
        assert config.author_name == "App Name"
        assert config.committer_name == "App Name"
        assert config.author_email == "67890+my-app[bot]@users.noreply.github.com"
        assert config.committer_email == "67890+my-app[bot]@users.noreply.github.com"

    def test_empty_when_no_identity_and_no_config(self) -> None:
        ctx = _make_ctx()
        config = GitConfig.resolve(ctx, "github.com", None)
        assert config.author_name == ""
        assert config.author_email == ""
        assert config.committer_name == ""
        assert config.committer_email == ""

    def test_partial_config_override(self) -> None:
        def mock_get_config(key: str, default=None):
            values = {"author_name": "Override"}
            return values.get(key, default)

        ctx = _make_ctx(mock_get_config)
        identity: AppIdentity = {
            "id": 123,
            "slug": "my-app",
            "name": "App Name",
            "bot_user_id": 67890,
        }
        config = GitConfig.resolve(ctx, "github.com", identity)
        assert config.author_name == "Override"
        assert config.committer_name == "App Name"
        assert config.author_email == "67890+my-app[bot]@users.noreply.github.com"


class TestBuildEnvPrefix:
    def _make_config(self) -> GitConfig:
        return GitConfig(
            host="github.com",
            author_name="Test App",
            author_email="123+test[bot]@users.noreply.github.com",
            committer_name="Test App",
            committer_email="123+test[bot]@users.noreply.github.com",
        )

    def test_contains_gh_token(self) -> None:
        prefix = self._make_config().build_env_prefix("ghs_token123")
        assert "GH_TOKEN=ghs_token123" in prefix

    def test_gh_token_quoted(self) -> None:
        prefix = self._make_config().build_env_prefix("ghs_token")
        assert shlex.quote("ghs_token") in prefix

    def test_empty_token_when_unauthenticated(self) -> None:
        prefix = self._make_config().build_env_prefix("")
        assert "GH_TOKEN=" in prefix

    def test_git_config_global_disabled(self) -> None:
        prefix = self._make_config().build_env_prefix("ghs_token")
        assert "GIT_CONFIG_GLOBAL=/dev/null" in prefix

    def test_author_and_committer_env_vars(self) -> None:
        prefix = self._make_config().build_env_prefix("ghs_token")
        assert "GIT_AUTHOR_NAME=" in prefix
        assert "GIT_AUTHOR_EMAIL=" in prefix
        assert "GIT_COMMITTER_NAME=" in prefix
        assert "GIT_COMMITTER_EMAIL=" in prefix

    def test_git_config_count(self) -> None:
        prefix = self._make_config().build_env_prefix("ghs_token")
        assert "GIT_CONFIG_COUNT=3" in prefix

    def test_ssh_to_https_rewrites(self) -> None:
        prefix = self._make_config().build_env_prefix("ghs_token")
        assert "url.https://github.com/.insteadOf" in prefix
        assert "git@github.com:" in prefix
        assert "ssh://git@github.com/" in prefix

    def test_basic_auth_header(self) -> None:
        token = "ghs_token123"
        prefix = self._make_config().build_env_prefix(token)
        expected_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        assert f"Authorization: Basic {expected_b64}" in prefix

    def test_starts_with_export_and_ends_with_semicolon(self) -> None:
        prefix = self._make_config().build_env_prefix("ghs_token")
        assert prefix.startswith("export ")
        assert prefix.endswith("; ")

    def test_enterprise_host_in_urls(self) -> None:
        config = GitConfig(
            host="git.enterprise.example.com",
            author_name="App",
            author_email="123+app[bot]@users.noreply.github.com",
            committer_name="App",
            committer_email="123+app[bot]@users.noreply.github.com",
        )
        prefix = config.build_env_prefix("ghs_token")
        assert "url.https://git.enterprise.example.com/.insteadOf" in prefix
        assert "git@git.enterprise.example.com:" in prefix


class TestIdentityConfigKeys:
    def test_contains_all_four_keys(self) -> None:
        assert set(GitConfig.IDENTITY_CONFIG_KEYS) == {
            "author_name",
            "author_email",
            "committer_name",
            "committer_email",
        }
