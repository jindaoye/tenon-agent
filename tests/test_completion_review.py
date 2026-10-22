"""Completion checks use observed results, respect budgets, and never grade answers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.domain import AssistantMessage, RunState, ToolMessage, UserMessage
from backend.domain.runtime_state import RuntimeState as TurnState
from backend.planning.llm import LLMPlanner
from backend.runtime import AgentRunner, AgentRuntime, PreparedResponse
from backend.runtime.execution.completion_review import (
    COMPLETION_REVIEW_HEADER,
    VERIFICATION_FEEDBACK_HEADER,
    check_completion_review,
    completion_concerns,
    refresh_completion_review_context,
    refresh_verification_feedback_context,
    verification_evidence,
)
from backend.runtime.execution.execution_feedback import (
    EXECUTION_FEEDBACK_HEADER,
    refresh_execution_feedback_context,
)
from backend.tools import Tool, ToolRegistry
from tests.local_store import session_store


class ReplyPlanner:
    name = "completion-review-test"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        return AssistantMessage(content="candidate")


def runtime_with(*actions: ToolMessage) -> AgentRuntime:
    runtime = AgentRunner(ReplyPlanner(), ToolRegistry()).new_runtime(task="Implement the request")
    runtime.run.actions.extend(actions)
    return runtime


def command_result(
    call_id: str,
    command: str = "pytest -q tests/test_feature.py",
    *,
    code: int | None = 1,
    output: str = "1 failed, 4 passed",
    name: str = "container_exec",
    session: str | None = None,
) -> ToolMessage:
    data = {"output": output}
    if code is not None:
        data["exit_code"] = code
    if session is not None:
        data["session_id"] = session
    arguments = {"cmd" if name == "run_command" else "command": command}
    if name == "write_stdin":
        arguments = {"session_id": "session-1", "chars": ""}
    return ToolMessage(name=name, call_id=call_id, arguments=arguments, content=json.dumps(data), status="succeeded")


@pytest.mark.parametrize("name", ["container_exec", "run_command"])
def test_command_execution_success_does_not_mean_verification_passed(name: str) -> None:
    runtime = runtime_with(command_result("failed-check", name=name))
    assert completion_concerns(runtime)[0].status == "failed"
    assert check_completion_review(runtime)
    assert COMPLETION_REVIEW_HEADER in runtime.model_messages()[-1].content


def test_unrelated_pass_does_not_clear_failure_but_same_check_rerun_does() -> None:
    runtime = runtime_with(
        command_result("failed-check"),
        command_result("other-pass", "npm test", code=0, output="all passed"),
        command_result("listing", "ls", code=0, output="files"),
    )
    assert [check.call_id for check in completion_concerns(runtime)] == ["failed-check"]
    runtime.run.actions.append(command_result("fixed-check", code=0, output="5 passed"))
    assert not check_completion_review(runtime)


def test_failed_test_summary_is_not_hidden_by_shell_exit_zero() -> None:
    runtime = runtime_with(command_result("masked", "pytest -q; echo done", code=0))
    assert completion_concerns(runtime)[0].status == "failed"


def test_failure_facts_reach_the_next_normal_decision_without_claiming_an_extra_review() -> None:
    runtime = runtime_with(
        command_result("check", output="E   Expected: invoke(option=True)\nE   Actual: invoke()\n1 failed")
    )
    refresh_verification_feedback_context(runtime)
    message = runtime.model_messages()[-1]
    assert VERIFICATION_FEEDBACK_HEADER in message.content
    assert "Expected: invoke(option=True)" in message.content
    assert "Actual: invoke()" in message.content
    assert runtime.run.completion_review_count == 0
    assert not runtime.run.completion_review_pending
    assert all(VERIFICATION_FEEDBACK_HEADER not in (item.content or "") for item in runtime.state.messages)
    runtime.run.actions.append(command_result("pass", code=0, output="5 passed"))
    refresh_verification_feedback_context(runtime)
    assert runtime.services.verification_feedback_message is None


def test_feedback_does_not_leak_to_plan_finished_or_another_run() -> None:
    runtime = runtime_with(command_result("check"))
    refresh_verification_feedback_context(runtime)
    assert VERIFICATION_FEEDBACK_HEADER in runtime.model_messages()[-1].content
    runtime.run.mode = "plan"
    assert not any(VERIFICATION_FEEDBACK_HEADER in (item.content or "") for item in runtime.model_messages())
    runtime.run.mode = "agent"
    runtime.run.status = "cancelled"
    assert not any(VERIFICATION_FEEDBACK_HEADER in (item.content or "") for item in runtime.model_messages())
    runtime.state.current_run = RunState(task="Next", mode="agent")
    assert not any(VERIFICATION_FEEDBACK_HEADER in (item.content or "") for item in runtime.model_messages())


def test_feedback_is_rebuilt_from_durable_actions_and_retains_failing_poll_details() -> None:
    runtime = runtime_with(
        command_result(
            "started", name="run_command", code=None, output="E   Expected: invoke(option=True)", session="session-1"
        ),
        command_result("poll", name="write_stdin", code=1, output="1 failed"),
    )
    runtime.state.current_run = RunState.from_dict(runtime.run.to_dict())
    refresh_verification_feedback_context(runtime)
    assert "Expected: invoke(option=True)" in runtime.model_messages()[-1].content
    assert "1 failed" in runtime.model_messages()[-1].content
    assert runtime.run.completion_review_count == 0


def test_direct_test_write_requires_independent_verification_after_a_failure() -> None:
    runtime = runtime_with(
        command_result("failed"),
        ToolMessage(
            name="file_operation",
            call_id="test-edit",
            arguments={"operation": "write", "path": "tests/test_feature.py", "content": "assert new"},
            content="edited",
            status="succeeded",
        ),
        command_result("passed", code=0, output="5 passed"),
    )
    assert [check.status for check in completion_concerns(runtime)] == ["modified_tests_observed_after_failure"]


def test_changed_test_expectations_remain_visible_after_the_same_check_passes() -> None:
    patch = "diff --git a/tests/test_feature.py b/tests/test_feature.py\n--- a/tests/test_feature.py\n+++ b/tests/test_feature.py\n-assert actual == old\n+assert actual == new\n"
    runtime = runtime_with(
        command_result("failed"),
        command_result("diff", "git diff -- tests/test_feature.py", code=0, output=patch),
        command_result("passed", code=0, output="5 passed"),
    )
    concerns = completion_concerns(runtime)
    assert len(concerns) == 1
    assert concerns[0].status == "modified_tests_observed_after_failure"
    assert "assert actual == old" in concerns[0].details
    refresh_verification_feedback_context(runtime)
    assert "independent validation" in runtime.model_messages()[-1].content
    assert check_completion_review(runtime)
    refresh_verification_feedback_context(runtime)
    assert runtime.services.verification_feedback_message is None  # no duplicate private guidance
    assert not check_completion_review(runtime)  # provenance alone is reviewed once
    assert runtime.run.completion_review_count == 1


@pytest.mark.parametrize("failed_first", [False, True])
def test_new_tests_and_preexisting_changes_do_not_force_an_independence_review(failed_first: bool) -> None:
    patch = "diff --git a/tests/test_feature.py b/tests/test_feature.py\n--- a/tests/test_feature.py\n+++ b/tests/test_feature.py\n+def test_added(): pass\n"
    actions = [command_result("diff", "git diff", code=0, output=patch)]
    if failed_first:
        actions.insert(0, command_result("failed"))
    actions.append(command_result("passed", code=0, output="5 passed"))
    assert not completion_concerns(runtime_with(*actions))


def test_observed_test_changes_before_the_failure_are_not_attributed_to_the_repair() -> None:
    patch = "diff --git a/tests/test_feature.py b/tests/test_feature.py\n--- a/tests/test_feature.py\n+++ b/tests/test_feature.py\n-assert old\n+assert new\n"
    runtime = runtime_with(
        command_result("initial-diff", "git diff", code=0, output=patch),
        command_result("failed"),
        command_result("passed", code=0, output="5 passed"),
    )
    assert not completion_concerns(runtime)


def test_full_pytest_rerun_clears_obsolete_filtered_failure() -> None:
    runtime = runtime_with(
        command_result("failed-selection", "PYTHONPATH=lib pytest tests/test_feature.py -k new_option"),
        command_result("full-pass", "PYTHONPATH=lib pytest -q tests/test_feature.py", code=0, output="5 passed"),
    )
    assert not completion_concerns(runtime)
    assert [check.call_id for check in verification_evidence(runtime)] == ["full-pass"]


def test_failed_or_running_checks_are_not_cleared_by_narrower_or_incomplete_reruns() -> None:
    runtime = runtime_with(
        command_result("failed", "pytest tests/test_feature.py"),
        command_result("narrow", "pytest tests/test_feature.py -k unaffected", code=0, output="1 passed"),
        command_result("running", "pytest tests/test_other.py -k feature", code=None, output="", session="session-1"),
        command_result("full-pass", "pytest tests/test_other.py", code=0, output="5 passed"),
    )
    assert [check.call_id for check in completion_concerns(runtime)] == ["failed", "running"]
    runtime.run.actions.append(command_result("failed-full", "pytest tests/test_other.py", code=1))
    assert [check.status for check in completion_concerns(runtime)] == ["failed", "running", "failed"]


@pytest.mark.parametrize("context_field", ["workdir", "container_id"])
@pytest.mark.parametrize("broader", [False, True])
def test_pass_in_different_execution_context_does_not_clear_failure(context_field: str, broader: bool) -> None:
    failed = command_result("failed", "pytest tests/test_feature.py -k feature")
    failed.arguments[context_field] = "first"
    passed = command_result(
        "passed",
        "pytest tests/test_feature.py" if broader else "pytest tests/test_feature.py -k feature",
        code=0,
        output="5 passed",
    )
    passed.arguments[context_field] = "second"
    assert [check.call_id for check in completion_concerns(runtime_with(failed, passed))] == ["failed"]


def test_async_pass_in_original_directory_resolves_its_failure() -> None:
    failed = command_result("failed", name="run_command")
    started = command_result("started", name="run_command", code=None, output="", session="session-1")
    for action in (failed, started):
        action.arguments["workdir"] = "/app"
    runtime = runtime_with(failed, started, command_result("poll", name="write_stdin", code=0, output="5 passed"))
    assert not completion_concerns(runtime)


@pytest.mark.parametrize("option", ["timeout_seconds", "yield_time_ms", "max_output_tokens"])
def test_rerun_options_do_not_keep_an_obsolete_failure(option: str) -> None:
    failed = command_result("failed")
    passed = command_result("passed", code=0, output="5 passed")
    failed.arguments[option] = 10
    passed.arguments[option] = 60
    runtime = runtime_with(failed, passed)
    assert [check.call_id for check in verification_evidence(runtime)] == ["passed"]
    assert not completion_concerns(runtime)


@pytest.mark.parametrize("name", ["container_exec", "run_command", "write_stdin"])
@pytest.mark.parametrize("code", [0, 2])
def test_command_mutation_invalidates_pass_even_when_the_command_fails(name: str, code: int) -> None:
    edit = command_result("edit", "python change_file.py", name=name, code=code, output="")
    edit.content = json.dumps({"exit_code": code, "workspace_change": "changed", "output": ""})
    if code:
        edit.status = "failed"
    runtime = runtime_with(command_result("pass", code=0, output="5 passed"), edit)
    assert completion_concerns(runtime)[0].status == "not_rechecked_after_edit"
    runtime.run.actions.append(command_result("rerun", code=0, output="5 passed"))
    assert not completion_concerns(runtime)


@pytest.mark.parametrize("change", ["unchanged", "unknown"])
def test_command_observation_distinguishes_a_read_from_missing_coverage(change: str) -> None:
    action = command_result("read", "python inspect_file.py", code=0, output="content")
    action.content = json.dumps({"exit_code": 0, "workspace_change": change, "output": "content"})
    runtime = runtime_with(command_result("pass", code=0, output="5 passed"), action)
    assert bool(completion_concerns(runtime)) is (change == "unknown")


def test_tool_denied_before_execution_does_not_invalidate_verification() -> None:
    action = command_result("denied", "python change_file.py", code=1)
    action.status = "failed"
    action.content = json.dumps({"not_started": True, "output": "Finalization reserve reached"})
    assert not completion_concerns(runtime_with(command_result("pass", code=0, output="5 passed"), action))


@pytest.mark.parametrize(
    ("command", "code", "output"),
    [
        ("grep missing README.md", 1, ""),
        ("rg pytest README.md", 1, ""),
        ("echo pytest failed", 0, "1 failed"),
        ("git diff --exit-code", 1, "diff"),
        ("pytest -q", 0, "0 failed, 5 passed"),
        ("npm test", 0, "2 expected failures; 3 passed"),
    ],
)
def test_search_results_and_expected_or_zero_failures_do_not_force_review(command, code, output) -> None:
    assert not completion_concerns(runtime_with(command_result("check", command, code=code, output=output)))


@pytest.mark.parametrize(
    "command",
    [
        "cd /app && uv run python -m pytest -q",
        ".venv\\Scripts\\python.exe -m pytest -q",
        '& "D:\\project dir\\python.exe" -m pytest -q',
        "npm run typecheck",
        "npx tsc --noEmit",
        "ruff format --check .",
        "python -m py_compile feature.py",
        "PYTHONPATH=lib pytest tests/test_feature.py",
        "git status --short; PYTHONPATH=lib pytest tests/test_feature.py",
        "gcc main.c -o /tmp/main",
    ],
)
def test_common_verification_invocations_are_recognized(command: str) -> None:
    assert completion_concerns(runtime_with(command_result("check", command)))


def test_async_verification_is_tracked_until_poll_finishes() -> None:
    runtime = runtime_with(
        command_result("started", name="run_command", code=None, output="running", session="session-1")
    )
    assert completion_concerns(runtime)[0].status == "running"
    runtime.run.actions.append(command_result("poll", name="write_stdin", code=0, output="5 passed"))
    assert not completion_concerns(runtime)


def test_async_failed_output_survives_subsequent_empty_poll() -> None:
    runtime = runtime_with(
        command_result("started", name="run_command", code=None, session="session-1"),
        command_result("poll", name="write_stdin", code=0, output=""),
    )
    assert completion_concerns(runtime)[0].status == "failed"


@pytest.mark.parametrize("tool_name", ["file_operation", "container_write_file"])
def test_file_edit_after_pass_needs_rechecking(tool_name: str) -> None:
    runtime = runtime_with(command_result("pass", code=0, output="5 passed"))
    runtime.run.actions.append(
        ToolMessage(
            name=tool_name, call_id="edit", arguments={"path": "feature.py"}, content="edited", status="succeeded"
        )
    )
    assert completion_concerns(runtime)[0].status == "not_rechecked_after_edit"
    runtime.run.actions.append(command_result("rerun", code=0, output="5 passed"))
    assert not completion_concerns(runtime)


def test_review_bound_and_pending_instruction_survive_serialization() -> None:
    runtime = runtime_with(command_result("check"))
    assert check_completion_review(runtime)
    runtime.state.current_run = RunState.from_dict(runtime.run.to_dict())
    runtime.services.completion_review_message = None
    refresh_completion_review_context(runtime)
    assert COMPLETION_REVIEW_HEADER in runtime.model_messages()[-1].content
    assert check_completion_review(runtime)
    assert not check_completion_review(runtime)
    assert runtime.run.completion_review_count == 2
    assert runtime.services.completion_review_message is None


def test_review_coexists_with_todo_context_and_does_not_change_original_task() -> None:
    runtime = runtime_with(command_result("check"))
    original = runtime.run.task
    runtime.services.context_suffix_messages = [UserMessage(content="todo reminder")]
    assert check_completion_review(runtime)
    assert runtime.model_messages()[-2].content == "todo reminder"
    assert runtime.run.task == original
    assert COMPLETION_REVIEW_HEADER not in "\n".join(message.content or "" for message in runtime.state.messages)


def test_review_context_does_not_leak_to_plan_mode_finished_or_new_runs() -> None:
    runtime = runtime_with(command_result("check"))
    assert check_completion_review(runtime)
    runtime.run.mode = "plan"
    assert all(COMPLETION_REVIEW_HEADER not in (message.content or "") for message in runtime.model_messages())
    runtime.run.mode = "agent"
    runtime.run.status = "cancelled"
    assert all(COMPLETION_REVIEW_HEADER not in (message.content or "") for message in runtime.model_messages())
    runtime.state.current_run = RunState(task="Next task", mode="agent")
    assert all(COMPLETION_REVIEW_HEADER not in (message.content or "") for message in runtime.model_messages())


def test_sqlite_resume_preserves_review_limit_and_rebuilds_private_context(tmp_path: Path) -> None:
    from backend.runtime.conversation.recovery.reconstruction import reconstruct_attempt

    store = session_store(tmp_path / "store")
    session = store.create_session("Completion review")
    runner = AgentRunner(ReplyPlanner(), ToolRegistry())
    runtime = runner.new_runtime(task="Implement", session_id=session.session_id, runtime_store=store)
    runtime.run.actions.append(command_result("check"))
    assert check_completion_review(runtime)
    restored = store.load_runtime(session.session_id)
    assert restored is not None
    _, resumed = reconstruct_attempt(restored)
    runtime.state = resumed
    runtime.services.completion_review_message = None
    refresh_completion_review_context(runtime)
    assert runtime.run.completion_review_count == 1
    assert COMPLETION_REVIEW_HEADER in runtime.model_messages()[-1].content
    assert check_completion_review(runtime)
    assert not check_completion_review(runtime)


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("kind", ["review", "feedback", "execution"])
def test_model_request_keeps_review_verbatim_outside_compacted_history(
    canonical: bool, compact: bool, kind: str
) -> None:
    class LocalClient:
        context_size = 100

        def __init__(self) -> None:
            self.estimates = [100, 20] if compact else [10]
            self.transcript = ""
            self.decision = []

        def estimate_tokens(self, _messages, _tools, _parameters):
            return self.estimates.pop(0) if len(self.estimates) > 1 else self.estimates[0]

        def run(self, runtime: AgentRuntime) -> PreparedResponse:
            if runtime.exchange.operation == "summarize":
                self.transcript = runtime.exchange.messages[-1].content
                return PreparedResponse(AssistantMessage(content="summary"), {"total_tokens": 1})
            self.decision = list(runtime.exchange.messages)
            return PreparedResponse(AssistantMessage(content="checked"), {"total_tokens": 1})

    runtime = runtime_with(command_result("check"))
    if canonical:
        turn = TurnState.create(
            session_id=runtime.state.session_id, thread_id=runtime.state.thread_id, user_content=runtime.run.task
        )
        runtime.services.runtime_node_context = lambda: [turn]
    if kind == "review":
        assert check_completion_review(runtime)
        review = runtime.services.completion_review_message
    elif kind == "feedback":
        refresh_verification_feedback_context(runtime)
        review = runtime.services.verification_feedback_message
    else:
        runtime.services.execution_time_remaining = lambda: 100.0
        refresh_execution_feedback_context(runtime)
        review = runtime.services.execution_feedback_message
    assert review is not None
    client = LocalClient()
    planner = LLMPlanner(client, [], [])
    assert planner.decide(runtime).content == "checked"
    assert sum(message is review for message in client.decision) == 1
    assert client.decision[-1] is review
    assert COMPLETION_REVIEW_HEADER not in client.transcript
    assert VERIFICATION_FEEDBACK_HEADER not in client.transcript
    assert EXECUTION_FEEDBACK_HEADER not in client.transcript
    assert bool(client.transcript) is compact
    assert all(COMPLETION_REVIEW_HEADER not in (message.content or "") for message in runtime.state.messages)


def test_no_verification_evidence_does_not_invent_tests() -> None:
    runtime = runtime_with()
    assert not verification_evidence(runtime)
    assert not check_completion_review(runtime)


class RepairPlanner:
    name = "repair-after-verification"

    def __init__(self, command: str, *, repair: bool = True, early_repair: bool = False) -> None:
        self.command = command
        self.repair = repair
        self.calls = 0
        self.saw_review = False
        self.saw_feedback = False
        self.early_repair = early_repair

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        self.calls += 1
        if self.early_repair:
            if self.calls in {1, 3}:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="container_exec", call_id=f"check-{self.calls}", arguments={"command": self.command}
                        )
                    ]
                )
            if self.calls == 2:
                self.saw_feedback = any(
                    VERIFICATION_FEEDBACK_HEADER in (message.content or "") for message in runtime.model_messages()
                )
                return AssistantMessage(tool_messages=[ToolMessage(name="repair", call_id="repair")])
            return AssistantMessage(content="verified final")
        if self.calls == 1 or self.calls == 4 and self.repair:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="container_exec", call_id=f"check-{self.calls}", arguments={"command": self.command}
                    )
                ]
            )
        if self.calls == 3 and self.repair:
            self.saw_review = any(
                COMPLETION_REVIEW_HEADER in (message.content or "") for message in runtime.model_messages()
            )
            return AssistantMessage(tool_messages=[ToolMessage(name="repair", call_id="repair")])
        return AssistantMessage(content="premature success" if self.calls == 2 else "verified final")


@pytest.mark.parametrize("env_prefix", [False, True])
@pytest.mark.parametrize("early_repair", [False, True])
def test_real_local_pytest_failure_is_reviewed_repaired_and_retested(
    tmp_path: Path, env_prefix: bool, early_repair: bool
) -> None:
    """Execute a real failing then passing local test; the planner is deterministic."""

    (tmp_path / "test_feature.py").write_text(
        "from pathlib import Path\ndef test_feature():\n    assert Path(__file__).with_name('fixed').exists()\n",
        encoding="utf-8",
    )
    command = ("PYTHONPATH=lib " if env_prefix else "") + "python -m pytest -q test_feature.py"
    planner = RepairPlanner(command, early_repair=early_repair)
    codes = []

    def execute(command: str) -> str:
        assert command == planner.command
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "test_feature.py"],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": "lib"} if env_prefix else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        codes.append(result.returncode)
        return json.dumps({"exit_code": result.returncode, "output": result.stdout + result.stderr})

    tools = ToolRegistry(
        [
            Tool("container_exec", "Run a command", execute),
            Tool("repair", "Repair the test fixture", lambda: (tmp_path / "fixed").write_text("fixed") and "fixed"),
        ]
    )
    runner = AgentRunner(planner, tools)
    events = []
    runtime = runner.new_runtime(task="Implement feature", on_event=events.append)
    result = runner.run(runtime)

    assert codes == [1, 0]
    assert planner.saw_feedback if early_repair else planner.saw_review
    assert result.status == "completed"
    assert result.final_answer == "verified final"
    assert result.completion_review_count == (0 if early_repair else 1)
    assert result.model_turns == (4 if early_repair else 5)
    assert not any(
        isinstance(message, AssistantMessage) and message.content == "premature success" for message in result.history
    )
    assert not any(event.kind == "assistant_message" and "premature success" in str(event.data) for event in events)


def test_ignored_review_is_bounded_and_tool_budget_still_applies() -> None:
    planner = RepairPlanner("pytest -q", repair=False)
    runner = AgentRunner(
        planner,
        ToolRegistry(
            [
                Tool(
                    "container_exec",
                    "Run",
                    lambda command: json.dumps(
                        {
                            "exit_code": 1,
                            "output": "1 failed",
                        }
                    ),
                )
            ]
        ),
    )
    runtime = runner.new_runtime(task="Test")
    result = runner.run(runtime)
    assert planner.calls == 4
    assert result.completion_review_count == 2

    runner = AgentRunner(planner, runner.tools, max_tool_calls=1)
    runtime = runner.new_runtime(task="Test again")
    planner.calls = 0
    result = runner.run(runtime)
    assert result.status == "failed"
    assert planner.calls == 1
    assert result.completion_review_count == 0


def test_cancellation_during_candidate_does_not_request_another_model_call() -> None:
    cancelled = False

    class CancellingPlanner(RepairPlanner):
        def decide(self, runtime: AgentRuntime) -> AssistantMessage:
            nonlocal cancelled
            response = super().decide(runtime)
            if self.calls == 2:
                cancelled = True
            return response

    planner = CancellingPlanner("pytest -q", repair=False)
    runner = AgentRunner(planner, ToolRegistry([Tool("container_exec", "Run", lambda command: '{"exit_code":1}')]))
    runtime = runner.new_runtime(task="Test")
    runtime.services.cancel_requested = lambda: cancelled
    result = runner.run(runtime)
    assert result.status == "cancelled"
    assert planner.calls == 2
