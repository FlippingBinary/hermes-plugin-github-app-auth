from __future__ import annotations

import json
import logging
import os
import shlex
from typing import Any, Callable, Protocol, overload

from .github_auth import AuthState, AuthStatus, GitHubAppAuth
from .schemas import LOGIN_SCHEMA, LOGOUT_SCHEMA

logger = logging.getLogger(__name__)

ToolArgs = dict[str, Any]
JSONSchema = dict[str, Any]


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

    @overload
    def get_config(self, key: str) -> Any: ...

    @overload
    def get_config(self, key: str, default: Any) -> Any: ...

    def get_config(self, key: str, default: Any = ...) -> Any: ...


_auth_state = AuthState()
_auth: GitHubAppAuth | None = None
_ctx: PluginContext | None = None


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data)


def _github_app_login_handler(args: ToolArgs, **kwargs: Any) -> str:
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
        return _json_result(
            {
                "status": "authenticated",
                "repo": f"{owner}/{repo_name}",
                "installation_id": installation_id,
                "expires_at": expires_at,
            }
        )
    except Exception as e:
        logger.exception("github_app_login failed")
        return _json_result({"status": "error", "message": str(e)})


def _github_app_logout_handler(args: ToolArgs, **kwargs: Any) -> str:
    iat = _auth_state.get_iat()
    if iat is None:
        return _json_result({"status": "already_logged_out"})

    revoked = False
    if _auth is not None:
        revoked = _auth.revoke_iat(iat)
    _auth_state.clear()
    return _json_result({"status": "logged_out", "revoked": revoked})


def _pre_llm_call_hook(
    session_id: str,
    user_message: str,
    conversation_history: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, str]:
    status = _auth_state.get_status()
    if status is None:
        message = (
            "[GitHub App] Not authenticated. Use github_app_login with a repo "
            "(owner/repo) to authenticate for GitHub operations."
        )
    else:
        expired = _auth_state.is_token_expired()
        if expired:
            message = (
                f"[GitHub App] Token for {status['repo']} has expired. "
                "Use github_app_login again to refresh."
            )
        else:
            message = (
                f"[GitHub App] Authenticated for {status['repo']} "
                f"(installation #{status['installation_id']}). "
                f"Token expires at {status['expires_at']}. "
                "Use github_app_logout when done."
            )
    return {"context": message}


def _pre_tool_call_hook(
    tool_name: str,
    args: ToolArgs,
    task_id: str,
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

    gh_token = "invalid"
    if status is not None and not expired:
        iat = _auth_state.get_iat()
        if iat is not None:
            gh_token = iat

    git_author_name = _ctx.get_config("git_author_name", "Hermes Agent")
    git_author_email = _ctx.get_config(
        "git_author_email", "hermes-agent[bot]@users.noreply.github.com"
    )
    git_committer_name = _ctx.get_config("git_committer_name", "Hermes Agent")
    git_committer_email = _ctx.get_config(
        "git_committer_email", "hermes-agent[bot]@users.noreply.github.com"
    )

    env_prefix = (
        f"export GH_TOKEN={shlex.quote(gh_token)} "
        f"GIT_AUTHOR_NAME={shlex.quote(git_author_name)} "
        f"GIT_AUTHOR_EMAIL={shlex.quote(git_author_email)} "
        f"GIT_COMMITTER_NAME={shlex.quote(git_committer_name)} "
        f"GIT_COMMITTER_EMAIL={shlex.quote(git_committer_email)}; "
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

    return {"action": "modify", "args": modified}


def register(ctx: PluginContext) -> None:
    global _auth, _ctx

    client_id = os.environ.get("GITHUB_APP_CLIENT_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")

    _auth = GitHubAppAuth(client_id, private_key)
    _ctx = ctx

    ctx.register_tool(
        "github_app_login",
        "github-app",
        LOGIN_SCHEMA,
        _github_app_login_handler,
    )

    ctx.register_tool(
        "github_app_logout",
        "github-app",
        LOGOUT_SCHEMA,
        _github_app_logout_handler,
    )

    ctx.register_hook("pre_llm_call", _pre_llm_call_hook)
    ctx.register_hook("pre_tool_call", _pre_tool_call_hook)
