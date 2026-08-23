LOGIN_SCHEMA = {
    "type": "object",
    "properties": {
        "repo": {
            "type": "string",
            "description": "GitHub repo in owner/repo format (e.g. 'octocat/Hello-World')",
        }
    },
    "required": ["repo"],
}

LOGOUT_SCHEMA = {
    "type": "object",
    "properties": {},
    "description": "No arguments. Clears and revokes the current GitHub App installation token.",
}
