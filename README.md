# Hermes Agent GitHub App Plugin

GitHub App authentication for Hermes Agent. Provides tools to authenticate as a GitHub App installation and automatically scopes `gh`/`git` CLI credentials in terminal tool calls.

## Features

- **`github_app_login`** — Authenticates as a GitHub App installation for a specific repo
- **`github_app_logout`** — Revokes the installation access token and clears local state
- **`pre_llm_call` hook** — Injects GitHub App auth status into the agent's context each turn
- **`pre_tool_call` hook** — Prefixes every `terminal` tool call with `GH_TOKEN` and git identity environment variables

## Security

This plugin prevents the agent from accidentally using the human user's GitHub credentials on shared workstations:

- `GH_TOKEN` is injected per-command via `export`, never written to disk or config files
- Git identity env vars (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`) override any on-disk git config
- When unauthenticated, `GH_TOKEN=invalid` prevents accidental use of cached/stored credentials
- Private key and client ID are read only from environment variables
- Installation access tokens (IATs) are revoked on logout when possible

## Requirements

### Environment Variables

| Variable | Description | Required |
|---|---|---|
| `GITHUB_APP_CLIENT_ID` | The GitHub App's client ID (used as JWT issuer) | Yes |
| `GITHUB_APP_PRIVATE_KEY` | PEM-format private key for signing GitHub App JWTs | Yes |

### Python Dependencies

- `PyJWT>=2.8,<3`
- `cryptography>=41.0`
- `httpx>=0.24,<1`

## Installation

### From the repository

```bash
hermes plugins install FlippingBinary/hermes-plugin-github-app-auth
```

### Manual install

Copy the plugin directory to `~/.hermes/plugins/github-app-auth/`.

## Configuration

Configurable settings under `plugins.entries.github-app-auth.settings`:

| Setting | Type | Default | Description |
|---|---|---|---|
| `git_author_name` | str | `Hermes Agent` | Git author name |
| `git_author_email` | str | `hermes-agent[bot]@users.noreply.github.com` | Git author email |
| `git_committer_name` | str | `Hermes Agent` | Git committer name |
| `git_committer_email` | str | `hermes-agent[bot]@users.noreply.github.com` | Git committer email |

## Usage

### Authenticate

```
github_app_login {"repo": "octocat/Hello-World"}
```

Response:
```json
{
  "status": "authenticated",
  "repo": "octocat/Hello-World",
  "installation_id": 12345,
  "expires_at": "2026-08-22T15:30:00Z"
}
```

### Logout

```
github_app_logout {}
```

Response:
```json
{"status": "logged_out", "revoked": true}
```

### Using terminal tools after login

After calling `github_app_login`, all `terminal` tool calls are automatically prefixed with:

```bash
export GH_TOKEN='<token>' GIT_AUTHOR_NAME='Hermes Agent' GIT_AUTHOR_EMAIL='...' GIT_COMMITTER_NAME='Hermes Agent' GIT_COMMITTER_EMAIL='...'; <original command>
```

When unauthenticated, `GH_TOKEN` is set to `invalid` to prevent accidental use of stored credentials.

## License

MIT
