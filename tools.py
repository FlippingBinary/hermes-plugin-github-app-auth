from __future__ import annotations

import base64
import json
import logging
import shlex
import threading
from typing import TYPE_CHECKING, Any, Protocol, overload

from .github_auth import (
    AppIdentity,
    AuthenticatedState,
    AuthState,
    GitHubApiError,
    GitHubAppAuth,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

ToolArgs = dict[str, Any]
JSONSchema = dict[str, Any]

_IDENTITY_CONFIG_KEYS = (
    "author_name",
    "author_email",
    "committer_name",
    "committer_email",
)


class AppIdentityCache:
    """Thread-safe cache for the GitHub App identity."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._identity: AppIdentity | None = None
        self._attempted: bool = False

    @property
    def is_resolved(self) -> bool:
        with self._lock:
            return self._identity is not None

    @property
    def was_attempted(self) -> bool:
        with self._lock:
            return self._attempted

    def get(self) -> AppIdentity | None:
        with self._lock:
            return self._identity

    def set_success(self, identity: AppIdentity) -> None:
        with self._lock:
            self._identity = identity
            self._attempted = True

    def set_failure(self) -> None:
        with self._lock:
            self._attempted = True


class PluginContext(Protocol):
    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: JSONSchema,
        handler: Callable[[ToolArgs], str],
        *,
        override: bool = False,
        check_fn: Callable[..., bool] | None = None,
    ) -> None: ...

    def register_hook(
        self,
        event_name: str,
        callback: Callable[..., Any],
    ) -> None: ...

    def register_middleware(
        self,
        kind: str,
        callback: Callable[..., Any],
    ) -> None: ...

    def register_system_prompt_section(
        self,
        section_id: str,
        content: str | Callable[..., str],
        *,
        position: str = "after_memory",
        max_chars: int = 4000,
    ) -> None: ...

    @overload
    def get_config(self, key: str) -> Any: ...

    @overload
    def get_config(self, key: str, default: Any) -> Any: ...

    def get_config(self, key: str, default: Any = ...) -> Any: ...


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data)


def _error_result(message: str) -> str:
    return _json_result({"status": "error", "message": message})


def _build_noreply_email(app: AppIdentity) -> str:
    return f"{app['bot_user_id']}+{app['slug']}[bot]@users.noreply.github.com"


def _build_git_env_prefix(
    host: str,
    gh_token: str,
    author_name: str,
    author_email: str,
    committer_name: str,
    committer_email: str,
) -> str:
    basic_auth = base64.b64encode(f"x-access-token:{gh_token}".encode()).decode()

    config_pairs: list[tuple[str, str]] = [
        (f"url.https://{host}/.insteadOf", f"git@{host}:"),
        (f"url.https://{host}/.insteadOf", f"ssh://git@{host}/"),
        (
            f"http.https://{host}/.extraHeader",
            f"Authorization: Basic {basic_auth}",
        ),
    ]

    env_parts = [
        f"GH_TOKEN={shlex.quote(gh_token)}",
        "GIT_CONFIG_GLOBAL=/dev/null",
        f"GIT_AUTHOR_NAME={shlex.quote(author_name)}",
        f"GIT_AUTHOR_EMAIL={shlex.quote(author_email)}",
        f"GIT_COMMITTER_NAME={shlex.quote(committer_name)}",
        f"GIT_COMMITTER_EMAIL={shlex.quote(committer_email)}",
        f"GIT_CONFIG_COUNT={len(config_pairs)}",
    ]
    for i, (key, value) in enumerate(config_pairs):
        env_parts.append(f"GIT_CONFIG_KEY_{i}={shlex.quote(key)}")
        env_parts.append(f"GIT_CONFIG_VALUE_{i}={shlex.quote(value)}")

    return f"export {' '.join(env_parts)}; "


class GitHubAppAuthPlugin:
    """GitHub App authentication plugin for Hermes Agent.

    Holds all plugin state (auth, identity cache, host config) as instance
    attributes. Handlers, hooks, and middleware are bound methods.
    """

    def __init__(
        self,
        ctx: PluginContext,
        auth: GitHubAppAuth | None,
        host: str,
    ) -> None:
        self.ctx = ctx
        self.auth = auth
        self.host = host
        self._auth_state = AuthState()
        self._identity_cache = AppIdentityCache()
        self._lock = threading.Lock()
        self._pending_announcement: str | None = None

    # -- Guidance -----------------------------------------------------

    def build_guidance_text(self, **kwargs: Any) -> str:
        return (
            "[GitHub App Auth] Before reading or writing any repo that is "
            f"hosted on {self.host}, you MUST login to the repo as a GitHub "
            "App using the github_app_login tool. This tool will configure "
            "your terminal so that all `git` and `gh` commands for that repo "
            "are authenticated and configured correctly. The plugin handles "
            "credentials transparently. Do not run `gh auth ...`, "
            "`git config ...`, or similar commands; they are unnecessary "
            "and may corrupt configuration files, negatively affecting other "
            "users. For repos hosted on any other host, github_app_login "
            "does not apply. Public reads may succeed without authentication, "
            "but private repos and write operations will not be accessible. "
            "If you need access to a private repo on another host, notify "
            "the user. Do NOT attempt any other method of logging in or "
            f"accessing any repo on {self.host} other than the "
            "github_app_login tool. If you have already logged in to the "
            "repo successfully with the github_app_login tool, but a `git` "
            "or `gh` command fails with an authentication error anyway, "
            "that means your access to the repo for that type of operation "
            "is intentionally limited or your access token expired and "
            "you'll need to call github_app_login again (it expires an hour "
            "after you last called the tool). If your limited access is a "
            "blocking issue, you MUST notify the user so they can choose "
            "whether to grant additional access or not. If you accidentally "
            "leak a credential or are simply finished with your `git`/`gh` "
            "operations for the time-being, you can logout by using the "
            "github_app_logout tool to revoke your transparent credential's "
            "access to the repo. That limits the damage that could be caused "
            "if someone else intercepted it and tries to use it."
        )

    # -- Identity resolution ------------------------------------------

    def _should_fetch_identity(self) -> bool:
        return any(
            self.ctx.get_config(key, None) is None for key in _IDENTITY_CONFIG_KEYS
        )

    def _resolve_git_identity(self) -> tuple[str, str, str, str]:
        identity = self._identity_cache.get()
        name_fallback = identity["name"] if identity is not None else None
        email_fallback = (
            _build_noreply_email(identity) if identity is not None else None
        )

        def resolve(key: str, fallback: str | None) -> str:
            configured = self.ctx.get_config(key, None)
            if configured is not None:
                return configured
            if fallback is not None:
                return fallback
            return ""

        return (
            resolve("author_name", name_fallback),
            resolve("author_email", email_fallback),
            resolve("committer_name", name_fallback),
            resolve("committer_email", email_fallback),
        )

    def _build_failure_announcement(self, *, network: bool, error: str) -> str:
        if network:
            description = "A network error occurred"
            remedy = f"Suggest that the user check network connectivity to {self.host}."
        else:
            description = "An authentication error occurred"
            remedy = (
                "Suggest that the user check the github-app-auth plugin's "
                "configuration and environment variables."
            )
        return (
            f"[GitHub App Auth] {description} while setting up the "
            f"github-app-auth toolgroup: {error} Please announce this "
            f"failure to the user now, before taking any other action. "
            f"{remedy} Do not attempt to fix this yourself. Do not create "
            f"git commits until you have successfully called "
            f"github_app_login."
        )

    def _set_pending_announcement(self, msg: str) -> None:
        with self._lock:
            self._pending_announcement = msg

    def _drain_pending_announcement(self) -> str | None:
        with self._lock:
            msg = self._pending_announcement
            self._pending_announcement = None
            return msg

    def _ensure_identity_if_needed(self) -> None:
        if self._identity_cache.is_resolved:
            return
        if self._identity_cache.was_attempted:
            return
        if not self._should_fetch_identity():
            return
        self._attempt_identity_fetch()

    def _attempt_identity_fetch(self) -> None:
        if self.auth is None:
            self._identity_cache.set_failure()
            self._set_pending_announcement(
                self._build_failure_announcement(
                    network=False,
                    error=(
                        "GITHUB_APP_CLIENT_ID and GITHUB_APP_PRIVATE_KEY "
                        "environment variables must be set for "
                        "github-app-auth to function."
                    ),
                )
            )
            return

        try:
            identity = self.auth.get_app()
            self._identity_cache.set_success(identity)
        except GitHubApiError as e:
            self._identity_cache.set_failure()
            self._set_pending_announcement(
                self._build_failure_announcement(network=e.network, error=str(e))
            )

    # -- Token lifecycle ----------------------------------------------

    def _revoke_and_clear(self, state: AuthenticatedState) -> bool:
        revoked = False
        if self.auth is not None:
            revoked = self.auth.revoke_iat(state.iat)
        self._auth_state.clear()
        return revoked

    def _sweep_expired_token(self) -> str | None:
        if not (
            self._auth_state.is_authenticated and self._auth_state.is_token_expired()
        ):
            return None
        state = self._auth_state.get_state()
        if state is None:
            return None
        self._revoke_and_clear(state)
        return (
            f"[GitHub App Auth] Token for {state.repo} has expired. Use "
            f"github_app_login again to re-authenticate before attempting "
            f"any `git` or `gh` operations on its {self.host} remote."
        )

    # -- Tool handlers ------------------------------------------------

    def login(self, args: ToolArgs, **kwargs: Any) -> str:
        if self.auth is None:
            return _error_result(
                "Plugin not configured: GITHUB_APP_CLIENT_ID and "
                "GITHUB_APP_PRIVATE_KEY environment variables must "
                "be set by the user."
            )

        owner, _, repo_name = args.get("repo", "").partition("/")
        owner, repo_name = owner.strip(), repo_name.strip()
        if not owner or not repo_name:
            return _error_result("repo must be in 'owner/repo' format")

        try:
            installation_id = self.auth.get_installation_id(owner, repo_name)
            token, expires_at = self.auth.create_iat(installation_id)
            self._auth_state.set(
                AuthenticatedState(
                    installation_id=installation_id,
                    iat=token,
                    repo=f"{owner}/{repo_name}",
                    expires_at=expires_at,
                )
            )
            if self._should_fetch_identity():
                self._attempt_identity_fetch()
            return _json_result(
                {
                    "status": "authenticated",
                    "repo": f"{owner}/{repo_name}",
                    "installation_id": installation_id,
                    "expires_at": expires_at,
                }
            )
        except GitHubApiError as e:
            logger.exception("github_app_login failed")
            return _error_result(str(e))

    def logout(self, args: ToolArgs, **kwargs: Any) -> str:
        state = self._auth_state.get_state()
        if state is None:
            return _json_result({"status": "already_logged_out"})
        try:
            revoked = self._revoke_and_clear(state)
        except GitHubApiError as e:
            logger.exception("github_app_logout failed")
            return _error_result(str(e))
        return _json_result({"status": "logged_out", "revoked": revoked})

    # -- Hooks ---------------------------------------------------------

    def on_session_start(self, **kwargs: Any) -> None:
        self._ensure_identity_if_needed()

    def pre_llm_call(
        self,
        *,
        is_first_turn: bool = False,
        **kwargs: Any,
    ) -> dict[str, str] | None:
        contexts: list[str] = []

        if is_first_turn:
            self._ensure_identity_if_needed()
            announcement = self._drain_pending_announcement()
            if announcement:
                contexts.append(announcement)

        expired_msg = self._sweep_expired_token()
        if expired_msg:
            contexts.append(expired_msg)

        if contexts:
            return {"context": "\n\n".join(contexts)}
        return None

    # -- Middleware ----------------------------------------------------

    def terminal_env_middleware(
        self,
        tool_name: str,
        args: ToolArgs,
        original_args: ToolArgs,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        if tool_name != "terminal":
            return None

        if not isinstance(args, dict):
            return None

        state = self._auth_state.get_state()
        expired = self._auth_state.is_token_expired()

        gh_token = ""
        if state is not None and not expired:
            gh_token = state.iat

        author_name, author_email, committer_name, committer_email = (
            self._resolve_git_identity()
        )

        env_prefix = _build_git_env_prefix(
            self.host,
            gh_token,
            author_name,
            author_email,
            committer_name,
            committer_email,
        )

        modified = dict(args)
        modified_any = False

        for field in ("command", "code"):
            value = modified.get(field, "")
            if isinstance(value, str) and value.strip():
                modified[field] = env_prefix + value
                modified_any = True

        if not modified_any:
            return None

        return {"args": modified}
