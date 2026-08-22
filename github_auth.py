from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import TypedDict

import httpx
import jwt

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

logger = logging.getLogger(__name__)


class AuthStatus(TypedDict):
    repo: str
    installation_id: int
    expires_at: str


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
                return datetime.now(timezone.utc) >= expires_dt
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
    """GitHub App authentication flow: JWT generation, installation lookup, IAT creation/revocation."""

    def __init__(self, client_id: str, private_key_pem: str) -> None:
        self._client_id = client_id
        self._private_key = private_key_pem

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

    def get_installation_id(self, owner: str, repo: str) -> int:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/installation"
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=self._jwt_headers())
            resp.raise_for_status()
            data = resp.json()
            return data["id"]

    def create_iat(self, installation_id: int) -> tuple[str, str]:
        url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, headers=self._jwt_headers())
            resp.raise_for_status()
            data = resp.json()
            return data["token"], data["expires_at"]

    def revoke_iat(self, iat: str) -> bool:
        url = f"{GITHUB_API_BASE}/installation/token"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {iat}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.delete(url, headers=headers)
                return resp.status_code == 204
        except Exception as e:
            logger.warning("Failed to revoke installation token: %s", e)
            return False
