"""Deterministic deadline checks, including tool and runner integration."""

from __future__ import annotations

import json
import os

import pytest

from backend.tools.base import ToolError
from benchmarks.containers import TaskContainer
from benchmarks.execution_budget import AgentTimeBudget
from benchmarks.tasks import TASKS_BY_NAME


def test_budget_caps_commands_and_reserves_finalization_time(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr("benchmarks.execution_budget.monotonic", lambda: now[0])
    budget = AgentTimeBudget(900, 60)
    budget.start()
    now[0] = 800
    assert budget.remaining() == 100
    assert budget.command_timeout(3600) == 40
    assert budget.command_timeout(10) == 10
    now[0] = 840
    with pytest.raises(ToolError) as error:
        budget.command_timeout(60)
    assert json.loads(error.value.tool_output)["not_started"] is True
    assert not budget.expired()
    now[0] = 900
    assert budget.expired()
    assert AgentTimeBudget().command_timeout(3600) == 3600


@pytest.mark.parametrize("timeout", [-1, 0, float("nan"), float("inf")])
def test_invalid_deadlines_are_rejected(timeout) -> None:
    with pytest.raises(ValueError):
        AgentTimeBudget(timeout)


def test_container_tool_uses_remaining_budget_and_reports_actual_mutation(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr("benchmarks.execution_budget.monotonic", lambda: now[0])
    container = TaskContainer(TASKS_BY_NAME["tb2-cancel-async-tasks"])
    container.agent_budget = AgentTimeBudget(900, 60)
    container.agent_budget.start()
    now[0] = 800
    snapshots = iter(["before", "after"])
    monkeypatch.setattr(container, "workspace_fingerprint", lambda: next(snapshots))
    calls = []
    monkeypatch.setattr(container, "exec", lambda script, **kwargs: calls.append(kwargs) or (0, "done"))
    tools = container.tools()
    result = json.loads(tools.invoke("container_exec", {"command": "run", "timeout_seconds": 3600}, confirmed=True))
    assert calls[0]["timeout"] == 40 and result["workspace_change"] == "changed"
    now[0] = 845
    with pytest.raises(ToolError):
        tools.invoke("container_exec", {"command": "run"}, confirmed=True)
    with pytest.raises(ToolError):
        tools.invoke("container_write_file", {"path": "/app/a", "content": "x"}, confirmed=True)
    assert len(calls) == 1


def test_runner_enforces_agent_deadline_with_real_local_http_and_process(
    tmp_path,
    local_sandbox_runtime,
) -> None:
    import sys

    from benchmarks.model import BenchmarkTask, SourceMetadata
    from benchmarks.runner import run_one_task
    from benchmarks.sandbox import Sandbox
    from tests.benchmark_local_support import local_model

    task = BenchmarkTask(
        name="local-timeout",
        description="Local deadline check",
        capability="terminal",
        prompt="Run command",
        difficulty="easy",
        source=SourceMetadata(
            benchmark="local",
            task_id="deadline",
            url="https://example.test",
            source_revision="local",
            license="local",
            adaptation_notes="Deterministic local HTTP fixture; no paid API.",
        ),
    )
    with local_model(
        tool_name="run_command",
        tool_arguments={
            "cmd": f'"{sys.executable}" -c "import time;time.sleep(30)"',
            "yield_time_ms": 10000,
        },
    ) as (config, calls):
        sandbox = Sandbox(tmp_path / "sandbox", model_config=config)
        sandbox.prepare()
        result = run_one_task(
            task,
            planner="llm",
            sandbox=sandbox,
            agent_timeout_seconds=2,
            finalization_reserve_seconds=0.2,
        )
    assert result.status == "error" and result.failure_phase == "timeout"
    assert result.score is None and calls
    assert result.metrics.duration_ms < 15000


@pytest.mark.skipif(
    os.environ.get("PRAXIS_TEST_DOCKER") != "1", reason="Set PRAXIS_TEST_DOCKER=1 for real Docker tests"
)
def test_real_container_observations_and_budget_preserve_files() -> None:
    from time import monotonic

    container = TaskContainer(TASKS_BY_NAME["tb2-log-summary-date-ranges"])
    try:
        # Use the already installed upstream image; no downloads or model calls.
        container.start(image=container.spec["image"], allow_download=False)
        tools = container.tools()
        changed = json.loads(
            tools.invoke(
                "container_exec",
                {"command": "printf before > observed.txt"},
                confirmed=True,
            )
        )
        assert changed["workspace_change"] == "changed"
        unchanged = json.loads(tools.invoke("container_exec", {"command": "cat observed.txt"}, confirmed=True))
        assert unchanged["workspace_change"] == "unchanged"
        failed = json.loads(
            tools.invoke(
                "container_exec",
                {"command": "printf partial > observed.txt; exit 2"},
                confirmed=True,
            )
        )
        assert failed["exit_code"] == 2 and failed["workspace_change"] == "changed"
        container.agent_budget = AgentTimeBudget(8, 2)
        container.agent_budget.start()
        started = monotonic()
        with pytest.raises(ToolError) as error:
            tools.invoke("container_exec", {"command": "sleep 30", "timeout_seconds": 3600}, confirmed=True)
        assert "timed out" in str(error.value)
        assert monotonic() - started < 20
        container.agent_budget = AgentTimeBudget()
        assert container.exec("cat observed.txt")[1] == "partial"
        _, processes = container.docker("top", container.name, "-eo", "pid,args")
        assert "sleep 30" not in processes
    finally:
        container.close()
