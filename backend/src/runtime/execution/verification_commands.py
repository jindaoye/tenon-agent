"""Recognize common checks and conservatively compare simple pytest scopes.

This inspects command text without executing or expanding shell expressions.
Unrecognized options and compound commands have no inferred coverage relation.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

_SHELL_VALUE = r"(?:[^\s;&|\"'\\]|\\[^\r\n]|\"(?:\\.|[^\"\\])*\"|'[^']*')*"
_ENV_PREFIX = (
    r"(?:env\s+(?:(?:-i|--ignore-environment)\s+|(?:-u|--unset)\s+[\w]+\s+|--unset=[\w]+\s+)*"
    r"(?:--\s+)?)?"
    rf"(?:[A-Za-z_][A-Za-z_0-9]*={_SHELL_VALUE}\s+)*"
)
_CHECK_COMMAND = re.compile(
    r"(?:^|[;&|\r\n])\s*(?:&\s*)?" + _ENV_PREFIX + r"(?:(?:uv|poetry)\s+run\s+|npx\s+)?"
    r"(?:(?:[\"'](?:[^\"'\r\n]*[\\/])?python[\d.]*(?:\.exe)?[\"']|"
    r"(?:[^\s\"']*[\\/])?python[\d.]*(?:\.exe)?)\s+-m\s+)?"
    r"(?P<check>(?:pytest|unittest|mypy|tsc|py_compile|compileall)\b|"
    r"(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:test(?::[\w-]+)?|typecheck|lint|build)\b|"
    r"(?:cargo|go|dotnet)\s+(?:test|check|build)\b|"
    r"ruff\s+(?:check|format\b[^\n]*--check)\b|node\s+--check\b|"
    r"(?:gcc|g\+\+|clang|clang\+\+|rustc)(?=\s|$)(?!\s+(?:--version|--help|-v)\s*$))",
    re.IGNORECASE,
)
_DISPLAY_FLAGS = frozenset(
    {"--quiet", "--verbose", "--disable-warnings", "--no-header", "--no-summary", "--showlocals", "-x"}
)


def is_verification_command(command: str) -> bool:
    # Split only at unquoted shell separators, so e.g. echo 'text; pytest'
    # does not count as actually invoking pytest. This is not a shell parser.
    start = 0
    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
        elif char in {"\\", "`"} and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char in ";&|\r\n":
            if _CHECK_COMMAND.match(command[start:index]):
                return True
            start = index + 1
    return _CHECK_COMMAND.match(command[start:]) is not None


def is_observation_command(command: str) -> bool:
    """Recognize only simple reads; this is not a shell security boundary."""
    if any(char in command for char in "\r\n;&|<>`$"):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    if parts[0] in {"cat", "head", "tail", "ls", "pwd", "wc", "grep"}:
        return True
    if parts[0] == "sed" and len(parts) >= 4 and parts[1] == "-n":
        return re.fullmatch(r"\d+(?:,\d+)?p", parts[2]) is not None
    if parts[0] == "rg":
        return not any(item.startswith(("--pre", "--hostname-bin")) for item in parts[1:])
    if parts[0] == "git" and len(parts) >= 2:
        return parts[1] in {"diff", "status", "show", "log"} and not any(
            item.startswith(("--output", "--ext-diff", "--textconv")) for item in parts[2:]
        )
    return False


@dataclass(frozen=True)
class PytestScope:
    launcher: tuple[str, ...]
    targets: tuple[str, ...]
    filters: tuple[str, ...]

    def covers(self, previous: PytestScope) -> bool:
        # Environment values and launchers must remain identical. Dropping a
        # -k/-m filter broadens a check; adding/changing one cannot prove a fix.
        if self.launcher != previous.launcher or self.filters and self.filters != previous.filters:
            return False
        if not self.targets or not previous.targets:
            # Default discovery may use configured testpaths, unlike pytest .
            return self.targets == previous.targets
        # An explicit file can cover its selected nodes. Do not infer that a
        # directory/default discovery includes explicitly selected files.
        return all(
            any(target == old or old.startswith(target + "::") for target in self.targets) for old in previous.targets
        )


def pytest_scope(command: str) -> PytestScope | None:
    """Infer coverage only for simple pytest invocations with known options."""

    if any(char in command for char in "\r\n;&|<>`\\$"):
        return None
    match = _CHECK_COMMAND.match(command)
    if match is None or match.group("check").casefold() != "pytest":
        return None
    try:
        launcher = tuple(shlex.split(command[: match.end("check")]))
        args = shlex.split(command[match.end("check") :])
    except ValueError:
        return None
    targets: list[str] = []
    filters: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-k", "-m", "--keyword", "--markexpr", "--maxfail", "--tb", "--color"}:
            if index + 1 == len(args):
                return None
            value = args[index + 1]
            if arg in {"-k", "-m", "--keyword", "--markexpr"}:
                filters.extend(("-k" if arg in {"-k", "--keyword"} else "-m", value))
            index += 2
            continue
        if arg.startswith(("--keyword=", "--markexpr=")):
            name, value = arg.split("=", 1)
            filters.extend(("-k" if name == "--keyword" else "-m", value))
        elif arg in _DISPLAY_FLAGS or re.fullmatch(r"-[qvs]+|-r\w+", arg):
            pass
        elif arg.startswith(("--tb=", "--color=", "--maxfail=")):
            pass
        elif arg.startswith("-"):
            # Includes collection-only, cached selection, config overrides,
            # ignored paths, deselection and plugins with unknown semantics.
            return None
        else:
            targets.append(arg.removeprefix("./").rstrip("/") or ".")
        index += 1
    return PytestScope(launcher, tuple(targets), tuple(filters))
