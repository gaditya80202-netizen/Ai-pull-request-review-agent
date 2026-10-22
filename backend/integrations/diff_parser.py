from __future__ import annotations

import re
from collections import defaultdict


_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@"
)


def parse_diff(diff: str) -> dict[str, set[int]]:
    """Return added line numbers for each file in a unified diff.

    Line numbers refer to the new version of each file, which is
    the coordinate system required for GitHub's RIGHT-side comments.
    Files with no added lines are omitted.
    """
    changed: dict[str, set[int]] = defaultdict(set)

    current_path: str | None = None
    new_line: int | None = None
    in_hunk = False

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            new_line = None
            in_hunk = False
            continue

        # File headers appear outside a hunk. Inside a hunk, a source
        # line can itself begin with "+++", so don't treat it as a header.
        if not in_hunk and line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0]

            if path == "/dev/null":
                current_path = None
            elif path.startswith("b/"):
                current_path = path[2:]
            else:
                current_path = path

            continue

        match = _HUNK_HEADER.match(line)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            continue

        if not in_hunk or new_line is None or current_path is None:
            continue

        if line.startswith("\\"):
            # For example: "\ No newline at end of file"
            continue

        if line.startswith("+"):
            changed[current_path].add(new_line)
            new_line += 1
        elif line.startswith("-"):
            # Deletions occupy no line in the new version.
            continue
        elif line.startswith(" "):
            # Context exists in both versions.
            new_line += 1

    return {
        path: line_numbers
        for path, line_numbers in changed.items()
        if line_numbers
    }
