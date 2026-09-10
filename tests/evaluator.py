from __future__ import annotations


def normalize(text: str) -> str:
    return text.lower().replace("\\", "/")


def evaluate_keywords(
    answer: str,
    expected_keywords: list[str],
) -> dict:

    answer = normalize(answer)

    matched = []
    missing = []

    for keyword in expected_keywords:
        if keyword.lower() in answer:
            matched.append(keyword)
        else:
            missing.append(keyword)

    if not expected_keywords:
        score = 1.0
    else:
        score = len(matched) / len(expected_keywords)

    return {
        "score": score,
        "matched": matched,
        "missing": missing,
    }


def evaluate_files(
    answer: str,
    expected_files: list[str],
) -> dict:

    answer = normalize(answer)

    matched = []
    missing = []

    for file_path in expected_files:

        if normalize(file_path) in answer:
            matched.append(file_path)
        else:
            missing.append(file_path)

    if not expected_files:
        score = 1.0
    else:
        score = len(matched) / len(expected_files)

    return {
        "score": score,
        "matched": matched,
        "missing": missing,
    }


def evaluate_case(
    test_case: dict,
    answer: str,
) -> dict:

    keyword_result = evaluate_keywords(
        answer,
        test_case.get("expected_keywords", []),
    )

    file_result = evaluate_files(
        answer,
        test_case.get("expected_files", []),
    )

    if test_case.get("expected_files"):

        final_score = (
            keyword_result["score"] * 0.5
            + file_result["score"] * 0.5
        )

    else:

        final_score = keyword_result["score"]

    return {
        "keyword_score": keyword_result["score"],
        "file_score": file_result["score"],
        "final_score": final_score,
        "keywords": keyword_result,
        "files": file_result,
    }