# Hermes Agent GitHub App Plugin

GitHub App authentication for Hermes Agent. Provides tools to authenticate as a GitHub App installation and automatically scopes `gh`/`git` CLI credentials in terminal tool calls.

## Features

- **`github_app_login`** — Authenticates as a GitHub App installation for a specific repo
- **`github_app_logout`** — Revokes the installation access token and clears local state
- **`pre_llm_call` hook** — Injects GitHub App auth status into the agent's context each turn
- **`pre_tool_call` hook** — Prefixes every `terminal` tool call with `GH_TOKEN`, git identity env vars, and `GIT_CONFIG_*` env vars that rewrite SSH remotes to HTTPS and inject bearer token authentication

## Security

This plugin prevents the agent from accidentally using the human user's GitHub credentials on shared workstations:

- `GH_TOKEN` is injected per-command via `export`, never written to disk or config files
- Git identity env vars (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`) override any on-disk git config
- SSH GitHub remote URLs are rewritten to HTTPS via `url.insteadOf` so SSH keys are never used
- Bearer token authentication is injected via `http.extraHeader` so git authenticates with the installation token, not stored credentials
- When unauthenticated, `GH_TOKEN` and the `extraHeader` token are set to `invalid` to prevent accidental use of cached/stored credentials
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
| `github_domains` | list | `["github.com"]` | GitHub domains for SSH-to-HTTPS rewriting and bearer auth. |

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

After calling `github_app_login`, all `terminal` tool calls are automatically prefixed with environment variables that configure both `gh` and `git`:

```bash
export GH_TOKEN='ghs_xxxx' \
  GIT_AUTHOR_NAME='Hermes Agent' \
  GIT_AUTHOR_EMAIL='hermes-agent[bot]@users.noreply.github.com' \
  GIT_COMMITTER_NAME='Hermes Agent' \
  GIT_COMMITTER_EMAIL='hermes-agent[bot]@users.noreply.github.com' \
  GIT_CONFIG_COUNT=3 \
  GIT_CONFIG_KEY_0='url.https://github.com/.insteadOf' \
  GIT_CONFIG_VALUE_0='git@github.com:' \
  GIT_CONFIG_KEY_1='url.https://github.com/.insteadOf' \
  GIT_CONFIG_VALUE_1='ssh://git@github.com/' \
  GIT_CONFIG_KEY_2='http.https://github.com/.extraHeader' \
  GIT_CONFIG_VALUE_2='Authorization: Bearer ghs_xxxx'; \
  <original command>
```

When unauthenticated or the token has expired, `GH_TOKEN` and the `extraHeader` token are set to `invalid` so git gets a 401 instead of falling back to stored credentials.

For GitHub Enterprise, add your domain to `github_domains`:

```yaml
plugins:
  entries:
    github-app-auth:
      settings:
        github_domains:
          - github.com
          - github.enterprise.example.com
```

## License

MIT
