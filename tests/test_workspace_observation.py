"""Actual file and command checks; no model calls or Broker bypass in product."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.tools import WorkspaceCommand
from backend.tools.workspace_observation import fingerprint_script, workspace_change, workspace_fingerprint


def test_fingerprint_tracks_content_creation_deletion_and_ignores_python_cache(tmp_path: Path) -> None:
    original = workspace_fingerprint(str(tmp_path))
    file = tmp_path / "feature.py"
    file.write_text("before")
    created = workspace_fingerprint(str(tmp_path))
    assert workspace_change(original, created) == "changed"
    file.write_text("after!")
    edited = workspace_fingerprint(str(tmp_path))
    assert workspace_change(created, edited) == "changed"
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "feature.pyc").write_bytes(b"cache")
    assert workspace_fingerprint(str(tmp_path)) == edited
    file.unlink()
    assert workspace_fingerprint(str(tmp_path)) == original
    assert workspace_change(None, edited) == "unknown"
    assert workspace_fingerprint(str(tmp_path / "missing")) is None


def test_remote_snapshot_script_matches_the_local_algorithm(tmp_path: Path) -> None:
    (tmp_path / "空 格.py").write_text("sample", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", fingerprint_script(str(tmp_path))],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == workspace_fingerprint(str(tmp_path))


def test_real_workspace_command_reports_reads_writes_and_failed_partial_writes(tmp_path: Path) -> None:
    from backend.jobs import CommandError

    file = tmp_path / "feature.txt"
    file.write_text("before")
    command = WorkspaceCommand(tmp_path)
    try:
        read = "type feature.txt" if os.name == "nt" else "cat feature.txt"
        write = "echo after>feature.txt" if os.name == "nt" else "printf after > feature.txt"
        assert json.loads(command.run(read))["workspace_change"] == "unchanged"
        assert json.loads(command.run(write))["workspace_change"] == "changed"
        with pytest.raises(CommandError) as failure:
            command.run(
                "echo partial>feature.txt & exit /b 2" if os.name == "nt" else "printf partial > feature.txt; exit 2"
            )
        assert json.loads(failure.value.tool_output)["workspace_change"] == "changed"
    finally:
        command.close()


def test_async_poll_observes_changes_without_reporting_the_same_edit_twice(tmp_path: Path) -> None:
    script = "import time; from pathlib import Path; time.sleep(.3); Path('async.txt').write_text('done')"
    command = WorkspaceCommand(tmp_path)
    try:
        result = json.loads(command.run(f'"{sys.executable}" -c "{script}"', yield_time_ms=0))
        assert result["workspace_change"] == "unchanged"
        session = result["session_id"]
        result = json.loads(command.write_stdin(session, yield_time_ms=5000))
        assert result["workspace_change"] == "changed" and result["exit_code"] == 0
        # A later read of the same finished session must not invalidate a check
        # that ran after its already-observed write.
        result = json.loads(command.write_stdin(session, yield_time_ms=0))
        assert result["workspace_change"] == "unchanged"
    finally:
        command.close()
