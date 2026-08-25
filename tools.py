from __future__ import annotations

import base64
import enum
import json
import logging
import shlex
import threading
from typing import TYPE_CHECKING, Any, Protocol, overload

import httpx
import jwt

from .github_auth import AppIdentity, AuthState, GitHubAppAuth

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

ToolArgs = dict[str, Any]
JSONSchema = dict[str, Any]


class _FetchOutcome(enum.Enum):
    SUCCESS = "success"
    NETWORK = "network"
    AUTH = "auth"


class AppIdentityCache:
    """Thread-safe cache for the GitHub App identity fetched from GET /app."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._identity: AppIdentity | None = None
        self._outcome: _FetchOutcome | None = None
        self._error: str | None = None
        self._attempted: bool = False
        self._pending_injection: bool = False

    @property
    def is_resolved(self) -> bool:
        with self._lock:
            return self._identity is not None

    @property
    def was_attempted(self) -> bool:
        with self._lock:
            return self._attempted

    def has_pending_injection(self) -> bool:
        with self._lock:
            return self._pending_injection

    def consume_pending_injection(self) -> tuple[_FetchOutcome, str] | None:
        with self._lock:
            if not self._pending_injection:
                return None
            self._pending_injection = False
            if self._outcome is None or self._error is None:
                raise RuntimeError(
                    "consume_pending_injection called with no pending failure state"
                )
            return self._outcome, self._error

    def get(self) -> AppIdentity | None:
        with self._lock:
            return self._identity

    def set_success(self, identity: AppIdentity) -> None:
        with self._lock:
            self._identity = identity
            self._outcome = _FetchOutcome.SUCCESS
            self._error = None
            self._attempted = True
            self._pending_injection = False

    def set_failure(self, outcome: _FetchOutcome, error: str) -> None:
        with self._lock:
            self._outcome = outcome
            self._error = error
            self._attempted = True
            self._pending_injection = True


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


_auth_state = AuthState()
_app_identity_cache = AppIdentityCache()
_auth: GitHubAppAuth | None = None
_ctx: PluginContext | None = None
_github_host: str = "github.com"


def _build_guidance_text(session_info: Any = None, **kwargs: Any) -> str:
    return (
        "[GitHub App Auth] Before reading or writing any repo that is hosted on "
        f"{_github_host}, you MUST login to the repo as a GitHub App using the "
        "github_app_login tool. This tool will configure your terminal so that "
        "all `git` and `gh` commands for that repo are authenticated and configured "
        "correctly. The plugin handles credentials transparently. Do not run `gh "
        "auth ...`, `git config ...`, or similar commands; they are unnecessary "
        "and may corrupt configuration files, negatively affecting other users. "
        "For repos hosted on any other host, github_app_login does not apply. "
        "Public reads may succeed without authentication, but private repos and "
        "write operations will not be accessible. If you need access to a private "
        "repo on another host, notify the user. Do NOT attempt any other method "
        f"of logging in or accessing any repo on {_github_host} other than the "
        "github_app_login tool. If you have already logged in to the repo successfully "
        "with the github_app_login tool, but a `git` or `gh` command fails with "
        "an authentication error anyway, that means your access to the repo for "
        "that type of operation is intentionally limited or your access token "
        "expired and you'll need to call github_app_login again (it expires an "
        "hour after you last called the tool). If your limited access is a blocking "
        "issue, you MUST notify the user so they can choose whether to grant "
        "additional access or not. If you accidentally leak a credential or are "
        "simply finished with your `git`/`gh` operations for the time-being, you "
        "can logout by using the github_app_logout tool to revoke your transparent "
        "credential's access to the repo. That limits the damage that could be "
        "caused if someone else intercepted it and tries to use it."
    )


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data)


def _build_noreply_email(app: AppIdentity) -> str:
    return f"{app['bot_user_id']}+{app['slug']}[bot]@users.noreply.github.com"


def _should_fetch_identity(ctx: PluginContext) -> bool:
    keys = ("author_name", "author_email", "committer_name", "committer_email")
    return any(ctx.get_config(key, None) is None for key in keys)


def _resolve_git_identity(ctx: PluginContext) -> tuple[str, str, str, str]:
    identity = _app_identity_cache.get()
    author_name = ctx.get_config("author_name", None)
    author_email = ctx.get_config("author_email", None)
    committer_name = ctx.get_config("committer_name", None)
    committer_email = ctx.get_config("committer_email", None)

    if identity is not None:
        if author_name is None:
            author_name = identity["name"]
        if author_email is None:
            author_email = _build_noreply_email(identity)
        if committer_name is None:
            committer_name = identity["name"]
        if committer_email is None:
            committer_email = _build_noreply_email(identity)

    if author_name is None:
        author_name = ""
    if author_email is None:
        author_email = ""
    if committer_name is None:
        committer_name = ""
    if committer_email is None:
        committer_email = ""

    return author_name, author_email, committer_name, committer_email


def _attempt_identity_fetch() -> None:
    if _auth is None:
        _app_identity_cache.set_failure(
            _FetchOutcome.AUTH,
            "GITHUB_APP_CLIENT_ID and GITHUB_APP_PRIVATE_KEY environment "
            "variables must be set for github-app-auth to function.",
        )
        return

    try:
        identity = _auth.get_app()
        _app_identity_cache.set_success(identity)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        _app_identity_cache.set_failure(
            _FetchOutcome.AUTH,
            f"GitHub API returned HTTP {status} while fetching App identity.",
        )
    except httpx.TransportError as e:
        _app_identity_cache.set_failure(
            _FetchOutcome.NETWORK,
            f"Network error while fetching App identity: {e}",
        )
    except httpx.RequestError as e:
        _app_identity_cache.set_failure(
            _FetchOutcome.NETWORK,
            f"Request error while fetching App identity: {e}",
        )
    except (jwt.PyJWTError, KeyError, RuntimeError) as e:
        _app_identity_cache.set_failure(
            _FetchOutcome.AUTH,
            f"Unexpected error while fetching App identity: {e}",
        )


def _github_app_login_handler(args: ToolArgs, **kwargs: Any) -> str:
    if _auth is None:
        return _json_result(
            {
                "status": "error",
                "message": (
                    "Plugin not configured: GITHUB_APP_CLIENT_ID and "
                    "GITHUB_APP_PRIVATE_KEY environment variables must "
                    "be set by the user."
                ),
            }
        )

    repo = args.get("repo", "").strip()
    if not repo or "/" not in repo:
        return _json_result(
            {"status": "error", "message": "repo must be in 'owner/repo' format"}
        )
    parts = repo.split("/", 1)
    owner, repo_name = parts[0].strip(), parts[1].strip()
    if not owner or not repo_name:
        return _json_result(
            {"status": "error", "message": "repo must be in 'owner/repo' format"}
        )

    try:
        installation_id = _auth.get_installation_id(owner, repo_name)
        token, expires_at = _auth.create_iat(installation_id)
        _auth_state.set(installation_id, token, f"{owner}/{repo_name}", expires_at)
        if _ctx is not None and _should_fetch_identity(_ctx):
            _attempt_identity_fetch()
        return _json_result(
            {
                "status": "authenticated",
                "repo": f"{owner}/{repo_name}",
                "installation_id": installation_id,
                "expires_at": expires_at,
            }
        )
    except (httpx.HTTPError, jwt.PyJWTError, KeyError, RuntimeError) as e:
        logger.exception("github_app_login failed")
        return _json_result({"status": "error", "message": str(e)})


def _github_app_logout_handler(args: ToolArgs, **kwargs: Any) -> str:
    try:
        iat = _auth_state.get_iat()
        if iat is None:
            return _json_result({"status": "already_logged_out"})

        revoked = False
        if _auth is not None:
            revoked = _auth.revoke_iat(iat)
        _auth_state.clear()
        return _json_result({"status": "logged_out", "revoked": revoked})
    except (httpx.HTTPError, RuntimeError) as e:
        logger.exception("github_app_logout failed")
        return _json_result({"status": "error", "message": str(e)})


def _on_session_start_hook(
    session_id: str,
    model: str,
    platform: str,
    **kwargs: Any,
) -> None:
    if _ctx is None:
        return
    if _app_identity_cache.is_resolved:
        return
    if not _should_fetch_identity(_ctx):
        return
    _attempt_identity_fetch()


def _pre_llm_call_hook(
    *,
    session_id: str,
    user_message: str,
    conversation_history: list[dict[str, Any]],
    is_first_turn: bool = False,
    **kwargs: Any,
) -> dict[str, str] | None:
    contexts: list[str] = []

    if is_first_turn:
        if (
            _ctx is not None
            and not _app_identity_cache.was_attempted
            and _should_fetch_identity(_ctx)
        ):
            _attempt_identity_fetch()
        pending = _app_identity_cache.consume_pending_injection()
        if pending is not None:
            outcome, error = pending
            if outcome is _FetchOutcome.NETWORK:
                contexts.append(
                    "[GitHub App Auth] A network error occurred while setting "
                    f"up the github-app-auth toolgroup: {error} Please announce "
                    "this failure to the user now, before taking any other action. "
                    "Suggest that the user check network connectivity to "
                    f"{_github_host}. Do not attempt to fix this yourself. Do "
                    "not create git commits until you have successfully called "
                    "github_app_login."
                )
            elif outcome is _FetchOutcome.AUTH:
                contexts.append(
                    "[GitHub App Auth] An authentication error occurred while "
                    f"setting up the github-app-auth toolgroup: {error} Please "
                    "announce this failure to the user now, before taking any "
                    "other action. Suggest that the user check the github-app-auth "
                    "plugin's configuration and environment variables. Do not "
                    "attempt to fix this yourself. Do not create git commits until "
                    "you have successfully called github_app_login."
                )

    if _auth_state.is_authenticated and _auth_state.is_token_expired():
        status = _auth_state.get_status()
        iat = _auth_state.get_iat()
        if iat is not None and _auth is not None:
            _auth.revoke_iat(iat)
        _auth_state.clear()

        repo = status["repo"] if status is not None else "the repository"
        contexts.append(
            f"[GitHub App Auth] Token for {repo} has expired. Use github_app_login "
            "again to re-authenticate before attempting any `git` or `gh` operations "
            f"on its {_github_host} remote."
        )

    if contexts:
        return {"context": "\n\n".join(contexts)}
    return None


def _terminal_env_middleware(
    tool_name: str,
    args: ToolArgs,
    original_args: ToolArgs,
    **kwargs: Any,
) -> dict[str, Any] | None:
    if tool_name != "terminal":
        return None

    if _ctx is None:
        return None

    if not isinstance(args, dict):
        return None

    status = _auth_state.get_status()
    expired = _auth_state.is_token_expired()

    gh_token = ""
    if status is not None and not expired:
        iat = _auth_state.get_iat()
        if iat is not None:
            gh_token = iat

    git_author_name, git_author_email, git_committer_name, git_committer_email = (
        _resolve_git_identity(_ctx)
    )
    domain = _github_host

    basic_auth = base64.b64encode(f"x-access-token:{gh_token}".encode()).decode()

    config_pairs: list[tuple[str, str]] = [
        (f"url.https://{domain}/.insteadOf", f"git@{domain}:"),
        (f"url.https://{domain}/.insteadOf", f"ssh://git@{domain}/"),
        (
            f"http.https://{domain}/.extraHeader",
            f"Authorization: Basic {basic_auth}",
        ),
    ]

    env_parts = [
        f"GH_TOKEN={shlex.quote(gh_token)}",
        "GIT_CONFIG_GLOBAL=/dev/null",
        f"GIT_AUTHOR_NAME={shlex.quote(git_author_name)}",
        f"GIT_AUTHOR_EMAIL={shlex.quote(git_author_email)}",
        f"GIT_COMMITTER_NAME={shlex.quote(git_committer_name)}",
        f"GIT_COMMITTER_EMAIL={shlex.quote(git_committer_email)}",
        f"GIT_CONFIG_COUNT={len(config_pairs)}",
    ]
    for i, (key, value) in enumerate(config_pairs):
        env_parts.append(f"GIT_CONFIG_KEY_{i}={shlex.quote(key)}")
        env_parts.append(f"GIT_CONFIG_VALUE_{i}={shlex.quote(value)}")

    env_prefix = f"export {' '.join(env_parts)}; "

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
