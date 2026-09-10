"""Small, dependency-free client for the GitHub REST API."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class GitHubAPIError(RuntimeError):
    """Raised when GitHub cannot satisfy a read-only request."""


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

    def __init__(self, token: str | None = None, timeout: int = 20) -> None:
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.timeout = timeout

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
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
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(f"GitHub API returned HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise GitHubAPIError(f"Could not reach the GitHub API: {error.reason}") from error

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

