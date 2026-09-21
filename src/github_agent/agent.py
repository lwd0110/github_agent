"""smolagents integration for read-only repository questions."""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from .evidence import EvidenceLedger
from .github_api import GitHubClient, RepositoryRef
from .repository_memory import RepositoryMemory
from .trace import TraceRecorder


class CodeSearchCache:
    """Deduplicate normalized code searches during one agent lifetime."""

    def __init__(self) -> None:
        self._results: dict[tuple[str, int], list[dict[str, object]]] = {}

    def get_or_search(
        self,
        query: str,
        max_results: int,
        search: Callable[[], list[dict[str, object]]],
    ) -> tuple[list[dict[str, object]], bool]:
        """Return de-duplicated results and whether the cached value was used."""
        key = (query, max_results)
        if key in self._results:
            return self._results[key], True

        unique_results: list[dict[str, object]] = []
        seen_paths: set[str] = set()
        for item in search():
            path = item.get("path")
            if not isinstance(path, str) or path in seen_paths:
                continue
            seen_paths.add(path)
            unique_results.append(item)

        self._results[key] = unique_results
        return unique_results, False


def format_code_search_evidence(
    query: str,
    matches: list[dict[str, object]],
    *,
    cached: bool,
    citation: str,
) -> str:
    """Produce a stable, JSON-encoded observation for a code-search tool call."""
    results = [
        {
            "path": item["path"],
            "repository": item.get("repository", {}).get("full_name", "")
            if isinstance(item.get("repository"), dict)
            else "",
            "url": item.get("html_url", ""),
        }
        for item in matches
    ]
    data: dict[str, object] = {
        "evidence_type": "code_search",
        "citation": citation,
        "query": query,
        "cache": "hit" if cached else "miss",
        "match_count": len(results),
        "matches": results,
    }
    if not results:
        data["result"] = "no_matches"
        data["note"] = "No matching code was found; this does not prove the repository lacks the term."

    return json.dumps(data, ensure_ascii=False, indent=2)


def build_agent(
    client: GitHubClient,
    repo: RepositoryRef,
    trace: TraceRecorder | None = None,
):
    """Create a ToolCallingAgent with only schema-validated read operations."""
    try:
        from smolagents import OpenAIModel, ToolCallingAgent, tool
    except ImportError as error:
        raise RuntimeError("Install dependencies first: pip install -e .") from error

    code_search_cache = CodeSearchCache()
    evidence_ledger = EvidenceLedger()
    retrieval_memory = RepositoryMemory()

    @tool
    def repository_overview() -> str:
        """
        Get high-level metadata about the configured GitHub repository.

        Use this tool when the user asks about:
        - repository description
        - repository metadata
        - programming languages
        - stars, forks, visibility, or other repository-level information

        Do NOT use this tool to determine:
        - whether a specific dependency is used
        - whether a specific technology is implemented
        - what a specific source file contains
        - implementation details

        The returned information is repository evidence and may be used
        to support repository-specific factual claims.

        Args:
            None.
        """


        data = client.repository(repo)
        data["languages"] = client.languages(repo)
        citation = evidence_ledger.record("repository", repo.slug)

        return (
            "[REPOSITORY_EVIDENCE]\n"
            f"Citation: {citation}\n"
            "Evidence type: repository metadata\n"
            f"Repository: {repo.owner}/{repo.name}\n"
            "Data:\n"
            f"{json.dumps(data, ensure_ascii=False, indent=2)}"
        )

    @tool
    def list_repository_files(prefix: str = "") -> str:
        """
        List files in the configured GitHub repository.

        Use this tool when you need to:
        - discover repository structure
        - locate potentially relevant files
        - identify files under a directory

        Do NOT use this tool to determine what code or configuration
        a file contains. Use read_repository_file instead.

        File existence is evidence of file existence only. It does not
        prove the file's contents, behavior, or implementation.

        Args:
            prefix: Optional directory prefix such as `src/`;
            leave empty for all files.
        """

        safe_prefix = prefix.strip("/")

        if ".." in safe_prefix.split("/"):
            return "[EVIDENCE_NOT_FOUND]\nInvalid repository path prefix."

        citation = evidence_ledger.record("file_listing", safe_prefix or "/")

        paths = [
            entry["path"]
            for entry in client.tree(repo)
            if entry.get("type") == "blob"
        ]

        if safe_prefix:
            paths = [
                path
                for path in paths
                if path.startswith(safe_prefix + "/") or path == safe_prefix
            ]

        if not paths:
            return (
                "[REPOSITORY_EVIDENCE]\n"
                f"Citation: {citation}\n"
                "Evidence type: file listing\n"
                "Result: No matching files."
            )

        return (
            "[REPOSITORY_EVIDENCE]\n"
            f"Citation: {citation}\n"
            "Evidence type: file listing\n"
            f"Prefix: {safe_prefix or '/'}\n"
            "Files:\n"
            + "\n".join(paths[:300]))

    @tool
    def search_repository_code(
        query: str,
        max_results: int = 20,
    ) -> str:
        """
        Search for a keyword or code pattern in the configured GitHub repository.

        Use this tool when the user's question requires finding where a
        technology, dependency, database, framework, API, function, class,
        configuration key, or implementation pattern appears in the repository.

        The observation contains valid JSON with matching repository paths.
        Read distinct relevant paths with read_repository_file before making
        implementation claims. Do not repeat the same normalized query: its
        result, including an empty result, is cached for this agent run.

        Args:
            query: Keyword or code pattern to search for.
            max_results: Maximum number of matching files to return.
        """
        safe_query = query.strip()

        if not safe_query:
            return (
                "[EVIDENCE_NOT_FOUND]\n"
                "Code search query cannot be empty."
            )

        max_results = max(1, min(max_results, 50))
        citation = evidence_ledger.record("code_search", safe_query)
        matches, cached = code_search_cache.get_or_search(
            safe_query,
            max_results,
            lambda: client.search_code(repo, safe_query, max_results=max_results),
        )
        return format_code_search_evidence(
            safe_query,
            matches,
            cached=cached,
            citation=citation,
        )


    @tool
    def read_repository_file(path: str) -> str:
        """
        Read the contents of a specific repository file.

        Use this tool when the user's question requires evidence from:
        - source code
        - configuration
        - dependency declarations
        - documentation
        - tests
        - other repository files

        If the relevant file path is unknown, use list_repository_files
        first to discover candidate files.

        The returned content is direct repository evidence and may be used
        to support repository-specific factual claims.

        Args:
            path: Repository-relative file path, for example `src/main.py`.
        """


        file_memory, cached = retrieval_memory.read_or_get(
            path,
            lambda: client.file_text(repo, path),
        )
        content = file_memory.content

        if not content:
            return (
                "[EVIDENCE_NOT_FOUND]\n"
                f"File: {path}\n"
                "The requested file could not be read or contained no readable content."
            )

        citation = evidence_ledger.record("file", file_memory.path)
        if cached:
            return json.dumps(
                {
                    "memory_type": "repository_file_summary",
                    "cache": "hit",
                    "citation": citation,
                    "summary": file_memory.summary(),
                    "note": "The file was already read during this agent run; full content is not repeated."
                },
                ensure_ascii=False,
                indent=2,
            )

        return (
            "[REPOSITORY_EVIDENCE]\n"
            f"Citation: {citation}\n"
            "Evidence type: repository file content\n"
            f"File: {file_memory.path}\n"
            "Content:\n"
            f"{content[:12000]}"
        )

    @tool
    def inspect_retrieval_memory(path: str = "") -> str:
        """Return compact summaries of files already read during this agent run.

        Use this tool to plan follow-up retrieval without repeating file reads.
        The summaries are planning memory only, not new repository evidence;
        final claims must cite the original [evidence:file:...] token from a
        file-read result.

        Args:
            path: Optional repository-relative path of one previously read file.
        """
        summaries = retrieval_memory.summaries(path)
        if not summaries:
            return "[EVIDENCE_NOT_FOUND]\nNo matching file memory is available for this agent run."
        return json.dumps(
            {
                "memory_type": "repository_retrieval_memory",
                "files": summaries,
                "note": "Use this summary to plan retrieval; cite the original file evidence in final answers.",
            },
            ensure_ascii=False,
            indent=2,
        )

    @tool
    def list_open_issues() -> str:
        """
        Return the ten most recently updated open issues, excluding pull requests.

        Use this tool when the user asks about:
        - open GitHub issues
        - issue titles, descriptions, or status
        - recent issue activity

        Do NOT use this tool to answer questions about source code,
        dependencies, repository configuration, or implementation details.

        The returned issues are repository evidence and may be cited
        when making claims about repository issues.

        Args:
            None.
        """

        issues = client.issues(repo)
        citation = evidence_ledger.record("issues", repo.slug)

        return (
            "[REPOSITORY_EVIDENCE]\n"
            f"Citation: {citation}\n"
            "Evidence type: GitHub issues\n"
            f"Repository: {repo.owner}/{repo.name}\n"
            "Issues:\n"
            f"{json.dumps(issues, ensure_ascii=False, indent=2)}"
        )


    @tool
    def list_open_pull_requests() -> str:
        """
        Return the ten most recently updated open pull requests.

        Use this tool when the user asks about:
        - open pull requests
        - recent PR activity
        - pull request titles, descriptions, or status

        Do NOT use this tool to answer questions about source code,
        dependencies, repository configuration, or implementation details.

        The returned pull requests are repository evidence and may be cited
        when making claims about repository development activity.

        Args:
            None.
        """

        pull_requests = client.pull_requests(repo)
        citation = evidence_ledger.record("pull_requests", repo.slug)

        return (
            "[REPOSITORY_EVIDENCE]\n"
            f"Citation: {citation}\n"
            "Evidence type: GitHub pull requests\n"
            f"Repository: {repo.owner}/{repo.name}\n"
            "Pull requests:\n"
            f"{json.dumps(pull_requests, ensure_ascii=False, indent=2)}"
        )
    
    """
    model = InferenceClientModel(
        model_id=os.getenv("HF_MODEL_ID", "Qwen/Qwen3-Next-80B-A3B-Thinking"),
        token=os.getenv("HF_TOKEN"),
        temperature=0.2,
    )"""

    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("DASHSCOPE_BASE_URL")

    model = OpenAIModel(
    model_id=os.getenv("DASHSCOPE_MODEL", "qwen-plus-0112"),
    api_base=base_url,
    api_key=api_key,
    temperature=0.1,)

    repository_tools = [
    repository_overview,
    list_repository_files,
    search_repository_code,
    read_repository_file,
    inspect_retrieval_memory,]

    issue_pr_tools = [
        list_open_issues,
        list_open_pull_requests,
    ]

    all_tools = repository_tools + issue_pr_tools

    def trace_agent_step(step: object, agent: object) -> None:
        if trace is not None:
            trace.record_agent_step(step)

    def require_evidence_citations(final_answer: object, _memory: object, *, agent: object) -> bool:
        """Reject final answers that omit or invent evidence citations."""
        return evidence_ledger.validate_final_answer(final_answer)

    agent = ToolCallingAgent(
        tools=all_tools,
        model=model,
        max_steps=6,
        final_answer_checks=[require_evidence_citations],
        step_callbacks=[trace_agent_step] if trace is not None else None,
        name="github_repository_analyst",
        description=(
        # ============================================================
        # 1. Agent Role
        # Define the agent's overall purpose and scope.
        # ============================================================
        "Answers questions about one configured GitHub repository using "
        "read-only API tools. "

        # ============================================================
        # 2. Tool Routing
        # Tell the agent to select tools according to the question.
        # ============================================================
        "Select the most relevant tool based on the user's question. "

        # ------------------------------------------------------------
        # Repository metadata questions
        # Example: repository description, languages, stars, forks
        # Tool: repository_overview
        # ------------------------------------------------------------
        "Use repository_overview for high-level repository metadata, "
        "such as repository description, languages, stars, and forks. "

        # ------------------------------------------------------------
        # Repository structure questions
        # Example: find files, inspect repository structure
        # Tool: list_repository_files
        # ------------------------------------------------------------
        "Use list_repository_files when repository structure must be "
        "discovered or relevant files need to be located. "

        # ------------------------------------------------------------
        # Repository code search questions
        # Example: find where MongoDB, Redis, an API, class, or function
        # appears in the repository
        # Tool: search_repository_code
        # ------------------------------------------------------------
        "Use search_repository_code when the question requires finding "
        "a keyword, technology, dependency, database, framework, API, "
        "function, class, configuration key, or implementation pattern "
        "across the repository and the relevant file is unknown. "

        "Prefer search_repository_code over list_repository_files when "
        "the question is about whether a specific technology or "
        "implementation appears somewhere in the repository. "

        # ------------------------------------------------------------
        # File content questions
        # Example: inspect requirements.txt, pyproject.toml, source code
        # Tool: read_repository_file
        # ------------------------------------------------------------
        "Use read_repository_file when the answer requires the actual "
        "contents of a specific repository file. "

        "Use inspect_retrieval_memory to recall concise metadata for files "
        "already read in this run. It is for planning only; final claims "
        "must cite original repository evidence rather than a memory summary. "

        # ------------------------------------------------------------
        # GitHub Issue questions
        # Tool: list_open_issues
        # ------------------------------------------------------------
        "Use list_open_issues for questions about open GitHub issues. "

        # ------------------------------------------------------------
        # GitHub Pull Request questions
        # Tool: list_open_pull_requests
        # ------------------------------------------------------------
        "Use list_open_pull_requests for questions about open pull requests. "

        # ============================================================
        # 3. Technology / Implementation Verification
        # Important rule: do not rely only on filenames or metadata.
        # ============================================================
        "When a question asks whether a technology, dependency, database, "
        "framework, API, or implementation is used, inspect relevant "
        "repository files rather than relying only on repository metadata "
        "or file names. "

        # ------------------------------------------------------------
        # Code search results should be followed by file inspection
        # when actual implementation needs to be understood.
        # ------------------------------------------------------------
        "When search_repository_code identifies relevant files, use "
        "read_repository_file to inspect the actual implementation before "
        "concluding that the technology or feature is used. "

        "For questions spanning multiple components, follow references found "
        "in inspected files with a new, distinct code search and read the "
        "additional relevant files. Reuse prior search evidence instead of "
        "repeating the same query. Before rereading a file, inspect retrieval "
        "memory; repeated file reads return a compact summary, not full content. "

        "Do not treat a code search match alone as proof of actual usage. "
        "Distinguish between imports, comments, documentation, tests, "
        "configuration, and executable implementation code. "

        # ============================================================
        # 4. Tool Usage Constraint
        # Prevent unnecessary or unrelated tool calls.
        # ============================================================
        "Do not call unrelated tools. "

        # ============================================================
        # 5. Evidence Constraint
        # Repository-specific claims must be supported by retrieved evidence.
        # ============================================================
        "Repository-specific factual claims must be supported by retrieved "
        "repository evidence. Every final answer must include at least one "
        "exact [evidence:...] citation token returned by a repository tool. "
        "Never invent a citation, and cite each repository-specific claim "
        "with the evidence that supports it. "

        # ------------------------------------------------------------
        # Do not hallucinate repository-specific information.
        # ------------------------------------------------------------
        "The agent must not guess repository facts "
        "or fill missing information using general knowledge. "

        # ------------------------------------------------------------
        # If evidence is insufficient:
        # 1. Retrieve more repository evidence.
        # 2. If still insufficient, explicitly say it cannot be verified.
        # ------------------------------------------------------------
        "If the available evidence is insufficient, the agent "
        "should retrieve additional repository information or explicitly "
        "state that the information cannot be verified."),
    )
    agent.evidence_ledger = evidence_ledger
    agent.retrieval_memory = retrieval_memory
    return agent

