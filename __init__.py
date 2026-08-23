from __future__ import annotations

import logging
import os

from . import tools
from .github_auth import GitHubAppAuth
from .schemas import LOGIN_SCHEMA, LOGOUT_SCHEMA

logger = logging.getLogger(__name__)


def register(ctx: tools.PluginContext) -> None:
    client_id = os.environ.get("GITHUB_APP_CLIENT_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if client_id is not None and private_key is not None:
        tools._auth = GitHubAppAuth(client_id, private_key)
    else:
        tools._auth = None
        logger.error(
            "GITHUB_APP_CLIENT_ID and GITHUB_APP_PRIVATE_KEY must be set "
            "for github_app_login to function"
        )
    tools._ctx = ctx

    ctx.register_tool(
        "github_app_login",
        "github-app-auth",
        LOGIN_SCHEMA,
        tools._github_app_login_handler,
    )

    ctx.register_tool(
        "github_app_logout",
        "github-app-auth",
        LOGOUT_SCHEMA,
        tools._github_app_logout_handler,
    )

    ctx.register_system_prompt_section(
        "github-app-auth-guidance",
        tools.STATIC_GUIDANCE_TEXT,
        position="after_memory",
    )
    ctx.register_hook("pre_llm_call", tools._pre_llm_call_hook)
    ctx.register_hook("on_session_start", tools._on_session_start_hook)
    ctx.register_middleware("tool_request", tools._terminal_env_middleware)
