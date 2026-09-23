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
  the caller blocks on data it already has. What those raw bytes mean is
  `framing`'s job: this module hands each read to a `StdoutFrameReader` and takes
  parsed messages back.
- The protocol runs both ways. A server makes requests of its client too, and
  numbers them from 1 as we do, so a frame carrying our id can be a question
  rather than an answer: only one with no `method` is ours. Questions get one
  reply, -32601, so a conformant server stops waiting on us. That reply is a
  single non-blocking write, dropped when the pipe is full, because the caller
  is waiting on their own answer while we are polite.
- Both pipes are bounded. stdout's three bounds live in `framing` (a per-frame
  cap, a per-request parse budget, and dropping a large frame that answers
  another request); the other pipe belongs to `stderr_capture`, ring buffer and
  all, and this module only hands it the stream and quotes what it kept.
- The request write is non-blocking and deadline-driven. A server that stops
  reading its stdin used to wedge this process forever once the pipe buffer
  filled, which a 300 KB `--file` reaches on the first write.
- Anything server-controlled that lands in an exception message goes through
  `sanitize_diagnostic_line` first, because those messages get printed, and the
  proxy chooses both what they say and how long they are.
"""

import json
import logging
import os
import select
import subprocess
import time

from roblox_studio_cli import __version__
from roblox_studio_cli.errors import (
    UNKNOWN_ERROR_CODE,
    StudioMcpError,
    StudioMcpProtocolError,
    StudioMcpTimeoutError,
    StudioNotConnectedError,
)
from roblox_studio_cli.framing import (
    MAX_REQUEST_TOTAL_BYTES,
    MAX_TOOLS_LIST_TOTAL_BYTES,
    StdoutFrameReader,
    response_matches_request,
)
from roblox_studio_cli.mcp_payloads import (
    ToolCallResult,
    ToolDefinition,
    build_tool_definitions,
    method_not_found_reply,
    parse_tool_call_result,
    raise_for_rpc_error,
)
from roblox_studio_cli.stderr_capture import ProxyStderrCapture

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
# A write that cannot make progress in this long is a proxy that stopped reading.
DEFAULT_WRITE_TIMEOUT_SECONDS = 15.0
# The most a decline may be. POSIX makes a pipe write of at most PIPE_BUF bytes
# all-or-nothing, which is what lets the reply be attempted once and abandoned:
# what reaches the proxy is the whole frame or none of it, never half of one.
MAX_DECLINE_REPLY_BYTES = select.PIPE_BUF
# Measured against Studio 0.739: the real proxy exits 0 within 10 ms of stdin
# EOF, so this grace only ever pays out for a wedged child.
SHUTDOWN_GRACE_SECONDS = 3.0
KILL_GRACE_SECONDS = 2.0
# A server that keeps handing back a nextCursor would otherwise loop forever.
TOOLS_LIST_PAGE_LIMIT = 50

STUDIO_NOT_ENABLED_MESSAGE = (
    "Studio's MCP server is not enabled. In Roblox Studio: "
    "Assistant settings > MCP Servers > Enable Studio as MCP server."
)


def handshake_timeout(caller_timeout: float) -> float:
    """The handshake's bound when a caller named one: the smaller of the two.

    `--timeout` is a promise about the command, so it has to reach the exchanges
    that happen BEFORE the tool call. Left on this module's default, a proxy
    that accepts `initialize` and never answers it held `luau --timeout 1` for
    18.1 s. It only ever shortens the wait: a caller asking for five minutes of
    tool time is not asking for five minutes of handshake.
    """
    return min(caller_timeout, DEFAULT_INITIALIZE_TIMEOUT_SECONDS)


def tools_list_timeout(caller_timeout: float) -> float:
    """The same rule for `tools/list`, which every convenience command runs first.

    Measured against a proxy that answers the handshake and then never answers
    `tools/list` (Studio with its MCP toggle off): `luau 'return 1' --timeout 1`
    took 30.3 s on this module's default. `doctor` and `tools` pass their own
    `--timeout` straight through instead, because for those two the number the
    caller typed IS the tools/list wait rather than a cap on it.
    """
    return min(caller_timeout, DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS)


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
        self.stdin_fd: int | None = None
        self.frames = StdoutFrameReader()
        self.stderr_capture = ProxyStderrCapture()
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
            self.stdin_fd = self.process.stdin.fileno()
            # Writes go to the raw fd, never through the buffered writer, so the
            # deadline in `write_all` is the only thing that can end a write.
            os.set_blocking(self.stdin_fd, False)
            self.stderr_capture.start(self.process.stderr)

            result = self.send_request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
                timeout=timeout,
            )
            # Both of these are whatever the server sent. `or {}` let a bare
            # string or a number through, and the next module to call `.get` on
            # it raised an AttributeError from three layers down.
            server_info = result.get("serverInfo")
            capabilities = result.get("capabilities")
            self.server_info = server_info if isinstance(server_info, dict) else {}
            self.server_capabilities = capabilities if isinstance(capabilities, dict) else {}
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
                ordinary timeout and must not be reported as the toggle, or the
                deadline ran out between pages (see the loop).
        """
        self.require_started()
        tools: list[ToolDefinition] = []
        cursor: str | None = None
        deadline = time.monotonic() + timeout
        # One budget for the whole paginated list, so a server cannot serve 4 MB
        # per page for fifty pages.
        byte_budget = MAX_TOOLS_LIST_TOTAL_BYTES

        for page_number in range(1, TOOLS_LIST_PAGE_LIMIT + 1):
            params: dict = {"cursor": cursor} if cursor else {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Asking anyway (what `max(..., 0.0)` did) fails inside `write_all`,
                # which blames the proxy for not reading. It was reading, and the
                # list is simply longer than the deadline, which is worth saying.
                raise StudioMcpTimeoutError(
                    f"tools/list had no time left to ask for page {page_number}; "
                    f"{len(tools)} tools arrived before it.{self.stderr_capture.stderr_suffix()}"
                )
            try:
                result = self.send_request(
                    "tools/list", params, timeout=remaining, byte_budget=byte_budget
                )
            except StudioMcpTimeoutError as timeout_error:
                if self.stderr_capture.studio_reported_no_tools():
                    raise self.not_connected_error() from timeout_error
                raise

            entries = result.get("tools", [])
            if not isinstance(entries, list):
                raise StudioMcpProtocolError(
                    code=UNKNOWN_ERROR_CODE, message="tools/list returned a non-list `tools` member"
                )
            tools.extend(build_tool_definitions(entries))

            byte_budget -= self.frames.bytes_consumed
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

    def send_request(
        self,
        method: str,
        params: dict,
        timeout: float,
        byte_budget: int = MAX_REQUEST_TOTAL_BYTES,
    ) -> dict:
        """Send one JSON-RPC request and wait for its matching response.

        `timeout` bounds the whole exchange, the write included: a server that
        stops reading its stdin is as much a timeout as one that never answers.
        `byte_budget` caps what may be parsed while waiting.

        Raises:
            StudioMcpTimeoutError: the write stalled, or no matching response
                arrived, before the deadline.
            StudioMcpProtocolError: the server answered with an `error` object,
                or with a frame that is not a JSON-RPC response at all.
        """
        self.request_counter += 1
        request_id = self.request_counter
        self.frames.begin_request(byte_budget)
        deadline = time.monotonic() + timeout
        self.send_message(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            deadline=deadline,
        )

        response = self.read_response(request_id, deadline=deadline)
        if response is None:
            raise StudioMcpTimeoutError(
                f"no response to {method!r} within {timeout:.1f}s.{self.stderr_capture.stderr_suffix()}"
            )
        raise_for_rpc_error(response)
        result = response.get("result")
        if not isinstance(result, dict):
            raise StudioMcpProtocolError(
                code=UNKNOWN_ERROR_CODE,
                message=f"{method} returned a non-object result ({type(result).__name__})",
            )
        return result

    def send_message(self, message: dict, deadline: float | None = None) -> None:
        """Write one JSON-RPC message as a single newline-terminated line."""
        self.require_started()
        payload = (json.dumps(message) + "\n").encode("utf-8")
        if deadline is None:
            deadline = time.monotonic() + DEFAULT_WRITE_TIMEOUT_SECONDS
        self.write_all(payload, deadline)

    def write_all(self, payload: bytes, deadline: float) -> None:
        """Write every byte to the proxy's stdin, or give up at `deadline`.

        The fd is non-blocking and this loop is select-driven, because a pipe
        holds about 64 KB: a proxy that stops reading (hung, or deliberately)
        used to park this process inside `write()` forever on the first `--file`
        larger than that buffer, whatever `--timeout` said.

        Raises:
            StudioMcpTimeoutError: the proxy stopped consuming its input.
            StudioMcpError: the pipe is closed, or the proxy exited.
        """
        remaining_payload = memoryview(payload)
        while remaining_payload:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise StudioMcpTimeoutError(
                    f"the Studio MCP proxy stopped reading its input "
                    f"({len(payload) - len(remaining_payload)} of {len(payload)} bytes written)."
                    f"{self.stderr_capture.stderr_suffix()}"
                )
            _, writable, _ = select.select(
                [], [self.stdin_fd], [], min(remaining_seconds, STDOUT_POLL_INTERVAL_SECONDS)
            )
            if not writable:
                if self.process.poll() is not None:
                    raise StudioMcpError(
                        f"the Studio MCP proxy exited (code {self.process.returncode})."
                        f"{self.stderr_capture.stderr_suffix()}"
                    )
                continue
            try:
                written = os.write(self.stdin_fd, remaining_payload)
            except BlockingIOError:
                continue
            except (OSError, ValueError) as write_error:
                raise StudioMcpError(
                    f"the Studio MCP proxy closed its input: {write_error}.{self.stderr_capture.stderr_suffix()}"
                ) from write_error
            remaining_payload = remaining_payload[written:]

    def read_response(self, request_id: int, deadline: float) -> dict | None:
        """Read messages until the response with `request_id` arrives or time runs out.

        Notifications and stray ids are logged and skipped, a request the
        server made of us is declined, and only the matching response comes
        back. `None` means the deadline passed.
        """
        while True:
            message = self.read_message(deadline, awaited_id=request_id)
            if message is None:
                return None
            if response_matches_request(message, request_id):
                return message
            self.decline_server_request(message)
            logger.debug("skipping %r while waiting for id %d", message.get("method") or message.get("id"), request_id)

    def decline_server_request(self, message: dict) -> None:
        """Answer a request the BRIDGE made of us with "method not found".

        A conformant server waits for a reply to every request it sends, so
        silence parks it until its own timeout. Being polite must not cost the
        caller their answer, though, which is why this is ONE non-blocking write
        and never a loop. `write_all` retries against the caller's deadline and
        stops reading stdout while it does, so a burst of questions from a
        server that has paused reading turned the courtesy into the caller's
        whole timeout: measured on the fake bridge, 150 questions delayed the
        answer until the 3 s deadline, and 700 ended in a timeout with the
        answer already sitting on the pipe.

        So the reply is dropped rather than retried when the pipe is full
        (`BlockingIOError`), and skipped outright when it would exceed
        `MAX_DECLINE_REPLY_BYTES`, since only a write that small is atomic.
        """
        if self.stdin_fd is None:
            return
        reply = method_not_found_reply(message)
        if reply is None:
            return
        payload = (json.dumps(reply) + "\n").encode("utf-8")
        if len(payload) > MAX_DECLINE_REPLY_BYTES:
            logger.debug("not declining a server request: a %d byte reply", len(payload))
            return
        try:
            os.write(self.stdin_fd, payload)
        except (OSError, ValueError) as reply_error:
            # BlockingIOError lands here too: the proxy is not reading its
            # input, and a courtesy is not worth waiting on.
            logger.debug("could not decline a server-to-client request: %s", reply_error)

    def read_message(self, deadline: float, awaited_id: int | None = None) -> dict | None:
        """Pop the next parsed JSON-RPC message, or `None` once `deadline` passes.

        Reads the raw stdout fd and hands every chunk to the frame reader, which
        does the line buffering so a chunk carrying two messages does not leave
        the second one stranded where `select` cannot see it. `awaited_id` lets
        an oversized frame belonging to some other request be dropped unparsed.
        """
        while True:
            message = self.frames.next_message()
            if message is not None:
                return message

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
                        f"{self.stderr_capture.stderr_suffix()}"
                    )
                continue

            chunk = os.read(self.stdout_fd, STDOUT_READ_CHUNK_BYTES)
            if not chunk:
                raise StudioMcpError(
                    f"the Studio MCP proxy closed its output.{self.stderr_capture.stderr_suffix()}"
                )
            self.frames.feed(chunk, awaited_id)
            self.enforce_message_size_limit()

    def enforce_message_size_limit(self) -> None:
        """Kill the proxy rather than let the frame reader buffer an unbounded line.

        The limit itself lives in `framing`; killing the child is process
        business, which is why the two halves sit on opposite sides of that line.
        """
        if not self.frames.holds_an_oversized_line():
            return
        try:
            self.process.kill()
            self.process.wait(timeout=KILL_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as kill_error:
            logger.warning("could not kill the Studio MCP proxy: %s", kill_error)
        raise StudioMcpError(self.frames.oversized_line_message())

    def not_connected_error(self) -> StudioNotConnectedError:
        """Build the "turn the toggle on" error, quoting the proxy's own WARN."""
        suffix = self.stderr_capture.stderr_suffix()
        return StudioNotConnectedError(f"{STUDIO_NOT_ENABLED_MESSAGE}{suffix}")

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

        # Only once the drain has finished: closing the pipe under a thread still
        # parked in readline() blocks on that thread's lock (see `join_drain`).
        if self.stderr_capture.join_drain(KILL_GRACE_SECONDS) and process.stderr is not None:
            try:
                process.stderr.close()
            except OSError:
                pass

        self.stdout_fd = None
        self.stdin_fd = None
        self.frames.reset()
