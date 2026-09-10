"""Deterministic Markdown report generation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .github_api import GitHubClient, RepositoryRef


IMPORTANT_FILES = ("README.md", "pyproject.toml", "package.json", "requirements.txt", "docker-compose.yml", "Dockerfile")


def _bullets(items: list[str], fallback: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {fallback}"


def build_report(client: GitHubClient, repo: RepositoryRef, max_files: int = 200) -> str:
    meta = client.repository(repo)
    languages = client.languages(repo)
    tree = client.tree(repo, meta["default_branch"])
    files = [entry["path"] for entry in tree if entry.get("type") == "blob"]
    directories = sorted({path.split("/", 1)[0] for path in files if "/" in path})
    issues = client.issues(repo)
    pulls = client.pull_requests(repo)
    try:
        readme = client.readme(repo)
    except Exception as error:  # A missing README should not stop onboarding.
        readme = f"README unavailable: {error}"

    key_files = [path for path in IMPORTANT_FILES if path in files]
    language_text = ", ".join(f"{name} ({bytes_ // 1024} KiB)" for name, bytes_ in languages.items()) or "Not reported"
    issue_lines = [f"#{item['number']} {item['title']}" for item in issues]
    pr_lines = [f"#{item['number']} {item['title']}" for item in pulls]
    visible_files = files[:max_files]
    truncation_note = "\n\n_File list truncated by --max-files._" if len(files) > max_files else ""

    return f"""# Onboarding report: {repo.slug}

Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from GitHub's read-only API.

## Project at a glance

- **Description:** {meta.get('description') or 'No description provided.'}
- **Default branch:** `{meta['default_branch']}`
- **Visibility:** {'private' if meta.get('private') else 'public'}
- **License:** {(meta.get('license') or {}).get('spdx_id') or 'Not specified'}
- **Stars / forks:** {meta.get('stargazers_count', 0)} / {meta.get('forks_count', 0)}
- **Primary languages:** {language_text}

## Suggested onboarding route

1. Read `README.md` and the files below marked as key project configuration.
2. Inspect the top-level source directories before changing behavior.
3. Review recent open issues and pull requests for current work and conventions.

### Key configuration files

{_bullets([f'`{path}`' for path in key_files], 'No common configuration file was found at the repository root.')}

### Top-level directories

{_bullets([f'`{directory}/`' for directory in directories], 'No nested source directories were detected.')}

## README preview

```text
{readme[:4000]}
```

## Recently updated open issues

{_bullets(issue_lines, 'No open issues returned.')}

## Open pull requests

{_bullets(pr_lines, 'No open pull requests returned.')}

## Repository file map

{_bullets([f'`{path}`' for path in visible_files], 'No files returned.')}{truncation_note}
"""


def save_report(markdown: str, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    return output

