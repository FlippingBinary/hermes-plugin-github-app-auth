from __future__ import annotations

import atexit
import logging
import os

from . import tools
from .github_auth import GitHubAppAuth, resolve_host
from .schemas import LOGIN_SCHEMA, LOGOUT_SCHEMA

logger = logging.getLogger(__name__)


def register(ctx: tools.PluginContext) -> None:
    client_id = os.environ.get("GITHUB_APP_CLIENT_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    host_config = resolve_host(os.environ.get("GITHUB_APP_HOST"))

    auth: GitHubAppAuth | None = None
    if client_id is not None and private_key is not None:
        auth = GitHubAppAuth(client_id, private_key, host_config)
        atexit.register(auth.close)
    else:
        logger.error(
            "GITHUB_APP_CLIENT_ID and GITHUB_APP_PRIVATE_KEY must be set "
            "for github_app_login to function"
        )

    plugin = tools.GitHubAppAuthPlugin(ctx, auth, host_config.hostname)

    ctx.register_tool(
        "github_app_login",
        "github-app-auth",
        LOGIN_SCHEMA,
        plugin.login,
    )

    ctx.register_tool(
        "github_app_logout",
        "github-app-auth",
        LOGOUT_SCHEMA,
        plugin.logout,
    )

    ctx.register_system_prompt_section(
        "github-app-auth-guidance",
        lambda *_: plugin.build_guidance_text(),
        position="after_memory",
    )
    ctx.register_hook("pre_llm_call", plugin.pre_llm_call)
    ctx.register_hook("on_session_start", plugin.on_session_start)
    ctx.register_middleware("tool_request", plugin.terminal_env_middleware)
