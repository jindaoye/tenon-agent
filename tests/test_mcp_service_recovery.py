"""Real stdio regression for default service scope and post-rebuild health."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.jobs import JobRegistry
from backend.mcp.client import start_external_tools
from backend.mcp.config import McpServerConfig, McpSettings
from backend.tools import ToolError, ToolRegistry


def test_service_recovers_after_its_stdio_child_dies(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/mcp_notes_lab/server.py"
    audit = tmp_path / "notes.jsonl"

    def events() -> list[dict]:
        if not audit.exists():
            return []
        return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line]

    def wait_for(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        pytest.fail("MCP service did not recover in time")

    config = McpServerConfig(
        "notes",
        sys.executable,
        (str(script), "--audit-log", str(audit)),
        timeout=5,
        env={"PYTHONIOENCODING": "utf-8"},
    )
    jobs = JobRegistry()
    resources = None
    try:
        # Omit job_scope deliberately: the Manager's fallback must use an existing kind.
        resources = start_external_tools(
            (config,), McpSettings(health_failure_threshold=1, rebuild_failure_threshold=2), job_registry=jobs
        )
        manager = resources.manager
        registry = ToolRegistry(resources)
        wait_for(lambda: manager.service_jobs["notes"].health.value == "healthy")
        old_pid = next(event["pid"] for event in events() if event["event"] == "server_started")

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                registry.invoke,
                "mcp_notes_read_document",
                {"name": "ecg-cleaning.md", "delay_seconds": 10},
                True,
            )
            wait_for(lambda: any(event["event"] == "tool_called" for event in events()))
            os.kill(old_pid, signal.SIGTERM)
            with pytest.raises(ToolError):
                pending.result(timeout=10)

        wait_for(lambda: any(event["event"] == "server_started" and event["pid"] != old_pid for event in events()))
        wait_for(lambda: manager.service_jobs["notes"].health.value == "healthy")
        assert manager.server_health["notes"] == "healthy"
        result = registry.invoke("mcp_notes_read_document", {"name": "ecg-cleaning.md"}, confirmed=True)
        assert json.loads(result)["name"] == "ecg-cleaning.md"
        # One interrupted invocation plus one NEW read, with no automatic replay.
        assert len([event for event in events() if event["event"] == "tool_called"]) == 2
    finally:
        try:
            jobs.close_all(reason="MCP recovery test finished", timeout=5)
        finally:
            if resources is not None:
                resources.close()
