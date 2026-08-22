# GitHub App Authentication Plugin for Hermes Agent

GitHub App authentication for Hermes Agent. Provides tools to authenticate as
a GitHub App installation and automatically scopes `gh`/`git` CLI credentials
in terminal tool calls.

## Features

- **`github_app_login`** — Authenticates as a GitHub App for a specific repo
- **`github_app_logout`** — Revokes the installation access token
- **`pre_llm_call` hook** — Injects GitHub App auth status into the agent's context
- **`pre_tool_call` hook** — Prefixes every `terminal` tool call with environment
  variables overriding `git` config and `gh` credentials

## Security

This plugin reduces the risk of the agent accidentally using the human user's
GitHub credentials on shared workstations by taking certain precautions:

- The private key and client ID of the Github App are read from environment variables,
  not from disk
- Installation access tokens (IATs) are short-lived (60 minutes) and only injected
  into the environment, not written to disk or config files
- Git identity env vars (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`) override any on-disk
  git config, avoiding improper attribution
- SSH GitHub remote URLs are rewritten to HTTPS via `url.insteadOf` so SSH keys
  are not used for GitHub operations
- When unauthenticated, the IAT is set to `invalid` so git gets a 401 instead
  of falling back to cached/stored credentials

Those precautions do NOT eliminate all risks of leaking the Github App's private
key to the agent, but it makes it easier to limit those risks. For tighter security,
the user should run Hermes Agent in a containerized environment that doesn't have
the private key on disk. As long as the Github App's private key is only made
available to the Hermes Agent as an environment variable, Hermes Agent doesn't
automatically pass it along to commands the agent may run. This effectively prevents
the agent from accidentally exfiltrating it in LLM requests or terminal commands
because it can't even view it. The short-lived IAT is accessible to it, but the
60 minute expiration makes it difficult for an accidentally leaked IAT to be used
by a third-party.

## Requirements

### Environment Variables

| Variable                 | Description                                        | Required |
| ------------------------ | -------------------------------------------------- | -------- |
| `GITHUB_APP_CLIENT_ID`   | The GitHub App's client ID (used as JWT issuer)    | Yes      |
| `GITHUB_APP_PRIVATE_KEY` | PEM-format private key for signing GitHub App JWTs | Yes      |

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

| Setting           | Type | Default                                      | Description                                                |
| ----------------- | ---- | -------------------------------------------- | ---------------------------------------------------------- |
| `author_name`     | str  | `Hermes Agent`                               | Git author name                                            |
| `author_email`    | str  | `hermes-agent[bot]@users.noreply.github.com` | Git author email                                           |
| `committer_name`  | str  | `Hermes Agent`                               | Git committer name                                         |
| `committer_email` | str  | `hermes-agent[bot]@users.noreply.github.com` | Git committer email                                        |
| `domains`         | list | `["github.com"]`                             | GitHub domains for SSH-to-HTTPS rewriting and bearer auth. |

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
{ "status": "logged_out", "revoked": true }
```

### Using terminal tools after login

After calling `github_app_login`, all `terminal` tool calls are automatically
prefixed with environment variables that configure both `gh` and `git`:

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

When unauthenticated or the token has expired, `GH_TOKEN` and the `extraHeader`
token are set to `invalid` so git gets a 401 instead of falling back to stored
credentials.

For GitHub Enterprise, add your domain to `domains`:

```yaml
plugins:
  entries:
    github-app-auth:
      settings:
        domains:
          - github.com
          - github.enterprise.example.com
```

## License

MIT
