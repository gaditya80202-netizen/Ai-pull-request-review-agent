from __future__ import annotations

import re

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r".*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

_TOKEN_PATTERNS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
]

_QUOTED_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|
        password|passwd|secret|token)\b\s*[:=]\s*)
    (["'])
    (.{8,}?)
    \2
    """
)

_UNQUOTED_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|
        password|passwd|secret)\b\s*[:=]\s*)
    ([A-Za-z0-9_/\-+=]{20,})
    """
)


def _mask_preserving_newlines(value: str) -> str:
    """Replace characters without changing the number of newline characters."""
    return "".join("\n" if char == "\n" else "X" for char in value)


def mask_secrets(text: str) -> str:
    """Best-effort masking of recognizable secrets before external processing.

    This is not a complete secret detector. It preserves line breaks so
    code line numbering remains stable.
    """
    text = _PRIVATE_KEY.sub(
        lambda match: _mask_preserving_newlines(match.group(0)),
        text,
    )

    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)

    text = _QUOTED_SECRET_ASSIGNMENT.sub(
        lambda match: (
            match.group(1)
            + match.group(2)
            + "[REDACTED]"
            + match.group(2)
        ),
        text,
    )

    text = _UNQUOTED_SECRET_ASSIGNMENT.sub(
        lambda match: match.group(1) + "[REDACTED]",
        text,
    )

    return text
