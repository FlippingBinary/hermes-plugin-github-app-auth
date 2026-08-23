from __future__ import annotations

import base64
import json
import logging
import shlex
from collections.abc import Callable
from typing import Any, Protocol, overload

from .github_auth import AuthState, GitHubAppAuth

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

    def register_middleware(
        self,
        kind: str,
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
    try:
        iat = _auth_state.get_iat()
        if iat is None:
            return _json_result({"status": "already_logged_out"})

        revoked = False
        if _auth is not None:
            revoked = _auth.revoke_iat(iat)
        _auth_state.clear()
        return _json_result({"status": "logged_out", "revoked": revoked})
    except Exception as e:
        logger.exception("github_app_logout failed")
        return _json_result({"status": "error", "message": str(e)})


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

    gh_token = "invalid"
    if status is not None and not expired:
        iat = _auth_state.get_iat()
        if iat is not None:
            gh_token = iat

    git_author_name = _ctx.get_config("author_name", "Hermes Agent")
    git_author_email = _ctx.get_config(
        "author_email", "hermes-agent[bot]@users.noreply.github.com"
    )
    git_committer_name = _ctx.get_config("committer_name", "Hermes Agent")
    git_committer_email = _ctx.get_config(
        "committer_email", "hermes-agent[bot]@users.noreply.github.com"
    )
    github_domains = _ctx.get_config("domains", ["github.com"])

    basic_auth = base64.b64encode(f"x-access-token:{gh_token}".encode()).decode()

    config_pairs: list[tuple[str, str]] = []
    for domain in github_domains:
        config_pairs.append((f"url.https://{domain}/.insteadOf", f"git@{domain}:"))
        config_pairs.append(
            (f"url.https://{domain}/.insteadOf", f"ssh://git@{domain}/")
        )
        config_pairs.append(
            (
                f"http.https://{domain}/.extraHeader",
                f"Authorization: Basic {basic_auth}",
            )
        )

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
