from __future__ import annotations

import re
from dataclasses import dataclass

from backend.security.masking import mask_secrets


PYTHON_SYMBOL = re.compile(
    r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"
)


@dataclass(frozen=True)
class CodeChunk:
    path: str
    symbol: str | None
    chunk_index: int
    start_line: int
    end_line: int
    content: str


def split_text_into_chunks(
    path: str,
    text: str,
    *,
    chunk_lines: int = 60,
    overlap_lines: int = 10,
    max_chunk_chars: int = 8_000,
) -> list[CodeChunk]:
    """Mask recognizable secrets and split text into overlapping chunks."""
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError("A relative repository path is required.")

    if (
        chunk_lines < 1
        or overlap_lines < 0
        or overlap_lines >= chunk_lines
        or max_chunk_chars < 200
    ):
        raise ValueError("Invalid chunking configuration.")

    masked = mask_secrets(text)
    lines = masked.splitlines()

    if not lines:
        return []

    chunks: list[CodeChunk] = []
    start = 0

    while start < len(lines):
        end = min(start + chunk_lines, len(lines))

        def render_chunk(stop: int) -> str:
            header = (
                f"# File: {path}\n"
                f"# Lines: {start + 1}-{stop}\n"
            )
            return header + "\n".join(lines[start:stop])

        # Respect the character cap without losing source lines.
        while end > start and len(render_chunk(end)) > max_chunk_chars:
            end -= 1

        if end == start:
            raise ValueError(
                f"Line {start + 1} in {path} exceeds the chunk-size limit."
            )

        symbol = None
        for index in range(start, -1, -1):
            match = PYTHON_SYMBOL.match(lines[index])
            if match:
                symbol = match.group(1)
                break

        chunks.append(
            CodeChunk(
                path=path,
                symbol=symbol,
                chunk_index=len(chunks),
                start_line=start + 1,
                end_line=end,
                content=render_chunk(end),
            )
        )

        if end == len(lines):
            break

        start = max(start + 1, end - overlap_lines)

    return chunks
