"""Short-lived retrieval memory for one repository-analysis agent run."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$]\w*)", re.MULTILINE),
    re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$]\w*)", re.MULTILINE),
)
_MAX_SYMBOLS = 12


@dataclass(frozen=True)
class FileMemory:
    """Cached content and deterministic summary for one repository file."""

    path: str
    content: str
    character_count: int
    line_count: int
    symbols: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        """Return compact metadata suitable for the model context."""
        return {
            "path": self.path,
            "character_count": self.character_count,
            "line_count": self.line_count,
            "symbols": list(self.symbols),
        }


class RepositoryMemory:
    """Cache file reads and expose compact summaries within one agent lifetime."""

    def __init__(self) -> None:
        self._files: OrderedDict[str, FileMemory] = OrderedDict()

    def read_or_get(self, path: str, loader: Callable[[], str]) -> tuple[FileMemory, bool]:
        """Load a file once and return whether the cached copy was reused."""
        clean_path = path.strip("/")
        if clean_path in self._files:
            return self._files[clean_path], True

        content = loader()
        record = FileMemory(
            path=clean_path,
            content=content,
            character_count=len(content),
            line_count=len(content.splitlines()),
            symbols=_extract_symbols(content),
        )
        self._files[clean_path] = record
        return record, False

    def summaries(self, path: str = "") -> list[dict[str, object]]:
        """Return one or all compact file summaries without returning full content."""
        clean_path = path.strip("/")
        if clean_path:
            record = self._files.get(clean_path)
            return [record.summary()] if record else []
        return [record.summary() for record in self._files.values()]


def _extract_symbols(content: str) -> tuple[str, ...]:
    symbols: list[str] = []
    for pattern in _SYMBOL_PATTERNS:
        for match in pattern.finditer(content):
            symbol = match.group(1)
            if symbol not in symbols:
                symbols.append(symbol)
                if len(symbols) == _MAX_SYMBOLS:
                    return tuple(symbols)
    return tuple(symbols)
