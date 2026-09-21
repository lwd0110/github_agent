"""Evidence tracking and answer validation for repository analysis."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


_CITATION_PATTERN = re.compile(r"\[evidence:([^\]\r\n]+)\]")
_UNVERIFIED_MARKERS = (
    "无法从仓库证据中验证",
    "无法从当前仓库证据中验证",
    "could not be verified from the repository",
)


class EvidenceValidationError(ValueError):
    """Raised when an answer lacks valid references to retrieved evidence."""


@dataclass(frozen=True)
class EvidenceRecord:
    """One source retrieved during the current agent run."""

    source_id: str
    kind: str
    reference: str

    @property
    def citation(self) -> str:
        return f"[evidence:{self.source_id}]"


class EvidenceLedger:
    """Track evidence sources and validate final-answer citations for one run."""

    def __init__(self) -> None:
        self._records: OrderedDict[str, EvidenceRecord] = OrderedDict()

    def record(self, kind: str, reference: str) -> str:
        """Register an evidence source and return its required citation token."""
        clean_kind = kind.strip().lower().replace(" ", "_")
        clean_reference = reference.strip().replace("]", "%5D")
        if not clean_kind or not clean_reference:
            raise ValueError("Evidence kind and reference must be non-empty.")

        source_id = f"{clean_kind}:{clean_reference}"
        self._records.setdefault(
            source_id,
            EvidenceRecord(source_id=source_id, kind=clean_kind, reference=clean_reference),
        )
        return self._records[source_id].citation

    def citations(self) -> tuple[str, ...]:
        """Return all citations available to the current answer."""
        return tuple(record.citation for record in self._records.values())

    def validate_final_answer(self, answer: Any) -> bool:
        """Require citations that were actually registered by repository tools."""
        text = str(answer).strip()
        if not text:
            raise EvidenceValidationError("Final answer is empty.")

        citations = {f"[evidence:{source_id}]" for source_id in _CITATION_PATTERN.findall(text)}
        if not self._records:
            if any(marker in text.lower() for marker in _UNVERIFIED_MARKERS):
                return True
            raise EvidenceValidationError(
                "No repository evidence was retrieved; state that the answer could not be verified from the repository."
            )

        if not citations:
            raise EvidenceValidationError(
                "Final answer must cite at least one retrieved source using an [evidence:...] token."
            )

        unknown_citations = citations.difference(self.citations())
        if unknown_citations:
            raise EvidenceValidationError(
                "Final answer cited sources that were not retrieved: " + ", ".join(sorted(unknown_citations))
            )
        return True

    def fallback_answer(self, validation_error: Exception) -> str:
        """Return a non-factual fallback instead of emitting an uncited answer."""
        available_citations = "\n".join(f"- {citation}" for citation in self.citations())
        if not available_citations:
            available_citations = "- No repository evidence was retrieved."
        return (
            "无法安全地给出仓库结论：最终回答未通过证据引用校验。\n"
            f"原因：{validation_error}\n\n"
            "已检索的证据引用：\n"
            f"{available_citations}\n\n"
            "请基于上述证据重新回答；若证据不足，请明确说明“无法从仓库证据中验证”。"
        )
