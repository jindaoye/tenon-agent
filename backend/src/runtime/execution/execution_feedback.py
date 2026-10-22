"""Advisory execution feedback, separate from completion and grading."""

from __future__ import annotations

import hashlib
import re
import shlex
from collections import Counter

from backend.domain import ToolMessage, UserMessage

from ..core.context import AgentRuntime
from .completion_review import _result
from .verification_commands import is_observation_command, is_verification_command
from .verification_diagnostics import failure_excerpt

PROGRESS_WINDOW = 12
REPEATED_OBSERVATIONS = 8
EXECUTION_FEEDBACK_HEADER = "[Execution feedback]"


def _command_segments(command: str) -> list[str]:
    """Inspect simple compound reads without executing or expanding shell code."""
    # Only inspect the shell header. A here-document body is script data, not
    # shell commands, and must never be mistaken for an additional read.
    command = command.split("\n", 1)[0].rstrip("\r")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        segments: list[list[str]] = [[]]
        for token in lexer:
            if token and all(char in ";&|" for char in token):
                segments.append([])
            else:
                segments[-1].append(token)
        return [shlex.join(parts) for parts in segments if parts]
    except ValueError:
        return []


def _read_segments(command: str) -> list[str]:
    segments = _command_segments(command)
    # Called only with a confirmed unchanged workspace. A trailing script may
    # add new output without changing the file excerpts read before it.
    return [segment for segment in segments if is_observation_command(segment)]


def _failure_identity(command: str, output: object) -> tuple[str, tuple[str, ...]]:
    checks = [segment for segment in _command_segments(command) if is_verification_command(segment)]
    excerpt = tuple(
        re.sub(r"\bin \d+(?:\.\d+)?(?:s| seconds?)\b", "in <duration>", line) for line in failure_excerpt(output)
    )
    return "; ".join(checks) or command.strip(), excerpt


def _observation_keys(tool: ToolMessage) -> set[tuple[str, str]]:
    observation = _observation(tool)
    if observation is not None:
        keys = {observation}
    else:
        keys = set()
    result = _result(tool)
    if (
        tool.name not in {"run_command", "container_exec"}
        or result.get("workspace_change") != "unchanged"
        or result.get("session_id") is not None
    ):
        return keys
    command = tool.arguments.get("command", tool.arguments.get("cmd", ""))
    if not isinstance(command, str):
        return keys
    reads = _read_segments(command)
    if len(reads) < 2:
        return keys
    # Stable blocks allow a repeated file excerpt to survive another excerpt
    # being appended to a compound command's output. Bound both CPU and memory.
    lines = str(result.get("output", ""))[:50000].splitlines()
    blocks = ["\n".join(lines[index : index + 8]) for index in range(max(1, len(lines) - 7))]
    blocks = [block for block in blocks if len(block.strip()) >= 80]
    if len(blocks) > 64:
        blocks = blocks[:16] + [blocks[16 + index * (len(blocks) - 17) // 47] for index in range(48)]
    for read in reads[:16]:
        for block in blocks:
            keys.add((read, hashlib.sha256(block.encode()).hexdigest()))
    return keys


def _repeated_observations(actions: list[ToolMessage]) -> bool:
    window: list[set[tuple[str, str]]] = []
    check_results: set[tuple[str, tuple[str, ...]]] = set()
    for tool in actions[-PROGRESS_WINDOW * 3 :]:
        result = _result(tool)
        if result.get("session_id") is not None or result.get("workspace_change") == "changed":
            window.clear()
            check_results.clear()
            continue
        command = tool.arguments.get("command", tool.arguments.get("cmd", ""))
        if isinstance(command, str) and is_verification_command(command):
            excerpt = failure_excerpt(result.get("output"))
            failed = tool.status == "failed" or result.get("exit_code") not in (None, 0) or bool(excerpt)
            # A new check result is progress. Repeating the same result on
            # unchanged files is not; ignore volatile test durations.
            check, details = _failure_identity(command, result.get("output"))
            identity = (check, (str(failed), *details))
            if result.get("workspace_change") != "unchanged" or identity not in check_results:
                window.clear()
                check_results.add(identity)
                continue
        keys = _observation_keys(tool)
        if not keys:
            # A repeated check may be interleaved with repeated reads.
            if (
                isinstance(command, str)
                and is_verification_command(command)
                and result.get("workspace_change") == "unchanged"
            ):
                check, excerpt = _failure_identity(command, result.get("output"))
                keys = {(check, hashlib.sha256(repr(excerpt).encode()).hexdigest())}
            else:
                window.clear()
                check_results.clear()
                continue
        window.append(keys)
        window = window[-PROGRESS_WINDOW:]
    if len(window) < PROGRESS_WINDOW:
        return False
    counts = Counter(key for keys in window for key in keys)
    # An action counts once even if it contains many identical read segments.
    if any(count - 1 >= REPEATED_OBSERVATIONS for count in counts.values()):
        return True
    simple = [_observation(tool) for tool in actions[-PROGRESS_WINDOW:]]
    return (
        all(key is not None for key in simple)
        and sum(count - 1 for count in Counter(simple).values()) >= REPEATED_OBSERVATIONS
    )


def _observation(tool: ToolMessage) -> tuple[str, str] | None:
    result = _result(tool)
    if tool.status != "succeeded" or result.get("exit_code") not in (None, 0):
        return None
    if result.get("session_id") is not None:
        return None  # Waiting for a live command is not a repetitive read.
    if tool.name in {"read_file", "container_read_file", "glob", "grep"}:
        target = str(tool.arguments.get("path", ""))
    elif tool.name in {"run_command", "container_exec"}:
        command = tool.arguments.get("command", tool.arguments.get("cmd", ""))
        if not isinstance(command, str) or is_verification_command(command):
            return None
        if result.get("workspace_change") != "unchanged" and not is_observation_command(command):
            return None
        if result.get("workspace_change") == "changed":
            return None
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        # Combine the object read with the returned content, so changing line
        # selectors cannot hide repeated observations of the same file.
        target = parts[-1] if parts else ""
    else:
        return None
    output = result.get("output", tool.content or "")
    return target, hashlib.sha256(str(output).encode()).hexdigest()


def refresh_execution_feedback_context(runtime: AgentRuntime) -> None:
    services = runtime.services
    services.execution_feedback_message = None
    if services.execution_feedback_run_id != runtime.run.run_id:
        services.execution_feedback_run_id = runtime.run.run_id
        services.progress_warning_action = 0
    messages: list[str] = []
    if services.execution_time_remaining is not None:
        remaining = services.execution_time_remaining()
        if remaining is not None:
            reserve = services.execution_finalization_reserve
            messages.append(
                f"Agent time remaining: {max(0, remaining):.1f} seconds; "
                f"finalization reserve: {reserve:.1f} seconds. "
                "Start with a small focused check before broad verification. "
                "Keep output visible, respect tool timeouts, and report unverified work."
            )
            if remaining <= reserve:
                messages.append(
                    "Finalization reserve reached. Do not start more tools; summarize the result and unresolved checks."
                )
    actions = [tool for tool in runtime.run.actions if tool.status != "pending"]
    if len(actions) >= PROGRESS_WINDOW and len(actions) - services.progress_warning_action >= PROGRESS_WINDOW:
        if _repeated_observations(actions):
            messages.append(
                "Recent tools repeatedly returned the same observations without edits or new verification evidence. "
                "Repeated read segments in compound commands and unchanged failing checks also count. "
                "Choose a concrete implementation change, a focused verification, or explain the blocker. "
                "This is advisory; do not change tests just to obtain a pass."
            )
            services.progress_warning_action = len(actions)
    if messages:
        services.execution_feedback_message = UserMessage(
            content=EXECUTION_FEEDBACK_HEADER + "\n" + "\n".join(messages)
        )
