"""Execution deadlines stop real local HTTP reads and retry waits; no model API."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic

import pytest

from backend.domain import UserMessage
from backend.planning import RuleBasedPlanner
from backend.providers import LLMClient, ModelConfig, ModelTransportError
from backend.runtime import AgentRunner
from backend.tools import ToolRegistry


@pytest.mark.parametrize("phase", ["headers", "stream", "json", "trickle", "retry"])
def test_execution_deadline_interrupts_real_http_and_retry_wait(phase: str) -> None:
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(1)
            if phase == "headers":
                release.wait(5)
                return
            self.send_response(429 if phase == "retry" else 200)
            if phase == "retry":
                self.send_header("Retry-After", "10")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_header("Content-Type", "application/json" if phase == "json" else "text/event-stream")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.flush()
            if phase == "trickle":
                while not release.wait(0.04):
                    try:
                        self.wfile.write(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
                        self.wfile.flush()
                    except OSError:
                        break
            else:
                release.wait(5)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="deadline fixture")
    runtime.exchange.messages = [UserMessage(content="hello")]
    runtime.exchange.stream = phase != "json"
    deadline = monotonic() + 0.4
    runtime.services.execution_time_remaining = lambda: max(0.0, deadline - monotonic())
    events = []
    runtime.services.publish = events.append
    client = LLMClient(ModelConfig("local-test", f"http://127.0.0.1:{server.server_port}", "demo", timeout_seconds=10))
    started = monotonic()
    try:
        with pytest.raises(ModelTransportError):
            client.run(runtime)
        assert monotonic() - started < 1.5, "total deadline must stop even continuously trickling data"
        assert len(requests) == 1, "retry waits must not extend the deadline or send another request"
    finally:
        release.set()
        client.transport.session.close()
        server.shutdown()
        server.server_close()
        serving.join(3)
