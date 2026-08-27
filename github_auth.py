from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, TypedDict
from urllib.parse import urlparse

import httpx
import jwt

GITHUB_API_VERSION = "2022-11-28"

logger = logging.getLogger(__name__)


class GitHubApiError(Exception):
    """Raised when interacting with the GitHub API fails."""

    def __init__(self, message: str, *, network: bool = False) -> None:
        super().__init__(message)
        self.network = network


@dataclass(frozen=True)
class AuthenticatedState:
    installation_id: int
    iat: str
    repo: str
    expires_at: str

    def is_expired(self) -> bool:
        try:
            expires_dt = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return datetime.now(UTC) >= expires_dt
        except (ValueError, AttributeError):
            return True


class AppIdentity(TypedDict):
    id: int
    slug: str
    name: str
    bot_user_id: int


@dataclass(frozen=True)
class GitHubHostConfig:
    hostname: str
    api_base: str | None


class GitHubAppAuth:
    """GitHub App authentication flow.

    JWT generation, installation lookup, IAT creation/revocation.
    """

    def __init__(
        self,
        client_id: str,
        private_key_pem: str,
        host_config: GitHubHostConfig,
    ) -> None:
        self._client_id = client_id
        self._private_key = private_key_pem
        self._host = host_config.hostname
        self._api_base: str | None = host_config.api_base
        self._base_lock = threading.Lock()
        self._client = httpx.Client(timeout=30)
        if self._api_base is None:
            try:
                self._resolve_api_base()
            except GitHubApiError as e:
                logger.warning("Could not resolve GitHub API base at startup: %s", e)

    def _generate_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": self._client_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def _auth_headers(self, token: str) -> dict[str, str]:
        headers = self._base_headers()
        headers["Authorization"] = f"Bearer {token}"
        return headers

    def _jwt_headers(self) -> dict[str, str]:
        return self._auth_headers(self._generate_jwt())

    def _resolve_api_base(self) -> str:
        if self._api_base is not None:
            return self._api_base
        with self._base_lock:
            if self._api_base is not None:
                return self._api_base
            self._api_base = self._probe_api_base()
            return self._api_base

    def _probe_api_base(self) -> str:
        candidates = [
            f"https://api.{self._host}",
            f"https://{self._host}/api/v3",
        ]
        network_error: GitHubApiError | None = None
        for base in candidates:
            try:
                resp = self._client.get(f"{base}/app", headers=self._jwt_headers())
                resp.raise_for_status()
                return base
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status == HTTPStatus.NOT_FOUND:
                    continue
                raise GitHubApiError(f"GitHub API returned HTTP {status}") from e
            except (httpx.TransportError, httpx.RequestError) as e:
                network_error = GitHubApiError(
                    f"Network error while probing API base: {e}",
                    network=True,
                )
        if network_error is not None:
            raise network_error
        raise GitHubApiError(
            f"Could not find a GitHub API at {self._host} "
            f"(tried {', '.join(candidates)})."
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        base = self._resolve_api_base()
        url = f"{base}{path}"
        try:
            resp = self._client.request(method, url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            raise GitHubApiError(f"GitHub API returned HTTP {status}") from e
        except (httpx.TransportError, httpx.RequestError) as e:
            raise GitHubApiError(f"Network error: {e}", network=True) from e
        try:
            data = resp.json()
        except ValueError as e:
            raise GitHubApiError(f"Invalid JSON in API response: {e}") from e
        if not isinstance(data, dict):
            raise GitHubApiError(f"Expected JSON object, got {type(data).__name__}")
        return data

    def get_app(self) -> AppIdentity:
        data = self._request("GET", "/app", headers=self._jwt_headers())
        slug = data.get("slug")
        if slug is None:
            raise GitHubApiError("GitHub API response missing 'slug' field")
        bot_data = self._request(
            "GET", f"/users/{slug}[bot]", headers=self._base_headers()
        )
        bot_user_id = bot_data.get("id")
        if bot_user_id is None:
            raise GitHubApiError("GitHub API response missing bot user 'id' field")
        app_id = data.get("id")
        if app_id is None:
            raise GitHubApiError("GitHub API response missing 'id' field")
        app_name = data.get("name")
        if app_name is None:
            raise GitHubApiError("GitHub API response missing 'name' field")
        return AppIdentity(
            id=app_id,
            slug=slug,
            name=app_name,
            bot_user_id=bot_user_id,
        )

    def get_installation_id(self, owner: str, repo: str) -> int:
        data = self._request(
            "GET",
            f"/repos/{owner}/{repo}/installation",
            headers=self._jwt_headers(),
        )
        installation_id = data.get("id")
        if installation_id is None:
            raise GitHubApiError("GitHub API response missing installation 'id' field")
        return installation_id

    def create_iat(self, installation_id: int) -> tuple[str, str]:
        data = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            headers=self._jwt_headers(),
        )
        token = data.get("token")
        if token is None:
            raise GitHubApiError("GitHub API response missing 'token' field")
        expires_at = data.get("expires_at")
        if expires_at is None:
            raise GitHubApiError("GitHub API response missing 'expires_at' field")
        return token, expires_at

    def revoke_iat(self, iat: str) -> bool:
        try:
            base = self._resolve_api_base()
        except GitHubApiError as e:
            logger.warning("Failed to resolve API base for token revocation: %s", e)
            return False
        url = f"{base}/installation/token"
        try:
            resp = self._client.delete(url, headers=self._auth_headers(iat))
            return resp.status_code == HTTPStatus.NO_CONTENT
        except httpx.HTTPError as e:
            logger.warning("Failed to revoke installation token: %s", e)
            return False


def _normalize_hostname(host: str | None) -> str | None:
    if host is None:
        return None
    stripped = host.strip().lower()
    if not stripped:
        return None
    candidate = stripped if "://" in stripped else f"https://{stripped}"
    try:
        parsed = urlparse(candidate)
    except ValueError as e:
        logger.error(
            "GITHUB_APP_HOST %r could not be parsed as a URL: %s",
            stripped,
            e,
        )
        return None
    hostname = parsed.hostname
    if not hostname:
        logger.error(
            "GITHUB_APP_HOST %r did not parse as having a hostname.",
            stripped,
        )
        return None
    hostname = hostname.rstrip(".")
    if hostname != stripped:
        logger.warning(
            "GITHUB_APP_HOST %r was normalized to %r; "
            "set just the bare hostname (e.g. %s) to silence this warning.",
            stripped,
            hostname,
            hostname,
        )
    return hostname


def resolve_host(raw: str | None) -> GitHubHostConfig:
    hostname = _normalize_hostname(raw)
    if hostname is None or hostname == "github.com":
        return GitHubHostConfig("github.com", "https://api.github.com")
    return GitHubHostConfig(hostname, None)
