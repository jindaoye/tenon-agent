"""Bounded excerpts and observed test changes from existing tool results only."""

from __future__ import annotations

import re

from backend.domain import redact_sensitive_text

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DIAGNOSTIC = re.compile(
    r"^\s*(?:FAILED\s|ERROR\s|E\s|(?:Assertion|Type|Attribute|Value|Import|ModuleNotFound)Error:|"
    r"Traceback \(most recent call last\):)|\b[1-9]\d*\s+(?:failed|errors?|failing)\b"
)
_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)(?:test_[^/]+|[^/]+[._](?:test|spec)\.[^/]+)$")


def failure_excerpt(output: object) -> tuple[str, ...]:
    if not isinstance(output, str):
        return ()
    lines = _ANSI.sub("", output).splitlines()
    # Preserve actionable assertions near the beginning and test IDs in the
    # final summary. Tool output is untrusted data, never executable guidance.
    matches = [line.strip() for line in [*lines[:2000], *lines[-200:]] if _DIAGNOSTIC.search(line)]
    unique = list(dict.fromkeys(matches))
    selected = unique[:5] + unique[-3:] if len(unique) > 8 else unique
    return tuple(redact_sensitive_text(line)[:240] for line in selected)


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


def changed_existing_tests(output: object) -> list[tuple[str, str]]:
    """Find removed test code in an observed Git diff, without reading files.

    A change is a reason to inspect provenance, not proof a test was weakened.
    Purely additive tests, whitespace and comment-only deletions do not qualify.
    """
    if not isinstance(output, str):
        return []
    path = ""
    changes: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith("diff --git "):
            path = ""
        elif line.startswith("--- a/"):
            path = line[6:]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif path and is_test_path(path) and line.startswith("-") and not line.startswith("---"):
            removed = line[1:].strip()
            if removed and not removed.startswith(("#", "//", "/*", "*")):
                changes.setdefault(path, redact_sensitive_text(removed)[:240])
    return list(changes.items())
