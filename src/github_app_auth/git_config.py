from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

    from .github_auth import AppIdentity


@dataclass(frozen=True)
class GitConfig:
    """Resolved git identity and configuration for authenticated git operations.

    Holds the four identity fields (author/committer name/email) plus the
    GitHub host, and produces the shell environment prefix that scopes
    ``git``/``gh`` commands to the GitHub App installation token.
    """

    host: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str

    IDENTITY_CONFIG_KEYS: ClassVar[tuple[str, ...]] = (
        "author_name",
        "author_email",
        "committer_name",
        "committer_email",
    )

    @staticmethod
    def _build_noreply_email(app: AppIdentity) -> str:
        return f"{app['bot_user_id']}+{app['slug']}[bot]@users.noreply.github.com"

    @classmethod
    def resolve(
        cls,
        ctx: PluginContext,
        host: str,
        identity: AppIdentity | None,
    ) -> GitConfig:
        """Build a GitConfig from plugin config, falling back to App identity."""
        name_fallback = identity["name"] if identity is not None else None
        email_fallback = (
            cls._build_noreply_email(identity) if identity is not None else None
        )

        def resolve_key(key: str, fallback: str | None) -> str:
            configured = ctx.get_config(key, None)
            if configured is not None:
                return configured
            if fallback is not None:
                return fallback
            return ""

        return cls(
            host=host,
            author_name=resolve_key("author_name", name_fallback),
            author_email=resolve_key("author_email", email_fallback),
            committer_name=resolve_key("committer_name", name_fallback),
            committer_email=resolve_key("committer_email", email_fallback),
        )

    def build_env_prefix(self, gh_token: str) -> str:
        """Build the shell ``export ...; `` prefix for git env vars."""
        basic_auth = base64.b64encode(f"x-access-token:{gh_token}".encode()).decode()

        config_pairs: list[tuple[str, str]] = [
            (f"url.https://{self.host}/.insteadOf", f"git@{self.host}:"),
            (f"url.https://{self.host}/.insteadOf", f"ssh://git@{self.host}/"),
            (
                f"http.https://{self.host}/.extraHeader",
                f"Authorization: Basic {basic_auth}",
            ),
        ]

        env_parts = [
            f"GH_TOKEN={shlex.quote(gh_token)}",
            "GIT_CONFIG_GLOBAL=/dev/null",
            f"GIT_AUTHOR_NAME={shlex.quote(self.author_name)}",
            f"GIT_AUTHOR_EMAIL={shlex.quote(self.author_email)}",
            f"GIT_COMMITTER_NAME={shlex.quote(self.committer_name)}",
            f"GIT_COMMITTER_EMAIL={shlex.quote(self.committer_email)}",
            f"GIT_CONFIG_COUNT={len(config_pairs)}",
        ]
        for i, (key, value) in enumerate(config_pairs):
            env_parts.append(f"GIT_CONFIG_KEY_{i}={shlex.quote(key)}")
            env_parts.append(f"GIT_CONFIG_VALUE_{i}={shlex.quote(value)}")

        return f"export {' '.join(env_parts)}; "
