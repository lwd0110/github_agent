"""smolagents integration for read-only repository questions."""

from __future__ import annotations

import json
import os

from .github_api import GitHubClient, RepositoryRef

def build_agent(client: GitHubClient, repo: RepositoryRef):
    """Create a ToolCallingAgent with only schema-validated read operations."""
    try:
        from smolagents import OpenAIModel, ToolCallingAgent, tool
    except ImportError as error:
        raise RuntimeError("Install dependencies first: pip install -e .") from error

    @tool
    def repository_overview() -> str:
        """Return repository metadata and language usage for the configured repository.

        Args:
            None.
        """
        data = client.repository(repo)
        data["languages"] = client.languages(repo)
        return json.dumps(data, ensure_ascii=False)

    @tool
    def list_repository_files(prefix: str = "") -> str:
        """List repository files, optionally restricted to a directory prefix.

        Args:
            prefix: Optional folder prefix such as `src/`; leave empty for all files.
        """
        safe_prefix = prefix.strip("/")
        if ".." in safe_prefix.split("/"):
            return "Invalid prefix."
        paths = [entry["path"] for entry in client.tree(repo) if entry.get("type") == "blob"]
        if safe_prefix:
            paths = [path for path in paths if path.startswith(safe_prefix + "/") or path == safe_prefix]
        return "\n".join(paths[:300]) or "No matching files."

    @tool
    def read_repository_file(path: str) -> str:
        """Read a UTF-8 text file from the configured repository. Large responses are shortened.

        Args:
            path: Repository-relative file path, for example `src/main.py`.
        """
        return client.file_text(repo, path)[:12000]

    @tool
    def list_open_issues() -> str:
        """Return the ten most recently updated open issues, excluding pull requests.

        Args:
            None.
        """
        return json.dumps(client.issues(repo), ensure_ascii=False)

    @tool
    def list_open_pull_requests() -> str:
        """Return the ten most recently updated open pull requests.

        Args:
            None.
        """
        return json.dumps(client.pull_requests(repo), ensure_ascii=False)
    
    """
    model = InferenceClientModel(
        model_id=os.getenv("HF_MODEL_ID", "Qwen/Qwen3-Next-80B-A3B-Thinking"),
        token=os.getenv("HF_TOKEN"),
        temperature=0.2,
    )"""

    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("DASHSCOPE_BASE_URL")

    model = OpenAIModel(
    model_id=os.getenv("DASHSCOPE_MODEL", "qwen-plus"),
    api_base=base_url,
    api_key=api_key,
    temperature=0.2,)

    return ToolCallingAgent(
        tools=[repository_overview, list_repository_files, read_repository_file, list_open_issues, list_open_pull_requests],
        model=model,
        max_steps=8,
        name="github_repository_analyst",
        description="Answers questions about one configured GitHub repository using read-only API tools.",
    )

