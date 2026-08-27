from __future__ import annotations

import json
import logging
import re
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import jwt

from .git_config import GitConfig
from .github_auth import (
    AppIdentity,
    AuthenticatedState,
    GitHubApiError,
    GitHubAppAuth,
)

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

logger = logging.getLogger(__name__)

ToolArgs = dict[str, Any]
JSONSchema = dict[str, Any]

_OWNER_RE = r"[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}"
_REPO_RE = r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?"
_REPO_SLUG_RE = re.compile(rf"^({_OWNER_RE})/({_REPO_RE})$")
_GIT_SUFFIX_RE = re.compile(r"\.git$")
_EXPECTED_URL_PATH_SEGMENTS = 2


def _parse_repo_input(raw: str, host: str) -> tuple[str, str]:
    """Parse owner/repo from a slug or URL on the configured host.

    Raises ValueError with a descriptive message on failure.
    """
    raw = raw.strip()

    match = _REPO_SLUG_RE.match(raw)
    if match:
        return match.group(1), match.group(2)

    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)

    if parsed.hostname and parsed.hostname != host:
        raise ValueError(
            f"Cannot login to {parsed.hostname}: this plugin only "
            f"authenticates repos on {host}."
        )

    if parsed.hostname:
        path = _GIT_SUFFIX_RE.sub("", parsed.path.strip("/"))
        parts = path.split("/", 1)
        if len(parts) == _EXPECTED_URL_PATH_SEGMENTS:
            match = _REPO_SLUG_RE.match(f"{parts[0]}/{parts[1].rstrip('/')}")
            if match:
                return match.group(1), match.group(2)

    raise ValueError(
        f"repo must be in 'owner/repo' format (e.g. 'octocat/Hello-World') "
        f"or a {host} URL"
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


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data)


def _error_result(message: str) -> str:
    return _json_result({"status": "error", "message": message})


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
        self._auth_state: AuthenticatedState | None = None
        self._identity_cache = AppIdentityCache()
        self._lock = threading.Lock()
        self._pending_announcement: str | None = None

    # -- Guidance -----------------------------------------------------

    def build_guidance_text(self, **kwargs: Any) -> str:
        return (
            "[GitHub App Auth] Before any `git` or `gh` operation on a repo "
            f"hosted on {self.host}, call `github_app_login` with the repo in "
            "`owner/repo` format. The plugin scopes credentials transparently "
            "— do not run `gh auth`, `git config`, or similar; they may "
            "corrupt shared config. For repos on other hosts, public reads may "
            "work, but if you cannot access a repo on another host, notify the "
            f"user — you only have GitHub App access to repos on {self.host}. "
            "If a `git`/`gh` command fails with an auth error after a successful "
            "login, either your permissions are intentionally limited or the "
            "token expired (1-hour lifetime) — call `github_app_login` "
            "again. If limited access blocks your task, notify the user. Call "
            "`github_app_logout` when finished to revoke the token."
        )

    # -- Identity resolution ------------------------------------------

    def _should_fetch_identity(self) -> bool:
        return any(
            self.ctx.get_config(key, None) is None
            for key in GitConfig.IDENTITY_CONFIG_KEYS
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

    def _get_auth_state_and_expiry(
        self,
    ) -> tuple[AuthenticatedState | None, bool]:
        with self._lock:
            if self._auth_state is None:
                return None, True
            return self._auth_state, self._auth_state.is_expired()

    def _revoke_and_clear(self, state: AuthenticatedState) -> bool:
        revoked = False
        if self.auth is not None:
            revoked = self.auth.revoke_iat(state.iat)
        with self._lock:
            self._auth_state = None
        return revoked

    def _sweep_expired_token(self) -> str | None:
        with self._lock:
            if self._auth_state is None or not self._auth_state.is_expired():
                return None
            state = self._auth_state
            self._auth_state = None

        if self.auth is not None:
            auth = self.auth

            def _revoke() -> None:
                if not auth.revoke_iat(state.iat):
                    logger.warning("Failed to revoke expired token for %s", state.repo)

            threading.Thread(target=_revoke, daemon=True).start()

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

        try:
            owner, repo_name = _parse_repo_input(args.get("repo", ""), self.host)
        except ValueError as e:
            return _error_result(str(e))

        try:
            installation_id = self.auth.get_installation_id(owner, repo_name)
            token, expires_at = self.auth.create_iat(installation_id)
            state = AuthenticatedState(
                installation_id=installation_id,
                iat=token,
                repo=f"{owner}/{repo_name}",
                expires_at=expires_at,
            )
            with self._lock:
                self._auth_state = state
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
        except (GitHubApiError, jwt.InvalidKeyError) as e:
            logger.exception("github_app_login failed")
            return _error_result(str(e))

    def logout(self, args: ToolArgs, **kwargs: Any) -> str:
        with self._lock:
            state = self._auth_state
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

        state, expired = self._get_auth_state_and_expiry()

        gh_token = ""
        if state is not None and not expired:
            gh_token = state.iat

        git_config = GitConfig.resolve(self.ctx, self.host, self._identity_cache.get())
        env_prefix = git_config.build_env_prefix(gh_token)

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
