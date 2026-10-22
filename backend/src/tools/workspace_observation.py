"""Bounded content observations; missing coverage is unknown, never unchanged."""

from __future__ import annotations

import inspect
import json


def workspace_fingerprint(root: str) -> str | None:
    """Hash regular files without following links or reading unbounded trees.

    This function is also sent verbatim to isolated task containers. Keep its
    dependencies inside the function and use only the Python standard library.
    Caches, dependencies and Git internals are outside the observation scope.
    """
    import hashlib
    import os
    import stat
    import time

    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    deadline = time.monotonic() + 1.0
    digest = hashlib.sha256()
    count = 0
    size = 0

    def failed(_error):
        raise _error

    try:
        if not os.path.isdir(root):
            return None
        for directory, directories, files in os.walk(root, followlinks=False, onerror=failed):
            if time.monotonic() >= deadline:
                return None
            directories[:] = sorted(name for name in directories if name not in ignored)
            # Include link targets, but never traverse or open a symlink.
            links = [
                name
                for name in directories
                if os.path.islink(os.path.join(directory, name))
                or getattr(os.path, "isjunction", lambda _path: False)(os.path.join(directory, name))
            ]
            directories[:] = [name for name in directories if name not in links]
            for name in sorted(files + links):
                if name.endswith((".pyc", ".pyo")):
                    continue
                count += 1
                if count > 20000 or time.monotonic() >= deadline:
                    return None
                path = os.path.join(directory, name)
                metadata = os.lstat(path)
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                digest.update(relative.encode("utf-8", errors="surrogateescape") + b"\0")
                digest.update(str(stat.S_IMODE(metadata.st_mode)).encode() + b"\0")
                if stat.S_ISLNK(metadata.st_mode):
                    digest.update(b"link\0" + os.readlink(path).encode("utf-8", errors="surrogateescape") + b"\0")
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    return None
                size += metadata.st_size
                if size > 64 * 1024 * 1024:
                    return None
                content = hashlib.sha256()
                flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(path, flags), "rb") as source:
                    opened = os.fstat(source.fileno())
                    if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
                        return None
                    while chunk := source.read(65536):
                        if time.monotonic() >= deadline:
                            return None
                        content.update(chunk)
                after = os.lstat(path)
                if (metadata.st_size, metadata.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    return None
                digest.update(content.digest())
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def workspace_change(before: str | None, after: str | None) -> str:
    if before is None or after is None:
        return "unknown"
    return "unchanged" if before == after else "changed"


def merge_workspace_changes(first: str, second: str) -> str:
    if "changed" in {first, second}:
        return "changed"
    if first == second == "unchanged":
        return "unchanged"
    return "unknown"


def fingerprint_script(root: str) -> str:
    """Run the same observation on the remote filesystem, not the host tree."""
    return (
        "from __future__ import annotations\nimport json\n"
        + inspect.getsource(workspace_fingerprint)
        + "\nprint(json.dumps(workspace_fingerprint("
        + json.dumps(root)
        + ")))\n"
    )
