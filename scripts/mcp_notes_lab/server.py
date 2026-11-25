"""Two observable MCP tools for the Tenon learning lab (SDK 2.x)."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path

from mcp import types
from mcp.server import MCPServer

SERVER = MCPServer("tenon-notes-lab")
NOTES = {
    "ecg-cleaning.md": "心电数据清洗：检查噪声、异常值和数据质量，保留可追溯的质量标记。",
    "agent-runtime.md": "Agent Runtime 组织上下文、调用模型、执行工具，并把工具结果放入下一轮上下文。",
    "mcp-notes.md": "MCP 通过初始化、工具发现和工具调用，把独立服务接入 Agent。",
}
AUDIT_LOG: Path | None = None
AUDIT_LOCK = threading.Lock()


def record(event: str, **data: object) -> None:
    """Log only this lab's fixed tool inputs; never log credentials."""
    if AUDIT_LOG is None:
        return
    entry = {"event": event, "pid": os.getpid(), "time": time.time(), **data}
    with AUDIT_LOCK:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def result(data: dict, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        isError=error,
    )


@SERVER.tool()
def search_notes(query: str, limit: int = 5) -> types.CallToolResult:
    """Search fixed learning notes by keyword; return filenames for read_document."""
    record("tool_called", tool="search_notes", query=query, limit=limit)
    if not query.strip():
        return result({"error": "query must not be empty"}, error=True)
    if not 1 <= limit <= 10:
        return result({"error": "limit must be between 1 and 10"}, error=True)
    keyword = query.strip().casefold()
    matches = [
        {"name": name, "excerpt": content}
        for name, content in NOTES.items()
        if keyword in name.casefold() or keyword in content.casefold()
    ]
    return result({"matches": matches[:limit]})


@SERVER.tool()
def read_document(name: str, delay_seconds: float = 0.0) -> types.CallToolResult:
    """Read one fixed note by exact filename. Optional delay demonstrates disconnection."""
    record("tool_called", tool="read_document", name=name, delay_seconds=delay_seconds)
    if name not in NOTES:
        return result({"error": f"Document does not exist: {name}"}, error=True)
    if not 0 <= delay_seconds <= 10:
        return result({"error": "delay_seconds must be between 0 and 10"}, error=True)
    time.sleep(delay_seconds)
    return result({"name": name, "content": NOTES[name]})


def main() -> None:
    global AUDIT_LOG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--audit-log", type=Path, default=Path(__file__).parent / ".runs/server.jsonl")
    args = parser.parse_args()
    AUDIT_LOG = args.audit_log.resolve()
    record("server_started", transport=args.transport)
    print(f"notes lab: pid={os.getpid()} transport={args.transport} audit={AUDIT_LOG}", file=sys.stderr)
    if args.transport == "stdio":
        SERVER.run(transport="stdio")
        return

    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse

    app = SERVER.streamable_http_app()
    token = os.environ.get("TENON_NOTES_LAB_TOKEN", "")
    if token:

        async def authenticate(request, call_next):
            provided = request.headers.get("authorization", "")
            if not hmac.compare_digest(provided.encode(), f"Bearer {token}".encode()):
                return PlainTextResponse("Unauthorized", status_code=401)
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
