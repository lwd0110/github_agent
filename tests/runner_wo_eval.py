from __future__ import annotations

import json
import sys
from pathlib import Path


# ============================================================
# Project root
# ============================================================

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))


# ============================================================
# Import your Agent
# ============================================================

from github_agent.agent import build_agent
from github_agent.github_api import GitHubClient, RepositoryRef


# ============================================================
# Repository
# ============================================================

OWNER = "huggingface"
REPOSITORY = "smolagents"


# ============================================================
# Create GitHub client
# ============================================================

client = GitHubClient()


# ============================================================
# Repository reference
# ============================================================

repo = RepositoryRef(
    owner=OWNER,
    name=REPOSITORY,
)


# ============================================================
# Create Agent
# ============================================================

agent = build_agent(
    client=client,
    repo=repo,
)


# ============================================================
# Load dataset
# ============================================================

dataset_path = ROOT / "tests" / "test_dataset.json"

with open(
    dataset_path,
    "r",
    encoding="utf-8",
) as f:
    dataset = json.load(f)


# ============================================================
# Run tests
# ============================================================

results = []

questions = dataset["questions"]
for index, test_case in enumerate(questions, start=1):

    question = test_case["question"]

    print("\n")
    print("=" * 80)
    print(f"TEST {index}/{len(questions)}")
    print("=" * 80)

    print("\nQuestion:")
    print(question)

    # --------------------------------------------------------
    # Run Agent
    # --------------------------------------------------------

    try:

        answer = agent.run(question)
        answer = str(answer)

    except Exception as error:

        print("\nAgent Error:")
        print(error)

        answer = ""

    # --------------------------------------------------------
    # Print Answer
    # --------------------------------------------------------

    print("\nAgent Answer:")
    print(answer)

    # --------------------------------------------------------
    # Save result
    # --------------------------------------------------------

    results.append(
        {
            "id": test_case["id"],
            "category": test_case.get("category"),
            "difficulty": test_case.get("difficulty"),
            "question": question,
            "answer": answer,
        }
    )


# ============================================================
# Save results
# ============================================================

result_path = ROOT / "tests" / "results_wo_eval.json"

with open(
    result_path,
    "w",
    encoding="utf-8",
) as f:

    json.dump(
        {
            "repository": f"{OWNER}/{REPOSITORY}",
            "total_questions": len(results),
            "results": results,
        },
        f,
        indent=2,
        ensure_ascii=False,
    )


# ============================================================
# Summary
# ============================================================

print("\n")
print("=" * 80)
print("TEST SUMMARY")
print("=" * 80)

print(
    f"Repository : {OWNER}/{REPOSITORY}"
)

print(
    f"Questions  : {len(results)}"
)

print(
    f"Results    : {result_path}"
)