## Plan: Hermes Agent GitHub App Plugin

### Overview

Create a native Hermes Agent plugin at `/home/jon/git/FlippingBinary/hermes-agent-github-app-plugin/` that provides GitHub App authentication. The plugin gives the agent tools to log in/logout as a GitHub App installation and uses hooks to (1) inform the agent of auth status before each LLM turn, and (2) automatically inject `GH_TOKEN` + git identity env vars into every `terminal` tool call, preventing accidental use of the human user's credentials on shared workstations.

### Files to Create

#### 1. `plugin.yaml` — Manifest

```yaml
name: github-app-auth
version: 0.1.0
description: >
  GitHub App authentication for Hermes Agent. Provides tools to authenticate
  as a GitHub App installation and automatically scopes gh/git CLI credentials
  in terminal tool calls.
author: FlippingBinary
provides_tools:
  - github_app_login
  - github_app_logout
provides_hooks:
  - pre_llm_call
  - pre_tool_call
requires_env:
  - name: GITHUB_APP_CLIENT_ID
    description: The GitHub App's client ID (used as JWT issuer)
  - name: GITHUB_APP_PRIVATE_KEY
    description: PEM-format private key for signing GitHub App JWTs
    secret: true
python_dependencies:
  - PyJWT>=2.8,<3
  - cryptography>=41.0
  - httpx>=0.24,<1
config_schema:
  git_author_name:
    type: str
    default: "Hermes Agent"
  git_author_email:
    type: str
    default: "hermes-agent[bot]@users.noreply.github.com"
  git_committer_name:
    type: str
    default: "Hermes Agent"
  git_committer_email:
    type: str
    default: "hermes-agent[bot]@users.noreply.github.com"
```

#### 2. `github_auth.py` — Auth Logic + State

Two classes:

**`AuthState`** — thread-safe runtime state holder (uses `threading.Lock`):
- Fields: `installation_id` (int), `iat` (token string), `repo` (owner/repo string), `expires_at` (ISO timestamp string)
- Methods: `get_status()`, `set(installation_id, iat, repo, expires_at)`, `clear()` (returns old IAT for revocation), `is_authenticated` property, `is_token_expired()` method, `get_iat()` method

**`GitHubAppAuth`** — GitHub App authentication flow:
- `__init__(client_id, private_key_pem)` — stores credentials
- `_generate_jwt()` — RS256 JWT with `iat` = now−60s, `exp` = now+600s, `iss` = client_id; signed with private key via `PyJWT`
- `get_installation_id(owner, repo)` — `GET /repos/{owner}/{repo}/installation` with JWT bearer → returns installation ID int
- `create_iat(installation_id)` — `POST /app/installations/{id}/access_tokens` with JWT bearer → returns `(token, expires_at)` tuple
- `revoke_iat(iat)` — `DELETE /installation/token` with IAT bearer → returns bool (best-effort; catches and returns False on any error)

All HTTP via `httpx` with headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <token>`, `X-GitHub-Api-Version: 2022-11-28`. HTTP errors raise exceptions (caught by tool handlers).

#### 3. `schemas.py` — Tool Schemas

- `LOGIN_SCHEMA` — JSON schema: `{"type": "object", "properties": {"repo": {"type": "string", "description": "GitHub repo in owner/repo format (e.g. 'octocat/Hello-World')"}}, "required": ["repo"]}`
- `LOGOUT_SCHEMA` — JSON schema: `{"type": "object", "properties": {}, "description": "No arguments. Clears and revokes the current GitHub App installation token."}`

#### 4. `__init__.py` — `register(ctx)` Entry Point

The `register(ctx)` function:
1. Reads `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_PRIVATE_KEY` from `os.environ`
2. Creates shared `AuthState` instance (module-level, persists for session lifetime)
3. Creates `GitHubAppAuth` instance with credentials
4. Registers **`github_app_login`** tool (schema from `schemas.LOGIN_SCHEMA`, toolset `"github-app"`):
   - Handler: parse `repo` arg → validate `owner/repo` format → `auth.get_installation_id(owner, repo)` → `auth.create_iat(installation_id)` → `state.set(...)` → return JSON `{"status": "authenticated", "repo": "owner/repo", "installation_id": 12345, "expires_at": "..."}`
   - On any error: catch, return JSON `{"status": "error", "message": "..."}`
5. Registers **`github_app_logout`** tool (schema from `schemas.LOGOUT_SCHEMA`, toolset `"github-app"`):
   - Handler: `state.get_iat()` → if IAT exists, attempt `auth.revoke_iat(iat)` (best-effort) → `state.clear()` → return JSON `{"status": "logged_out", "revoked": true/false}`
   - If already logged out: return JSON `{"status": "already_logged_out"}`
6. Registers **`pre_llm_call`** hook:
   - Reads `AuthState`; if not authenticated or token expired → inject: `"[GitHub App] Not authenticated. Use github_app_login with a repo (owner/repo) to authenticate for GitHub operations."`
   - If authenticated → inject: `"[GitHub App] Authenticated for owner/repo (installation #12345). Token expires at <ISO>. Use github_app_logout when done."`
   - Returns `{"context": message}` always
7. Registers **`pre_tool_call`** hook:
   - If `tool_name != "terminal"` → return None (observer)
   - If `tool_name == "terminal"`:
     - Read `AuthState`; determine `GH_TOKEN` value: valid IAT if authenticated and not expired, `"invalid"` otherwise
     - Read git config from `ctx.get_config()` with defaults: `git_author_name`, `git_author_email`, `git_committer_name`, `git_committer_email`
     - Build prefix string: `export GH_TOKEN='<value>' GIT_AUTHOR_NAME='<name>' GIT_AUTHOR_EMAIL='<email>' GIT_COMMITTER_NAME='<name>' GIT_COMMITTER_EMAIL='<email>'; `
     - Modify `args`: if `command` key exists and is non-empty → prefix it; if `code` key exists and is non-empty → prefix it too (both fields are treated identically by the terminal tool)
     - Return `{"action": "modify", "args": modified_args}`

#### 5. `README.md` — Documentation

Setup instructions, env var requirements, config schema table, usage examples, and security notes.

### Design Decisions (with chosen defaults)

1. **Env var prefix style:** Using `export ...; <command>` so env vars persist for chained commands (e.g., `git clone ... && cd ... && make`). Inline `VAR=val cmd` only applies to the first command.
2. **Expired IAT handling:** `pre_tool_call` falls back to `GH_TOKEN=invalid` when the IAT is expired (doesn't auto-refresh). `pre_llm_call` reports the token as expired. The agent can call `github_app_login` again to refresh.
3. **Plugin name:** `github-app-auth`
4. **No pyproject.toml:** Flat native plugin layout (installed via `hermes plugins install <path>` or copy to `~/.hermes/plugins/github-app-auth/`). Can add later for pip distribution.
5. **Logout revocation:** Always attempts `DELETE /installation/token` via IAT bearer. If it fails (network error, already expired, etc.), logs a warning but still clears local state — the token will expire naturally within the hour.

### Security Properties

- `GH_TOKEN` env var is injected per-command via `export`, never written to disk or config files
- Git identity env vars (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`) override any on-disk git config
- When unauthenticated, `GH_TOKEN=invalid` prevents accidental use of cached/stored GitHub credentials
- Private key and client ID only read from environment variables (never stored in plugin state)
- IAT is revoked on logout when possible
