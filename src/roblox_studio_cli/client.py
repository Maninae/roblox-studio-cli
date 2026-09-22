"""Minimal MCP client that drives Roblox Studio's built-in MCP proxy over stdio.

Roblox Studio (0.739 and later) ships a proxy binary inside the application
bundle at `/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP`. It speaks
the Model Context Protocol (spec revision 2024-11-05) as newline-delimited
JSON-RPC 2.0 on stdin/stdout. This module spawns it, performs the `initialize`
handshake, and exposes `list_tools()` / `call_tool()`, so a shell command can
reach a running Studio without registering an MCP server in an agent runtime.

The proxy is only half the bridge. The other half is Roblox Studio itself: until
Studio is running, signed in, and has "Enable Studio as MCP server" switched on,
the proxy answers `initialize` normally but never answers `tools/list`. It logs
`WARN ... Timed out waiting for tools to become available` on stderr after about
20 seconds and stays silent on stdout. `list_tools()` turns that silence into a
`StudioNotConnectedError` carrying the enable-the-toggle instruction, because a
bare timeout is indistinguishable from a hang and sends the reader hunting in
the wrong place.

Design notes worth knowing before editing:

- Tool names and argument keys are NEVER hardcoded. Roblox iterates on this
  surface; callers discover both from `tools/list` at runtime.
- stdout is read from the raw fd with `select` and an explicit deadline, not via
  `readline()` on a buffered text stream. A buffered reader can swallow a second
  message into Python's internal buffer, after which `select` reports "not
  ready" and the caller blocks on data it already has.
- stderr is drained by a daemon thread into a ring buffer. Draining prevents the
  child blocking on a full pipe, and the ring buffer is what lets errors quote
  the proxy's own WARN text back to the user.
"""

import base64
import json
import logging
import os
import select
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_STUDIO_MCP_BINARY_PATH = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"
STUDIO_MCP_BINARY_ENV_VAR = "ROBLOX_STUDIO_MCP_BIN"

MCP_PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "roblox-studio-cli"
CLIENT_VERSION = "0.1.0"

DEFAULT_INITIALIZE_TIMEOUT_SECONDS = 15.0
DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS = 30.0
DEFAULT_CALL_TOOL_TIMEOUT_SECONDS = 120.0

STDOUT_POLL_INTERVAL_SECONDS = 0.2
STDOUT_READ_CHUNK_BYTES = 65536
SHUTDOWN_GRACE_SECONDS = 3.0
KILL_GRACE_SECONDS = 2.0
STDERR_RING_BUFFER_LINES = 200
STDERR_QUOTE_LINES = 6
# A server that keeps handing back a nextCursor would otherwise loop forever.
TOOLS_LIST_PAGE_LIMIT = 50

STUDIO_NOT_ENABLED_MESSAGE = (
    "Studio's MCP server is not enabled. In Roblox Studio: "
    "Assistant menu > three dots > Manage MCP Servers > Enable Studio as MCP server."
)
PROXY_NO_TOOLS_STDERR_MARKER = "Timed out waiting for tools to become available"

MIME_TYPE_TO_FILE_EXTENSION: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
DEFAULT_IMAGE_FILE_EXTENSION = ".png"


class StudioMcpError(Exception):
    """Base class for every failure raised by this client."""


class StudioMcpTimeoutError(StudioMcpError):
    """The proxy did not answer a request before its deadline."""


class StudioNotConnectedError(StudioMcpError):
    """The proxy is alive but Roblox Studio is not exposing its tools.

    Almost always means the "Enable Studio as MCP server" toggle is off, or
    Studio is closed or signed out. The message carries the enable instructions.
    """


class StudioMcpProtocolError(StudioMcpError):
    """The proxy returned a JSON-RPC `error` object (unknown tool, bad args...)."""

    def __init__(self, code: int, message: str, data: object = None):
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


@dataclass(frozen=True)
class ToolDefinition:
    """One entry from `tools/list`: what the tool is called and what it accepts."""

    name: str
    description: str
    input_schema: dict

    @property
    def argument_names(self) -> list[str]:
        """Every argument key the tool's JSON Schema declares, in schema order."""
        return list(self.input_schema.get("properties", {}).keys())

    @property
    def required_argument_names(self) -> list[str]:
        """The subset of arguments the schema marks required."""
        required = self.input_schema.get("required", [])
        return [name for name in required if isinstance(name, str)]

    def property_schema(self, argument_name: str) -> dict:
        """JSON Schema fragment for one argument, or `{}` when the tool has no such argument."""
        properties = self.input_schema.get("properties", {})
        schema = properties.get(argument_name, {})
        return schema if isinstance(schema, dict) else {}


@dataclass(frozen=True)
class ToolImage:
    """An `{"type": "image"}` content item from a tool result."""

    mime_type: str
    data_base64: str

    def decoded_bytes(self) -> bytes:
        """Raw image bytes. Raises `StudioMcpError` when the payload is not valid base64."""
        try:
            return base64.b64decode(self.data_base64, validate=True)
        except (ValueError, TypeError) as decode_error:
            raise StudioMcpError(
                f"tool returned undecodable image data: {decode_error}"
            ) from decode_error

    def file_extension(self) -> str:
        """Filename suffix matching the declared MIME type (`.png` when unknown)."""
        return MIME_TYPE_TO_FILE_EXTENSION.get(self.mime_type.lower(), DEFAULT_IMAGE_FILE_EXTENSION)


@dataclass(frozen=True)
class ToolCallResult:
    """A flattened `tools/call` result: the text, the images, and the untouched payload."""

    is_error: bool
    text: str
    images: list[ToolImage] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def resolve_studio_binary_path() -> str:
    """Path to the Studio MCP proxy binary.

    Defaults to the binary inside RobloxStudio.app. `ROBLOX_STUDIO_MCP_BIN`
    overrides it, which covers a non-standard install location, a Studio update
    that moves the binary, and pointing the test suite at a fake server.
    """
    override = os.environ.get(STUDIO_MCP_BINARY_ENV_VAR, "").strip()
    return override or DEFAULT_STUDIO_MCP_BINARY_PATH


class StudioMcpClient:
    """Synchronous MCP client over one spawned Studio MCP proxy process.

    Usage:
        with StudioMcpClient() as client:
            for tool in client.list_tools():
                print(tool.name, tool.argument_names)
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
        self.stdout_buffer = b""
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
        server info without respawning.

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
        self.server_instructions = result.get("instructions", "") or ""
        self.send_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        logger.debug("initialized against %s", self.server_info)
        return result

    def list_tools(
        self, timeout: float = DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS
    ) -> list[ToolDefinition]:
        """Every tool Studio currently exposes, following `nextCursor` pagination.

        Raises:
            StudioNotConnectedError: the proxy went silent, which is what it does
                when Studio is closed or the MCP toggle is off.
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
                raise self.not_connected_error() from timeout_error

            for entry in result.get("tools", []):
                if not isinstance(entry, dict) or "name" not in entry:
                    continue
                input_schema = entry.get("inputSchema", {})
                tools.append(
                    ToolDefinition(
                        name=entry["name"],
                        description=entry.get("description", "") or "",
                        input_schema=input_schema if isinstance(input_schema, dict) else {},
                    )
                )

            cursor = result.get("nextCursor")
            if not cursor:
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
            StudioMcpProtocolError: the server answered with an `error` object.
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
        if "error" in response:
            error = response["error"] or {}
            raise StudioMcpProtocolError(
                code=error.get("code", -1),
                message=error.get("message", "unknown error"),
                data=error.get("data"),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise StudioMcpError(f"{method!r} returned a non-object result: {result!r}")
        return result

    def send_message(self, message: dict) -> None:
        """Write one JSON-RPC message as a single newline-terminated line."""
        self.require_started()
        payload = (json.dumps(message) + "\n").encode("utf-8")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as write_error:
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
            if "method" in message:
                logger.debug("ignoring server-initiated %s", message.get("method"))
            else:
                logger.debug("ignoring response for unexpected id %r", message.get("id"))

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

    def consume_buffered_lines(self) -> None:
        """Split whole lines off the stdout buffer and queue the ones that parse as JSON."""
        while b"\n" in self.stdout_buffer:
            raw_line, self.stdout_buffer = self.stdout_buffer.split(b"\n", 1)
            line = raw_line.strip()
            if not line:
                continue
            try:
                self.pending_messages.append(json.loads(line))
            except json.JSONDecodeError:
                # The proxy occasionally prints non-protocol noise on stdout; it is
                # not fatal, so log it and keep reading for real messages.
                logger.debug("skipping non-JSON stdout line: %r", line[:200])

    def drain_stderr(self) -> None:
        """Thread body: copy the proxy's stderr into a ring buffer, line by line."""
        stderr_stream = self.process.stderr
        for raw_line in iter(stderr_stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            with self.stderr_lock:
                self.stderr_lines.append(line)
            logger.debug("StudioMCP stderr: %s", line)

    def recent_stderr(self, max_lines: int = STDERR_QUOTE_LINES) -> str:
        """The last few stderr lines the proxy logged, newest last."""
        with self.stderr_lock:
            lines = list(self.stderr_lines)
        return "\n".join(lines[-max_lines:])

    def stderr_suffix(self) -> str:
        """Recent stderr formatted for appending to an exception message."""
        stderr_text = self.recent_stderr()
        return f"\nProxy stderr:\n{stderr_text}" if stderr_text else ""

    def not_connected_error(self) -> StudioNotConnectedError:
        """Build the "turn the toggle on" error, quoting the proxy's own WARN when present."""
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

        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=KILL_GRACE_SECONDS)
            self.stderr_thread = None
        try:
            if process.stderr is not None:
                process.stderr.close()
        except OSError:
            pass
        self.stdout_fd = None
        self.stdout_buffer = b""
        self.pending_messages.clear()


def parse_tool_call_result(result: dict) -> ToolCallResult:
    """Flatten an MCP `tools/call` result into text, images, and the raw payload.

    Handles the three 2024-11-05 content shapes: `text`, `image` (base64 plus
    mimeType), and embedded `resource` (whose `text` member, when present, reads
    as more text). Unknown content types are ignored rather than fatal, so a
    future Roblox content type cannot break an otherwise good call.
    """
    text_parts: list[str] = []
    images: list[ToolImage] = []

    for item in result.get("content", []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text_parts.append(str(item.get("text", "")))
        elif item_type == "image":
            images.append(
                ToolImage(
                    mime_type=str(item.get("mimeType", "image/png")),
                    data_base64=str(item.get("data", "")),
                )
            )
        elif item_type == "resource":
            resource = item.get("resource", {})
            if isinstance(resource, dict) and resource.get("text"):
                text_parts.append(str(resource["text"]))
        else:
            logger.debug("ignoring unknown content type %r", item_type)

    return ToolCallResult(
        is_error=bool(result.get("isError", False)),
        text="\n".join(text_parts),
        images=images,
        raw=result,
    )
