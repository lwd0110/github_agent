# GitHub Onboarding Agent

A read-only GitHub repository analyst built with [smolagents](https://github.com/huggingface/smolagents). Give it a public GitHub repository URL to produce an onboarding report or answer questions about the project.

## What it does

- Fetches repository metadata, languages, README, file tree, open issues, and pull requests through the GitHub REST API.
- Produces a deterministic Markdown onboarding report without an LLM.
- Uses a `smolagents.ToolCallingAgent` for natural-language repository questions. The agent only receives read-only tools.
- Never creates issues, comments, commits, branches, releases, or pull requests.

## Setup

```bash
python -m venv .venv
.venv\\Scripts\\activate       # Windows PowerShell
pip install -e .
copy .env.example .env
```

Set `GITHUB_TOKEN` in your shell or `.env` loader of choice to raise the GitHub API rate limit and access repositories that token is allowed to read. The current MVP does not load `.env` automatically, so you can use PowerShell:

```powershell
$env:GITHUB_TOKEN = "github_pat_..."
$env:HF_TOKEN = "hf_..."  # needed only for `ask`
```

## Usage

Create a report (works for public repositories without an LLM key):

```bash
github-agent report https://github.com/huggingface/smolagents
```

Ask a natural-language question (requires `HF_TOKEN`):

```bash
github-agent ask https://github.com/huggingface/smolagents "How is a CodeAgent executed, and what security constraints matter?"
```

Limit or redirect output:

```bash
github-agent report https://github.com/OWNER/REPO --output reports/onboarding.md --max-files 150
```

## Safety model

This is deliberately a read-only first version. GitHub credentials are sent only to `api.github.com`; they are never printed, stored, or sent to the model. Repository content returned to the LLM can contain untrusted instructions, so treat the answer as analysis rather than authority. Before adding any write action, use a separate least-privilege token and implement a preview plus explicit confirmation step.

