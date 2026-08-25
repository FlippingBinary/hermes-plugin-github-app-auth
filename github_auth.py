from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, TypedDict, TypeVar
from urllib.parse import urlparse

import httpx
import jwt

if TYPE_CHECKING:
    from collections.abc import Callable

GITHUB_API_VERSION = "2022-11-28"

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AuthStatus(TypedDict):
    repo: str
    installation_id: int
    expires_at: str


class AppIdentity(TypedDict):
    id: int
    slug: str
    name: str
    bot_user_id: int


class AuthState:
    """Thread-safe holder for GitHub App installation authentication state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installation_id: int | None = None
        self._iat: str | None = None
        self._repo: str | None = None
        self._expires_at: str | None = None

    @property
    def is_authenticated(self) -> bool:
        with self._lock:
            return self._iat is not None and self._installation_id is not None

    def is_token_expired(self) -> bool:
        with self._lock:
            if self._expires_at is None:
                return True
            try:
                expires_dt = datetime.fromisoformat(
                    self._expires_at.replace("Z", "+00:00")
                )
                return datetime.now(UTC) >= expires_dt
            except (ValueError, AttributeError):
                return True

    def get_iat(self) -> str | None:
        with self._lock:
            return self._iat

    def get_status(self) -> AuthStatus | None:
        with self._lock:
            if self._iat is None or self._installation_id is None:
                return None
            return {
                "repo": self._repo,  # type: ignore[typeddict-item]
                "installation_id": self._installation_id,
                "expires_at": self._expires_at,  # type: ignore[typeddict-item]
            }

    def set(
        self,
        installation_id: int,
        iat: str,
        repo: str,
        expires_at: str,
    ) -> None:
        with self._lock:
            self._installation_id = installation_id
            self._iat = iat
            self._repo = repo
            self._expires_at = expires_at

    def clear(self) -> str | None:
        with self._lock:
            old_iat = self._iat
            self._installation_id = None
            self._iat = None
            self._repo = None
            self._expires_at = None
            return old_iat


class GitHubAppAuth:
    """GitHub App authentication flow.

    JWT generation, installation lookup, IAT creation/revocation.
    """

    def __init__(self, client_id: str, private_key_pem: str, host: str | None) -> None:
        self._client_id = client_id
        self._private_key = private_key_pem
        normalized = _normalize_host(host)
        if normalized is None or normalized == "github.com":
            self._api_base: str | None = "https://api.github.com"
        else:
            self._api_base = None
        self._host = normalized or "github.com"

    def _generate_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": self._client_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _jwt_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._generate_jwt()}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def _api_candidates(self) -> list[str]:
        if self._api_base is not None:
            return [self._api_base]
        return [f"https://api.{self._host}", f"https://{self._host}/api/v3"]

    def _try_with_candidates(self, api_call: Callable[[str], T]) -> T:
        candidates = self._api_candidates()
        if len(candidates) == 1:
            return api_call(candidates[0])

        network_error: Exception | None = None
        auth_error: Exception | None = None

        for base in candidates:
            try:
                result = api_call(base)
                self._api_base = base
                return result
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status == HTTPStatus.NOT_FOUND:
                    continue
                auth_error = e
            except (httpx.TransportError, httpx.RequestError) as e:
                network_error = e

        if network_error is not None:
            raise network_error
        if auth_error is not None:
            raise auth_error
        raise RuntimeError(
            f"Could not find a GitHub API at {self._host} "
            f"(tried {', '.join(candidates)})."
        )

    def _get_app(self, base: str) -> AppIdentity:
        headers = self._jwt_headers()
        url = f"{base}/app"
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            slug = data["slug"]
            bot_headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            }
            bot_resp = client.get(f"{base}/users/{slug}[bot]", headers=bot_headers)
            bot_resp.raise_for_status()
            bot_data = bot_resp.json()
            return AppIdentity(
                id=data["id"],
                slug=slug,
                name=data["name"],
                bot_user_id=bot_data["id"],
            )

    def get_app(self) -> AppIdentity:
        return self._try_with_candidates(self._get_app)

    def _get_installation_id(self, base: str, owner: str, repo: str) -> int:
        url = f"{base}/repos/{owner}/{repo}/installation"
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=self._jwt_headers())
            resp.raise_for_status()
            data = resp.json()
            return data["id"]

    def get_installation_id(self, owner: str, repo: str) -> int:
        return self._try_with_candidates(
            lambda base: self._get_installation_id(base, owner, repo)
        )

    def _create_iat(self, base: str, installation_id: int) -> tuple[str, str]:
        url = f"{base}/app/installations/{installation_id}/access_tokens"
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, headers=self._jwt_headers())
            resp.raise_for_status()
            data = resp.json()
            return data["token"], data["expires_at"]

    def create_iat(self, installation_id: int) -> tuple[str, str]:
        return self._try_with_candidates(
            lambda base: self._create_iat(base, installation_id)
        )

    def revoke_iat(self, iat: str) -> bool:
        if self._api_base is None:
            return False
        url = f"{self._api_base}/installation/token"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {iat}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.delete(url, headers=headers)
                return resp.status_code == HTTPStatus.NO_CONTENT
        except httpx.HTTPError as e:
            logger.warning("Failed to revoke installation token: %s", e)
            return False


def _normalize_host(host: str | None) -> str | None:
    if host is None:
        return None
    stripped = host.strip().lower()
    if not stripped:
        return None
    candidate = stripped if "://" in stripped else f"https://{stripped}"
    try:
        parsed = urlparse(candidate)
    except ValueError as e:
        logger.error("GITHUB_APP_HOST %r could not be parsed as a URL: %s", stripped, e)
        return None
    hostname = parsed.hostname
    if not hostname:
        logger.error("GITHUB_APP_HOST %r did not parse as having a hostname.", stripped)
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
