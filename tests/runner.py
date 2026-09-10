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

from evaluator import evaluate_case


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


for index, test_case in enumerate(dataset, start=1):

    question = test_case["question"]

    print("\n")
    print("=" * 80)
    print(f"TEST {index}/{len(dataset)}")
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
    # Evaluate
    # --------------------------------------------------------

    evaluation = evaluate_case(
        test_case=test_case,
        answer=answer,
    )

    print("\nEvaluation:")

    print(
        f"Keyword Score : "
        f"{evaluation['keyword_score']:.2%}"
    )

    print(
        f"File Score    : "
        f"{evaluation['file_score']:.2%}"
    )

    print(
        f"Final Score   : "
        f"{evaluation['final_score']:.2%}"
    )

    results.append(
        {
            "id": test_case["id"],
            "category": test_case["category"],
            "question": question,
            "answer": answer,
            "evaluation": evaluation,
        }
    )


# ============================================================
# Overall score
# ============================================================

if results:

    overall_score = (
        sum(
            result["evaluation"]["final_score"]
            for result in results
        )
        / len(results)
    )

else:

    overall_score = 0.0


# ============================================================
# Save results
# ============================================================

result_path = ROOT / "tests" / "results.json"

with open(
    result_path,
    "w",
    encoding="utf-8",
) as f:

    json.dump(
        {
            "repository": f"{OWNER}/{REPOSITORY}",
            "total_questions": len(results),
            "overall_score": overall_score,
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
print("EVALUATION SUMMARY")
print("=" * 80)

print(
    f"Repository    : {OWNER}/{REPOSITORY}"
)

print(
    f"Questions     : {len(results)}"
)

print(
    f"Overall Score : {overall_score:.2%}"
)

print(
    f"Results       : {result_path}"
)