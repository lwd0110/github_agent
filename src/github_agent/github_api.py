"""Small, dependency-free client for the GitHub REST API."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class GitHubAPIError(RuntimeError):
    """Raised when GitHub cannot satisfy a read-only request."""


@dataclass(frozen=True)
class CachedResponse:
    """One in-memory GET response, valid until its monotonic expiry time."""

    payload: Any
    expires_at: float


class APIResponseCache:
    """Small per-client cache for JSON-safe GitHub GET responses."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._entries: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], CachedResponse] = OrderedDict()

    def get(self, key: tuple[str, tuple[tuple[str, str], ...]]) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        return copy.deepcopy(entry.payload)

    def put(self, key: tuple[str, tuple[tuple[str, str], ...]], payload: Any, ttl_seconds: float) -> None:
        self._entries[key] = CachedResponse(
            payload=copy.deepcopy(payload),
            expires_at=self._clock() + ttl_seconds,
        )


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def parse_repository_url(value: str) -> RepositoryRef:
    """Parse a github.com owner/repository URL or an OWNER/REPO shorthand."""
    value = value.strip().removesuffix("/")
    shorthand = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", value)
    if shorthand:
        return RepositoryRef(*shorthand.groups())

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        raise ValueError("Use a GitHub URL such as https://github.com/owner/repository.")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2:
        raise ValueError("The URL must identify one repository: https://github.com/owner/repository.")
    owner, name = segments
    return RepositoryRef(owner, name.removesuffix(".git"))


class GitHubClient:
    """Read-only GitHub REST client; no mutation endpoints are implemented."""

    api_base = "https://api.github.com"

    def __init__(
        self,
        token: str | None = None,
        timeout: int = 20,
        max_retries: int = 2,
        backoff_seconds: float = 0.25,
        cache_ttl_seconds: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self._sleep = sleep
        self._event_sink = event_sink
        self._cache = APIResponseCache(clock)

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        normalized_params = tuple(sorted((params or {}).items()))
        cache_key = (path, normalized_params)
        cached_payload = self._cache.get(cache_key)
        if cached_payload is not None:
            self._emit("api_cache_hit", path=path, params=dict(normalized_params))
            return cached_payload

        query = ""
        if params:
            query = "?" + "&".join(f"{quote(key)}={quote(value)}" for key, value in params.items())
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-onboarding-agent/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.api_base}{path}{query}", headers=headers, method="GET")

        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw_response = response.read()
                try:
                    payload = json.loads(raw_response.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._emit("api_error", path=path, error_type="invalid_json")
                    raise GitHubAPIError("GitHub API returned an invalid JSON response.") from error

                self._cache.put(cache_key, payload, self._cache_ttl(path))
                self._emit("api_request", path=path, params=dict(normalized_params), attempt=attempt + 1)
                return copy.deepcopy(payload)
            except HTTPError as error:
                if self._is_retryable_status(error.code) and attempt < self.max_retries:
                    delay = self._retry_delay(attempt, error.headers.get("Retry-After"))
                    self._emit(
                        "api_retry",
                        path=path,
                        reason=f"http_{error.code}",
                        attempt=attempt + 1,
                        delay_seconds=delay,
                    )
                    self._sleep(delay)
                    continue
                detail = error.read().decode("utf-8", errors="replace")
                self._emit("api_error", path=path, error_type="http", status_code=error.code)
                raise GitHubAPIError(f"GitHub API returned HTTP {error.code}: {detail}") from error
            except URLError as error:
                if attempt < self.max_retries:
                    delay = self._retry_delay(attempt)
                    self._emit(
                        "api_retry",
                        path=path,
                        reason="network",
                        attempt=attempt + 1,
                        delay_seconds=delay,
                    )
                    self._sleep(delay)
                    continue
                self._emit("api_error", path=path, error_type="network")
                raise GitHubAPIError(f"Could not reach the GitHub API: {error.reason}") from error

        raise AssertionError("GET retry loop exited unexpectedly.")

    def _cache_ttl(self, path: str) -> float:
        if path.startswith("/search/"):
            return min(self.cache_ttl_seconds, 120.0)
        if path.endswith("/issues") or path.endswith("/pulls"):
            return min(self.cache_ttl_seconds, 60.0)
        return self.cache_ttl_seconds

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return self.backoff_seconds * (2**attempt)

    def _emit(self, event: str, **data: Any) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event, data)
        except Exception:
            # Observability must not interfere with repository retrieval.
            return

    def repository(self, repo: RepositoryRef) -> dict[str, Any]:
        return self.get(f"/repos/{quote(repo.owner)}/{quote(repo.name)}")

    def languages(self, repo: RepositoryRef) -> dict[str, int]:
        return self.get(f"/repos/{quote(repo.owner)}/{quote(repo.name)}/languages")

    def tree(self, repo: RepositoryRef, ref: str | None = None) -> list[dict[str, Any]]:
        branch = ref or self.repository(repo)["default_branch"]
        payload = self.get(
            f"/repos/{quote(repo.owner)}/{quote(repo.name)}/git/trees/{quote(branch, safe='')}?recursive=1"
        )
        if payload.get("truncated"):
            # The response is still useful, but callers should know it may be incomplete.
            return payload["tree"] + [{"path": "[tree truncated by GitHub]", "type": "notice"}]
        return payload["tree"]

    def search_code(
        self,
        repo: RepositoryRef,
        query: str,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Search code within a GitHub repository and validate its response schema."""
        clean_query = query.strip()

        if not clean_query:
            return []

        max_results = max(1, min(max_results, 100))

        payload = self.get(
            "/search/code",
            {
                "q": f"{clean_query} repo:{repo.slug}",
                "per_page": str(max_results),
            },
        )
        if not isinstance(payload, dict):
            raise GitHubAPIError("GitHub code search returned an unexpected JSON payload.")

        items = payload.get("items")
        if not isinstance(items, list):
            raise GitHubAPIError("GitHub code search response did not contain a list of items.")

        results: list[dict[str, Any]] = []
        for item in items[:max_results]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
                raise GitHubAPIError("GitHub code search returned an invalid result item.")
            results.append(item)

        return results


    def file_text(self, repo: RepositoryRef, path: str, ref: str | None = None) -> str:
        clean_path = path.strip("/")
        if not clean_path or ".." in clean_path.split("/"):
            raise ValueError("File paths must stay within the repository.")
        params = {"ref": ref} if ref else None
        payload = self.get(
            f"/repos/{quote(repo.owner)}/{quote(repo.name)}/contents/{quote(clean_path, safe='/')}", params
        )
        if payload.get("type") != "file":
            raise GitHubAPIError(f"{path!r} is not a file.")
        if payload.get("encoding") != "base64":
            raise GitHubAPIError(f"Unsupported GitHub content encoding for {path!r}.")
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")

    def readme(self, repo: RepositoryRef) -> str:
        payload = self.get(f"/repos/{quote(repo.owner)}/{quote(repo.name)}/readme")
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")

    def issues(self, repo: RepositoryRef, limit: int = 10) -> list[dict[str, Any]]:
        items = self.get(
            f"/repos/{quote(repo.owner)}/{quote(repo.name)}/issues",
            {"state": "open", "sort": "updated", "direction": "desc", "per_page": str(min(limit, 100))},
        )
        return [item for item in items if "pull_request" not in item]

    def pull_requests(self, repo: RepositoryRef, limit: int = 10) -> list[dict[str, Any]]:
        return self.get(
            f"/repos/{quote(repo.owner)}/{quote(repo.name)}/pulls",
            {"state": "open", "sort": "updated", "direction": "desc", "per_page": str(min(limit, 100))},
        )

