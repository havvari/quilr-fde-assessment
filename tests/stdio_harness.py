"""Drive an MCP stdio server as a real subprocess and keep its streams apart.

Deliberately hand-rolled rather than built on the SDK client: the property under
test is "nothing but JSON-RPC ever reaches fd 1", and a client that parses fd 1
for you is not in a position to prove it. Here the test owns the pipe and sees
every byte.

Responses are drained by a reader thread and stdin is held open until the last
one has arrived. Writing every frame and then closing stdin looks simpler but
races the server's own shutdown: a synchronous tool body runs in a worker
thread, and on EOF the session's task group is cancelled out from under it, so
the reply is computed and then thrown away.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from types import TracebackType
from typing import IO, Any

from mcp_types import LATEST_PROTOCOL_VERSION

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESPONSE_TIMEOUT = 15.0
EXIT_TIMEOUT = 10.0


@dataclass
class Session:
    """Everything the server emitted, split by stream."""

    stdout: str = ""
    stderr: str = ""
    responses: list[dict[str, Any]] = field(default_factory=list)
    returncode: int | None = None

    def by_id(self, request_id: int) -> dict[str, Any]:
        for response in self.responses:
            if response.get("id") == request_id:
                return response
        raise AssertionError(f"no response with id={request_id}; got {[r.get('id') for r in self.responses]}")

    @property
    def stdout_lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line.strip()]


def initialize_frames() -> list[dict[str, Any]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "stdio-purity-harness", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


def call_tool(request_id: int, name: str, arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _pump(stream: IO[str], sink: queue.Queue[str | None]) -> None:
    for line in stream:
        sink.put(line)
    sink.put(None)


class StdioServer:
    """A running server subprocess with independently drained stdout and stderr."""

    def __init__(self, module: str = "task1_mcp_server") -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        # Unbuffered, so an import-time write reaches the pipe rather than being
        # lost with the process -- otherwise the leak test passes for the wrong reason.
        env["PYTHONUNBUFFERED"] = "1"

        self._proc = subprocess.Popen(
            [sys.executable, "-m", module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr
        self._out: queue.Queue[str | None] = queue.Queue()
        self._err: queue.Queue[str | None] = queue.Queue()
        self._threads = [
            threading.Thread(target=_pump, args=(self._proc.stdout, self._out), daemon=True),
            threading.Thread(target=_pump, args=(self._proc.stderr, self._err), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self.session = Session()

    def send(self, payload: dict[str, Any]) -> None:
        self.send_raw(json.dumps(payload) + "\n")

    def send_raw(self, text: str) -> None:
        """Write verbatim, for frames `json.dumps` would never produce."""
        stdin = self._proc.stdin
        assert stdin is not None
        stdin.write(text)
        stdin.flush()

    def read_line(self, timeout: float = RESPONSE_TIMEOUT) -> str | None:
        """One raw stdout line, or None at end of stream. Not assumed to be JSON."""
        try:
            line = self._out.get(timeout=timeout)
        except queue.Empty:
            message = f"no stdout line within {timeout}s; stderr so far:\n{self._drain_stderr()}"
            raise AssertionError(message) from None
        if line is None:
            return None
        self.session.stdout += line
        return line

    def read_response(self, timeout: float = RESPONSE_TIMEOUT) -> dict[str, Any]:
        line = self.read_line(timeout)
        if line is None:
            raise AssertionError(f"server closed stdout early; stderr:\n{self._drain_stderr()}")
        payload: dict[str, Any] = json.loads(line)
        self.session.responses.append(payload)
        return payload

    def handshake(self) -> None:
        for frame in initialize_frames():
            self.send(frame)
            if "id" in frame:
                self.read_response()

    def _drain_stderr(self) -> str:
        while True:
            try:
                line = self._err.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            self.session.stderr += line
        return self.session.stderr

    def close(self) -> Session:
        stdin = self._proc.stdin
        if stdin is not None and not stdin.closed:
            stdin.close()
        # Drain whatever the server emits on its way out, so a farewell banner on
        # stdout is still caught by the purity assertion.
        while True:
            line = self._out.get(timeout=EXIT_TIMEOUT)
            if line is None:
                break
            self.session.stdout += line
        self._proc.wait(timeout=EXIT_TIMEOUT)
        self._drain_stderr()
        self.session.returncode = self._proc.returncode
        self._release()
        return self.session

    def _release(self) -> None:
        """Close the pipe objects the reader threads were iterating.

        Popen does not close them for us, and leaving them to the garbage
        collector raises ResourceWarning -- which this suite treats as an error,
        because a leaked descriptor per session is a leak in a long-lived proxy too.
        """
        for thread in self._threads:
            thread.join(timeout=EXIT_TIMEOUT)
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self) -> StdioServer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc.poll() is None:
            try:
                self.close()
            except Exception:  # pragma: no cover - teardown of an already-broken server
                self._proc.kill()
                self._proc.wait(timeout=EXIT_TIMEOUT)
                self._release()


def run_session(
    requests: list[dict[str, Any]],
    *,
    module: str = "task1_mcp_server",
    raw_frames: list[str] | None = None,
    expect_responses: int | None = None,
) -> Session:
    """Handshake, send `requests`, collect one response each, then shut down.

    `expect_responses` overrides the response count when some frame is not
    expected to produce one (a notification, or a parse error the server drops).
    """
    with StdioServer(module) as server:
        server.handshake()
        for request in requests:
            server.send(request)
        for text in raw_frames or []:
            server.send_raw(text)
        wanted = expect_responses if expect_responses is not None else sum(1 for r in requests if "id" in r)
        for _ in range(wanted):
            server.read_response()
        return server.close()
