# GitHub App Authentication Plugin for Hermes Agent

GitHub App authentication for [Hermes Agent](https://hermes-agent.nousresearch.com).
Provides tools to authenticate as a GitHub App installation and automatically
scopes `gh`/`git` CLI credentials in terminal tool calls.

## Features

- **`github_app_login`** — Authenticates as a GitHub App for a specific repo
- **`github_app_logout`** — Revokes the installation access token
- **`on_session_start` hook** — Fetches the GitHub App's identity for default
  git identity (name and noreply email)
- **`pre_llm_call` hook** — Injects GitHub App auth status into the agent's context
- **`tool_request` middleware** — Prefixes every `terminal` tool call with
  environment variables overriding Github credentials and git config

## Security

LLM-based agents are inherently unpredictable. Many of them, if not most, are
not trustworthy with secrets. The same agent might publish a private key in a
public web app for convenience during one session and flag the very same thing
as a major concern during the next. In the meantime, you might not notice your
GitHub bill skyrocketing or a repo of yours suddenly hosting malware.

To reduce the risk of damage from agents accidentally leaking secrets, this plugin
makes every effort to reduce their access to the GitHub App's private key. The key
is read from an environment variable, so it never has to exist on disk. The only
token the agent must have access to is the installation access token (IAT) that
this plugin creates, which has a 60 minute expiration from the time it is created.
Even so, these measures do not eliminate all the risks of leaking the private
key - they simply make it easier to limit those risks. For tighter security, run
Hermes Agent in a containerized environment that doesn't have the private key
on disk. As long as the GitHub App's private key is only made available to Hermes
Agent as an environment variable, Hermes Agent doesn't automatically pass the
key along to commands the agent may run. This effectively prevents the agent from
accidentally exfiltrating the private key in LLM requests or terminal commands,
because the agent can't even view it. The short-lived IAT is accessible to the
agent, but the 60 minute expiration makes it difficult for an accidentally leaked
IAT to be used by a third-party.

This plugin also reduces the risk of the agent accidentally using a human user's
GitHub credentials or git identity on shared workstations by taking certain other
precautions:

- The user's git config is disabled, preventing the accidental use of any stored
  credentials or identity it may contain
- SSH GitHub remote URLs are rewritten to HTTPS so SSH keys are not accidentally
  used for GitHub operations
- When unauthenticated, an invalid credential is injected so git gets a 401
  instead of falling back to cached/stored credentials

## Requirements

### Environment Variables

| Variable                 | Description                                                                | Required |
| ------------------------ | -------------------------------------------------------------------------- | -------- |
| `GITHUB_APP_CLIENT_ID`   | The GitHub App's client ID (used as JWT issuer)                            | Yes      |
| `GITHUB_APP_PRIVATE_KEY` | PEM-format private key for signing GitHub App JWTs                         | Yes      |
| `GITHUB_APP_HOST`        | Hostname of the GitHub App's server (e.g. `github.com` or a GHES hostname) | No       |

### Python Dependencies

- `PyJWT[crypto]>=2.8,<3`
- `httpx>=0.24,<1`

## Installation

### From the repository

```bash
hermes plugins install FlippingBinary/hermes-plugin-github-app-auth
```

### Manual install

Copy the plugin directory to `~/.hermes/plugins/github-app-auth/`, then enable it:

```bash
hermes plugins enable github-app-auth
```

## Configuration

Configurable settings under `plugins.entries.github-app-auth.settings`:

| Setting           | Type | Default | Description                                                                              |
| ----------------- | ---- | ------- | ---------------------------------------------------------------------------------------- |
| `author_name`     | str  | _auto_  | Git author name. When unset, derived from the GitHub App's `name` via `GET /app`.        |
| `author_email`    | str  | _auto_  | Git author email. When unset, derived as `{bot_user_id}+{slug}[bot]@users.noreply.github.com`.    |
| `committer_name`  | str  | _auto_  | Git committer name. When unset, derived from the GitHub App's `name` via `GET /app`.     |
| `committer_email` | str  | _auto_  | Git committer email. When unset, derived as `{bot_user_id}+{slug}[bot]@users.noreply.github.com`. |

### Auto-detected git identity

When any of the four identity settings (`author_name`, `author_email`, `committer_name`,
`committer_email`) are unset, the plugin fetches the GitHub App's slug, name,
and bot user ID from the GitHub API. The `author_name` and `committer_name` are
set to the GitHub App's name, and the email address is constructed from the slug
and bot user's ID as `{bot_user_id}+{slug}[bot]@users.noreply.github.com`. Each
setting is independent — you can override any subset and leave the rest auto-detected.

If all four settings are explicitly configured, no API calls are made.

#### Failure handling

If the GitHub App identity can't be obtained (network error or authentication
error), the plugin forces the git identity environment variables to be empty (so
`git` will error if the agent attempts to commit) and injects a one-time message
into the agent's context on the first turn. The agent is instructed to announce
the failure to the user before taking any other action:

- **Network error** — suggests checking network connectivity to the configured host
- **Authentication error** — suggests checking the plugin's configuration
  (`GITHUB_APP_CLIENT_ID` and `GITHUB_APP_PRIVATE_KEY` environment variables)

The fetch is retried as part of `github_app_login`, so the correct identity
is used once login succeeds.

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

If no session is active, returns:

```json
{ "status": "already_logged_out" }
```

### Using terminal tools

After calling `github_app_login`, all `terminal` tool calls are automatically
prefixed with environment variables that configure both `gh` and `git`:

```bash
export GH_TOKEN='ghs_xxxx' \
  GIT_CONFIG_GLOBAL=/dev/null \
  GIT_AUTHOR_NAME='My GitHub App' \
  GIT_AUTHOR_EMAIL='67890+my-app[bot]@users.noreply.github.com' \
  GIT_COMMITTER_NAME='My GitHub App' \
  GIT_COMMITTER_EMAIL='67890+my-app[bot]@users.noreply.github.com' \
  GIT_CONFIG_COUNT=3 \
  GIT_CONFIG_KEY_0='url.https://github.com/.insteadOf' \
  GIT_CONFIG_VALUE_0='git@github.com:' \
  GIT_CONFIG_KEY_1='url.https://github.com/.insteadOf' \
  GIT_CONFIG_VALUE_1='ssh://git@github.com/' \
  GIT_CONFIG_KEY_2='http.https://github.com/.extraHeader' \
  GIT_CONFIG_VALUE_2='Authorization: Basic eC1hY2xxx='; \
  <original command>
```

The `GIT_AUTHOR_*` and `GIT_COMMITTER_*` values shown above are derived from the
GitHub App's `/app` endpoint by default. If the fetch fails, the environment variables
are left empty so `git` will error rather than commit with an incorrect identity.

When unauthenticated or the token has expired, `GH_TOKEN` and the `extraHeader`
token are set to `invalid` so git gets a 401 instead of falling back to stored
credentials.

For GitHub Enterprise, set the `GITHUB_APP_HOST` environment variable to your
GHES hostname:

```bash
export GITHUB_APP_HOST=github.enterprise.example.com
```

The hostname is used to generate the `GIT_CONFIG_*` values that tell `git` to use
https with the GitHub App's IAT even if the repo is configured with an SSH URL.
The authorization header is injected only for git operations involving that host
so that it does not accidentally get used for git operations on GitLab or somewhere
else. When the host is not `github.com`, the plugin discovers the API base by
probing `api.{host}` first, then `{host}/api/v3`.

## License

MIT
