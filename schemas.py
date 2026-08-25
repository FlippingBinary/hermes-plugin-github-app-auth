from typing import Any, Final

LOGIN_SCHEMA: Final[dict[str, Any]] = {
    "name": "github_app_login",
    "description": (
        "Authenticate as a GitHub App installation for a specific repository. "
        "Call this before performing git clone/push/pull or gh CLI operations "
        "on a GitHub repo to ensure credentials are scoped to the App, not "
        "the human user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": (
                    "GitHub repo in owner/repo format (e.g. 'octocat/Hello-World')"
                ),
            }
        },
        "required": ["repo"],
    },
}

LOGOUT_SCHEMA: Final[dict[str, Any]] = {
    "name": "github_app_logout",
    "description": (
        "Revoke the current GitHub App installation access token and clear "
        "authentication state. Call this when GitHub operations are complete "
        "to ensure the short-lived token is properly revoked."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}
