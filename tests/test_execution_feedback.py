"""Read loops trigger advisory feedback without stopping legitimate work."""

from __future__ import annotations

import json

from backend.domain import AssistantMessage, ToolMessage
from backend.runtime import AgentRunner
from backend.runtime.execution.execution_feedback import refresh_execution_feedback_context
from backend.tools import Tool, ToolRegistry
from tests.test_completion_review import command_result, runtime_with


def read(index: int, *, output: str = "same source", command: str = "sed -n 1,30p feature.py") -> ToolMessage:
    action = command_result(str(index), command, code=0, output=output)
    action.content = json.dumps({"exit_code": 0, "output": output, "workspace_change": "unchanged"})
    return action


def test_repeated_observations_warn_once_then_cool_down() -> None:
    runtime = runtime_with(*(read(i) for i in range(12)))
    refresh_execution_feedback_context(runtime)
    message = runtime.services.execution_feedback_message
    assert message is not None and "repeatedly" in message.content
    assert message in runtime.model_messages()
    assert runtime.run.status == "running"
    refresh_execution_feedback_context(runtime)
    assert runtime.services.execution_feedback_message is None
    runtime.run.actions.extend(read(i) for i in range(12, 24))
    refresh_execution_feedback_context(runtime)
    assert runtime.services.execution_feedback_message is not None


def test_new_content_edits_checks_and_running_sessions_break_the_window() -> None:
    runtime = runtime_with(*(read(i, output=f"new evidence {i}") for i in range(12)))
    refresh_execution_feedback_context(runtime)
    assert runtime.services.execution_feedback_message is None
    for boundary in (
        command_result("test", code=0, output="5 passed"),
        command_result("running", "cat feature.py", code=None, output="", session="live"),
        ToolMessage(name="container_write_file", call_id="write", content="written", status="succeeded"),
    ):
        runtime = runtime_with(*(read(i) for i in range(11)), boundary)
        refresh_execution_feedback_context(runtime)
        assert runtime.services.execution_feedback_message is None


def test_budget_feedback_is_private_and_only_in_the_current_run() -> None:
    runtime = runtime_with()
    runtime.services.execution_time_remaining = lambda: 40.0
    runtime.services.execution_finalization_reserve = 60
    refresh_execution_feedback_context(runtime)
    message = runtime.services.execution_feedback_message
    assert "40.0" in message.content and "Do not start more tools" in message.content
    assert message in runtime.model_messages()
    assert message not in runtime.state.messages
    runtime.run.status = "completed"
    assert message not in runtime.model_messages()


def test_compound_reads_survive_varying_tail_output_and_repeated_checks() -> None:
    runtime = runtime_with()
    common = "\n".join(f"source line {i}: unchanged assertion and expected tuple" for i in range(12))
    warned = False
    for index in range(28):
        command = "git diff -- tests/test_feature.py; sed -n '1,12p' tests/test_feature.py; "
        if index % 4 == 0:
            command += "pytest -q tests/test_feature.py"
            output = common + f"\nE   AssertionError: tuples differ\n1 failed in {index + 1}.5s"
            action = read(index, command=command, output=output)
            result = json.loads(action.content)
            result["exit_code"] = 1
            action.content = json.dumps(result)
        else:
            command += f"python - <<'PY'\nprint('new diagnostic {index}')\nPY"
            action = read(index, command=command, output=common + f"\nnew diagnostic {index}")
        runtime.run.actions.append(action)
        refresh_execution_feedback_context(runtime)
        message = runtime.services.execution_feedback_message
        warned |= message is not None and "repeatedly" in message.content
    assert warned


def test_new_check_results_and_actual_edits_reset_compound_read_detection() -> None:
    common = "\n".join(f"same source line {i} with unchanged details" for i in range(12))
    runtime = runtime_with()
    for index in range(24):
        action = read(index, command="git diff -- feature.py; sed -n 1,12p feature.py", output=common)
        if index % 4 == 0:
            action = read(index, command=f"pytest -q tests/test_{index}.py", output="1 passed")
        runtime.run.actions.append(action)
        refresh_execution_feedback_context(runtime)
        assert runtime.services.execution_feedback_message is None
    runtime = runtime_with(*(read(i) for i in range(11)))
    changed = read(12)
    changed.content = json.dumps({"exit_code": 0, "workspace_change": "changed", "output": common})
    runtime.run.actions.append(changed)
    refresh_execution_feedback_context(runtime)
    assert runtime.services.execution_feedback_message is None


def test_common_header_does_not_turn_new_simple_read_content_into_a_loop() -> None:
    common = "\n".join(f"license header line {i}" for i in range(12))
    runtime = runtime_with(
        *(read(i, command="cat feature.py", output=common + f"\nnew implementation {i}") for i in range(16))
    )
    refresh_execution_feedback_context(runtime)
    assert runtime.services.execution_feedback_message is None


def test_bounded_snapshot_failure_still_allows_known_read_observations() -> None:
    actions = [read(i) for i in range(12)]
    for action in actions:
        action.content = json.dumps({"exit_code": 0, "output": "same source", "workspace_change": "unknown"})
    runtime = runtime_with(*actions)
    refresh_execution_feedback_context(runtime)
    assert runtime.services.execution_feedback_message is not None


def test_workflow_delivers_progress_feedback_before_the_model_finishes() -> None:
    class Planner:
        name = "repeated-reader"

        def __init__(self):
            self.calls = 0
            self.warned = False

        def decide(self, runtime):
            self.calls += 1
            if self.calls == 13:
                self.warned = any("repeatedly" in (message.content or "") for message in runtime.model_messages())
                return AssistantMessage(content="I will change approach.")
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(name="container_exec", call_id=str(self.calls), arguments={"command": "cat feature.py"})
                ]
            )

    planner = Planner()
    tools = ToolRegistry(
        [
            Tool(
                "container_exec",
                "Read fixture",
                lambda command: json.dumps({"exit_code": 0, "output": "source", "workspace_change": "unchanged"}),
            )
        ]
    )
    runner = AgentRunner(planner, tools)
    result = runner.run(runner.new_runtime(task="Inspect feature"))
    assert result.status == "completed" and planner.calls == 13 and planner.warned
