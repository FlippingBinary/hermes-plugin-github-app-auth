# GitHub App Authentication Plugin for Hermes Agent

GitHub App authentication for Hermes Agent. Provides tools to authenticate as
a GitHub App installation and automatically scopes `gh`/`git` CLI credentials
in terminal tool calls.

## Features

- **`github_app_login`** — Authenticates as a GitHub App for a specific repo
- **`github_app_logout`** — Revokes the installation access token
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

| Variable                 | Description                                        | Required |
| ------------------------ | -------------------------------------------------- | -------- |
| `GITHUB_APP_CLIENT_ID`   | The GitHub App's client ID (used as JWT issuer)    | Yes      |
| `GITHUB_APP_PRIVATE_KEY` | PEM-format private key for signing GitHub App JWTs | Yes      |

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

| Setting           | Type | Default                                      | Description                                                |
| ----------------- | ---- | -------------------------------------------- | ---------------------------------------------------------- |
| `author_name`     | str  | `Hermes Agent`                               | Git author name                                            |
| `author_email`    | str  | `hermes-agent[bot]@users.noreply.github.com` | Git author email                                           |
| `committer_name`  | str  | `Hermes Agent`                               | Git committer name                                         |
| `committer_email` | str  | `hermes-agent[bot]@users.noreply.github.com` | Git committer email                                        |
| `domains`         | list | `["github.com"]`                             | GitHub domains for SSH-to-HTTPS rewriting and token auth. |

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

### Using terminal tools after login

After calling `github_app_login`, all `terminal` tool calls are automatically
prefixed with environment variables that configure both `gh` and `git`:

```bash
export GH_TOKEN='ghs_xxxx' \
  GIT_CONFIG_GLOBAL=/dev/null \
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
  GIT_CONFIG_VALUE_2='Authorization: Basic eC1hY2xxx='; \
  <original command>
```

When unauthenticated or the token has expired, `GH_TOKEN` and the
`extraHeader` token are set to `invalid` so git gets a 401 instead of falling
back to stored credentials.

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
