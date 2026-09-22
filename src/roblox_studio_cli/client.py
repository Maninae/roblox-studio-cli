"""Minimal MCP client that drives Roblox Studio's built-in MCP proxy over stdio.

Roblox Studio (0.739 and later) ships a proxy binary inside the application
bundle at `/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP`, speaking the
Model Context Protocol (revision 2024-11-05) as newline-delimited JSON-RPC 2.0
on stdin/stdout. This module spawns it, runs the `initialize` handshake, and
exposes `list_tools()` / `call_tool()`, so a shell command can reach a running
Studio without registering an MCP server in an agent runtime.

The proxy is only half the bridge. The other half is Roblox Studio itself: until
Studio is running, signed in, and has "Enable Studio as MCP server" switched on,
the proxy answers `initialize` normally but never answers `tools/list`, logging
one WARN on stderr after about 20 seconds. `list_tools` reads that marker and
says so; see its docstring.

Design notes worth knowing before editing:

- Tool names and argument keys are NEVER hardcoded. Roblox iterates on this
  surface; callers discover both from `tools/list` at runtime (see `discovery`).
- stdout is read from the raw fd with `select` and an explicit deadline, never
  via `readline()` on a buffered stream: a buffered reader can swallow a second
  message into Python's own buffer, after which `select` reports "not ready" and
  the caller blocks on data it already has.
- Both pipes are bounded (`MAX_MESSAGE_BYTES`, `STDERR_MAX_LINE_BYTES`): a proxy
  that never sends a newline must not grow this process. See the methods.
- Anything server-controlled that lands in an exception message goes through
  `sanitize_terminal_text` first, because those messages get printed.
"""

import json
import logging
import os
import select
import subprocess
import threading
import time
from collections import deque

from roblox_studio_cli import __version__
from roblox_studio_cli.errors import (
    StudioMcpError,
    StudioMcpProtocolError,
    StudioMcpTimeoutError,
    StudioNotConnectedError,
)
from roblox_studio_cli.mcp_payloads import (
    ToolCallResult,
    ToolDefinition,
    build_tool_definitions,
    parse_tool_call_result,
    raise_for_rpc_error,
)
from roblox_studio_cli.terminal import sanitize_terminal_text

logger = logging.getLogger(__name__)

DEFAULT_STUDIO_MCP_BINARY_PATH = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"
STUDIO_MCP_BINARY_ENV_VAR = "ROBLOX_STUDIO_MCP_BIN"

MCP_PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "roblox-studio-cli"
CLIENT_VERSION = __version__

DEFAULT_INITIALIZE_TIMEOUT_SECONDS = 15.0
DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS = 30.0
DEFAULT_CALL_TOOL_TIMEOUT_SECONDS = 120.0

STDOUT_POLL_INTERVAL_SECONDS = 0.2
STDOUT_READ_CHUNK_BYTES = 65536
# One JSON-RPC line cannot legitimately be this big; a screen capture, the
# largest thing Studio sends, is a few hundred KB of base64.
MAX_MESSAGE_BYTES = 64 * 2**20
STDERR_MAX_LINE_BYTES = 64 * 1024
# Measured against Studio 0.739: the real proxy exits 0 within 10 ms of stdin
# EOF, so this grace only ever pays out for a wedged child.
SHUTDOWN_GRACE_SECONDS = 3.0
KILL_GRACE_SECONDS = 2.0
STDERR_RING_BUFFER_LINES = 200
STDERR_QUOTE_LINES = 6
# A server that keeps handing back a nextCursor would otherwise loop forever.
TOOLS_LIST_PAGE_LIMIT = 50

STUDIO_NOT_ENABLED_MESSAGE = (
    "Studio's MCP server is not enabled. In Roblox Studio: "
    "Assistant settings > MCP Servers > Enable Studio as MCP server."
)
PROXY_NO_TOOLS_STDERR_MARKER = "Timed out waiting for tools to become available"


def resolve_studio_binary_path() -> str:
    """Path to the Studio MCP proxy binary.

    Defaults to the binary inside RobloxStudio.app. `ROBLOX_STUDIO_MCP_BIN`
    overrides it, which covers a non-standard install location, a Studio update
    that moves the binary, and pointing the test suite at a fake server. That
    variable names a program this process then executes with the environment it
    inherited, so treat it like `GIT_SSH`: set it only in a shell you control.
    """
    override = os.environ.get(STUDIO_MCP_BINARY_ENV_VAR, "").strip()
    return override or DEFAULT_STUDIO_MCP_BINARY_PATH


class StudioMcpClient:
    """Synchronous MCP client over one spawned Studio MCP proxy process.

    Usage:
        with StudioMcpClient() as client:
            result = client.call_tool("execute_luau", {"code": "return 1 + 1"})

    One request is in flight at a time, so responses are matched by id and any
    interleaved server notification is skipped. The context manager guarantees
    the child process is reaped even when a call raises.
    """

    def __init__(self, command: list[str] | None = None):
        """Prepare (but do not spawn) a proxy process.

        Args:
            command: argv to launch. Defaults to the resolved binary path.
        """
        self.command = list(command) if command else [resolve_studio_binary_path()]
        self.process: subprocess.Popen | None = None
        self.stdout_fd: int | None = None
        self.stdout_buffer = bytearray()
        self.stdout_scan_position = 0
        self.pending_messages: deque[dict] = deque()
        self.stderr_lines: deque[str] = deque(maxlen=STDERR_RING_BUFFER_LINES)
        self.stderr_lock = threading.Lock()
        self.stderr_thread: threading.Thread | None = None
        self.request_counter = 0
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.server_instructions = ""

    def __enter__(self) -> "StudioMcpClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def start(self, timeout: float = DEFAULT_INITIALIZE_TIMEOUT_SECONDS) -> dict:
        """Spawn the proxy and run the `initialize` + `notifications/initialized` handshake.

        Idempotent: calling it again on a started client returns the cached
        server info without respawning. Any failure after the spawn tears the
        child down before propagating, so a failed handshake cannot leave an
        orphan proxy attached to Studio.

        Returns:
            The server's `initialize` result (protocolVersion, capabilities,
            serverInfo, instructions).
        """
        if self.process is not None:
            return {"serverInfo": self.server_info, "instructions": self.server_instructions}

        logger.debug("launching Studio MCP proxy: %s", self.command)
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as spawn_error:
            raise StudioMcpError(
                f"could not launch the Studio MCP proxy ({self.command[0]}): {spawn_error}. "
                f"A Roblox Studio update can move this binary; set {STUDIO_MCP_BINARY_ENV_VAR} "
                "to its current path."
            ) from spawn_error

        try:
            self.stdout_fd = self.process.stdout.fileno()
            self.stderr_thread = threading.Thread(
                target=self.drain_stderr, name="studio-mcp-stderr", daemon=True
            )
            self.stderr_thread.start()

            result = self.send_request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
                timeout=timeout,
            )
            self.server_info = result.get("serverInfo", {}) or {}
            self.server_capabilities = result.get("capabilities", {}) or {}
            self.server_instructions = str(result.get("instructions", "") or "")
            self.send_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

        logger.debug("initialized against %s", self.server_info)
        return result

    def list_tools(
        self, timeout: float = DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS
    ) -> list[ToolDefinition]:
        """Every tool Studio currently exposes, following `nextCursor` pagination.

        Raises:
            StudioNotConnectedError: the proxy went silent AND logged its "no
                tools" WARN, which is what it does when the MCP toggle is off.
            StudioMcpTimeoutError: silent without that marker, which is an
                ordinary timeout and must not be reported as the toggle.
        """
        self.require_started()
        tools: list[ToolDefinition] = []
        cursor: str | None = None
        deadline = time.monotonic() + timeout

        for _ in range(TOOLS_LIST_PAGE_LIMIT):
            params: dict = {"cursor": cursor} if cursor else {}
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                result = self.send_request("tools/list", params, timeout=remaining)
            except StudioMcpTimeoutError as timeout_error:
                if self.studio_reported_no_tools():
                    raise self.not_connected_error() from timeout_error
                raise

            entries = result.get("tools", [])
            if not isinstance(entries, list):
                raise StudioMcpProtocolError(
                    code=-1, message="tools/list returned a non-list `tools` member"
                )
            tools.extend(build_tool_definitions(entries))

            cursor = result.get("nextCursor")
            if not cursor or not isinstance(cursor, str):
                return tools

        logger.warning(
            "tools/list kept paginating past %d pages; returning what we have",
            TOOLS_LIST_PAGE_LIMIT,
        )
        return tools

    def call_tool(
        self,
        name: str,
        arguments: dict,
        timeout: float = DEFAULT_CALL_TOOL_TIMEOUT_SECONDS,
    ) -> ToolCallResult:
        """Invoke one tool and flatten its `content[]` into text plus images.

        A tool that fails its own business logic comes back as a normal result
        with `is_error=True` and the failure text; only transport and protocol
        failures raise.

        Args:
            name: tool name exactly as `tools/list` reported it.
            arguments: JSON-serializable argument mapping.
            timeout: seconds to wait for the result.
        """
        self.require_started()
        result = self.send_request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        return parse_tool_call_result(result)

    def send_request(self, method: str, params: dict, timeout: float) -> dict:
        """Send one JSON-RPC request and wait for its matching response.

        Raises:
            StudioMcpTimeoutError: no matching response before the deadline.
            StudioMcpProtocolError: the server answered with an `error` object,
                or with a frame that is not a JSON-RPC response at all.
        """
        self.request_counter += 1
        request_id = self.request_counter
        self.send_message(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )

        response = self.read_response(request_id, deadline=time.monotonic() + timeout)
        if response is None:
            raise StudioMcpTimeoutError(
                f"no response to {method!r} within {timeout:.1f}s.{self.stderr_suffix()}"
            )
        raise_for_rpc_error(response)
        result = response.get("result")
        if not isinstance(result, dict):
            raise StudioMcpProtocolError(
                code=-1,
                message=f"{method} returned a non-object result ({type(result).__name__})",
            )
        return result

    def send_message(self, message: dict) -> None:
        """Write one JSON-RPC message as a single newline-terminated line."""
        self.require_started()
        payload = (json.dumps(message) + "\n").encode("utf-8")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (OSError, ValueError) as write_error:
            raise StudioMcpError(
                f"the Studio MCP proxy closed its input: {write_error}.{self.stderr_suffix()}"
            ) from write_error

    def read_response(self, request_id: int, deadline: float) -> dict | None:
        """Read messages until the response with `request_id` arrives or time runs out.

        Server-initiated notifications and stray ids are logged and skipped; only
        the matching response is returned. `None` means the deadline passed.
        """
        while True:
            message = self.read_message(deadline)
            if message is None:
                return None
            if message.get("id") == request_id:
                return message
            logger.debug("skipping %r while waiting for id %d", message.get("method") or message.get("id"), request_id)

    def read_message(self, deadline: float) -> dict | None:
        """Pop the next parsed JSON-RPC message, or `None` once `deadline` passes.

        Reads the raw stdout fd and does its own line buffering so a chunk that
        carries two messages does not leave the second one stranded in a
        buffered reader where `select` cannot see it.
        """
        while True:
            if self.pending_messages:
                return self.pending_messages.popleft()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            ready, _, _ = select.select(
                [self.stdout_fd], [], [], min(remaining, STDOUT_POLL_INTERVAL_SECONDS)
            )
            if not ready:
                if self.process.poll() is not None:
                    raise StudioMcpError(
                        f"the Studio MCP proxy exited (code {self.process.returncode})."
                        f"{self.stderr_suffix()}"
                    )
                continue

            chunk = os.read(self.stdout_fd, STDOUT_READ_CHUNK_BYTES)
            if not chunk:
                raise StudioMcpError(
                    f"the Studio MCP proxy closed its output.{self.stderr_suffix()}"
                )
            self.stdout_buffer += chunk
            self.consume_buffered_lines()
            self.enforce_message_size_limit()

    def consume_buffered_lines(self) -> None:
        """Split whole lines off the stdout buffer and queue the ones that parse as JSON.

        Scanning restarts where the previous scan stopped, so a chunk costs a
        search of its own bytes rather than of everything buffered so far.
        """
        while True:
            newline_index = self.stdout_buffer.find(b"\n", self.stdout_scan_position)
            if newline_index < 0:
                self.stdout_scan_position = len(self.stdout_buffer)
                return
            line = bytes(self.stdout_buffer[:newline_index]).strip()
            del self.stdout_buffer[: newline_index + 1]
            self.stdout_scan_position = 0
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # The proxy occasionally prints non-protocol noise on stdout; it is
                # not fatal, so log it and keep reading for real messages.
                logger.debug("skipping non-JSON stdout line: %r", line[:200])
                continue
            if not isinstance(message, dict):
                logger.debug("skipping non-object JSON-RPC frame: %r", line[:200])
                continue
            self.pending_messages.append(message)

    def enforce_message_size_limit(self) -> None:
        """Kill the proxy rather than buffer an unbounded line.

        The buffer holds only the tail after the last newline, so this trips on
        one absurd message, never on a busy session.
        """
        buffered = len(self.stdout_buffer)
        if buffered <= MAX_MESSAGE_BYTES:
            return
        try:
            self.process.kill()
            self.process.wait(timeout=KILL_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as kill_error:
            logger.warning("could not kill the Studio MCP proxy: %s", kill_error)
        raise StudioMcpError(
            f"the Studio MCP proxy sent {buffered} bytes with no line break "
            f"(limit {MAX_MESSAGE_BYTES}); killed it rather than buffer more."
        )

    def drain_stderr(self) -> None:
        """Thread body: copy the proxy's stderr into a ring buffer, line by line.

        Each `readline` is capped, and the remainder of an over-long line is
        discarded, so a proxy that logs one enormous line cannot grow this
        process without bound.
        """
        stderr_stream = self.process.stderr
        try:
            while True:
                raw_line = stderr_stream.readline(STDERR_MAX_LINE_BYTES)
                if not raw_line:
                    return
                while not raw_line.endswith(b"\n"):
                    # Over-long line: drop the remainder rather than buffer it.
                    extra = stderr_stream.readline(STDERR_MAX_LINE_BYTES)
                    if not extra:
                        break
                    raw_line = extra
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                with self.stderr_lock:
                    self.stderr_lines.append(line)
                logger.debug("StudioMCP stderr: %s", line)
        except (OSError, ValueError):
            # close() got there first and closed the pipe under us.
            return

    def recent_stderr(self, max_lines: int = STDERR_QUOTE_LINES) -> str:
        """The last few stderr lines the proxy logged, newest last."""
        with self.stderr_lock:
            lines = list(self.stderr_lines)
        return "\n".join(lines[-max_lines:])

    def stderr_suffix(self) -> str:
        """Recent stderr, sanitised, formatted for appending to an exception message."""
        stderr_text = sanitize_terminal_text(self.recent_stderr())
        return f"\nProxy stderr:\n{stderr_text}" if stderr_text else ""

    def not_connected_error(self) -> StudioNotConnectedError:
        """Build the "turn the toggle on" error, quoting the proxy's own WARN."""
        return StudioNotConnectedError(f"{STUDIO_NOT_ENABLED_MESSAGE}{self.stderr_suffix()}")

    def studio_reported_no_tools(self) -> bool:
        """True when the proxy logged its "no tools" WARN, meaning Studio never attached."""
        return PROXY_NO_TOOLS_STDERR_MARKER in self.recent_stderr(
            max_lines=STDERR_RING_BUFFER_LINES
        )

    def require_started(self) -> None:
        """Guard for calls that need a live process."""
        if self.process is None:
            raise StudioMcpError(
                "client is not started; use `with StudioMcpClient() as client:` or call start()"
            )

    def close(self) -> None:
        """Close stdin, then terminate and reap the proxy. Safe to call twice."""
        if self.process is None:
            return
        process, self.process = self.process, None

        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

        try:
            process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning("Studio MCP proxy ignored SIGTERM; killing it")
                process.kill()
                process.wait()

        # Closing stderr while the drain thread is parked inside readline() on the
        # same buffered reader blocks on that thread's lock, which is how close()
        # used to take 90 s. Leave the pipe to the daemon thread unless the drain
        # has actually finished.
        drain_finished = True
        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=KILL_GRACE_SECONDS)
            drain_finished = not self.stderr_thread.is_alive()
            self.stderr_thread = None
        if drain_finished and process.stderr is not None:
            try:
                process.stderr.close()
            except OSError:
                pass

        self.stdout_fd = None
        self.stdout_buffer = bytearray()
        self.stdout_scan_position = 0
        self.pending_messages.clear()
