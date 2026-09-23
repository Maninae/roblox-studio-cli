"""The proxy's other pipe: what StudioMCP logs on stderr, and what we quote back.

Two things come off that pipe, and neither of them knows a request exists, which
is why they sit on this side of the line rather than inside the client:

- Diagnostics worth quoting when something fails. A bare "no response in 30.0s"
  and the same line with the proxy's last few log lines under it are different
  messages to the person reading them.
- One marker that changes the diagnosis. With Studio's "Enable Studio as MCP
  server" toggle off, the proxy answers `initialize` normally and then never
  answers `tools/list`, logging `PROXY_NO_TOOLS_STDERR_MARKER` after about
  twenty seconds. Silence WITH that line is the toggle; silence without it is an
  ordinary timeout, and reporting one as the other sends the caller to the wrong
  setting.

The pipe is bounded on the way in and capped again on the way out: each
`readline` stops at `STDERR_MAX_LINE_BYTES` and lands in a ring buffer of
`STDERR_RING_BUFFER_LINES` lines, so a proxy that logs without stopping cannot
grow this process, and every line an exception quotes goes through
`sanitize_diagnostic_line` first, because the proxy chooses both what those
lines say and how long they are.
"""

import logging
import threading
from collections import deque
from typing import IO

from roblox_studio_cli.terminal import sanitize_diagnostic_line

logger = logging.getLogger(__name__)

STDERR_MAX_LINE_BYTES = 64 * 1024
STDERR_RING_BUFFER_LINES = 200
STDERR_QUOTE_LINES = 6

PROXY_NO_TOOLS_STDERR_MARKER = "Timed out waiting for tools to become available"


class ProxyStderrCapture:
    """One proxy process's stderr, drained on a thread into a bounded ring buffer.

    Owned by `StudioMcpClient`, which hands it the child's stderr stream once the
    child exists and reads diagnostics back out of it. Everything here is safe to
    call from the main thread while the drain runs: the buffer is only ever
    touched under `stderr_lock`.
    """

    def __init__(self):
        """Prepare an empty buffer. Nothing is read until `start` gets a stream."""
        self.stderr_lines: deque[str] = deque(maxlen=STDERR_RING_BUFFER_LINES)
        self.stderr_lock = threading.Lock()
        self.stderr_stream: IO[bytes] | None = None
        self.stderr_thread: threading.Thread | None = None

    def start(self, stderr_stream: IO[bytes]) -> None:
        """Begin draining `stderr_stream` on a daemon thread.

        Daemon, because this thread does nothing but read a pipe the parent owns:
        a drain still parked in `readline` must never be what keeps the process
        alive.
        """
        self.stderr_stream = stderr_stream
        self.stderr_thread = threading.Thread(
            target=self.drain_stderr, name="studio-mcp-stderr", daemon=True
        )
        self.stderr_thread.start()

    def drain_stderr(self) -> None:
        """Thread body: copy the proxy's stderr into a ring buffer, line by line.

        Each `readline` is capped, and an over-long line is read to its end one
        capped chunk at a time, keeping only the last chunk. So a proxy that logs
        one enormous line cannot grow this process, and what lands in the ring
        buffer is that line's TAIL, which is where a log line puts its message.
        """
        stderr_stream = self.stderr_stream
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

    def join_drain(self, timeout: float) -> bool:
        """Wait up to `timeout` for the drain thread to finish. True when it has.

        The caller closes the stderr pipe only on a True. Closing it while the
        drain is parked inside `readline()` on the same buffered reader blocks on
        that thread's lock, which is how `close()` used to take 90 s.
        """
        if self.stderr_thread is None:
            return True
        self.stderr_thread.join(timeout=timeout)
        drain_finished = not self.stderr_thread.is_alive()
        self.stderr_thread = None
        return drain_finished

    def recent_stderr(self, max_lines: int = STDERR_QUOTE_LINES) -> str:
        """The last few stderr lines the proxy logged, newest last."""
        with self.stderr_lock:
            lines = list(self.stderr_lines)
        return "\n".join(lines[-max_lines:])

    def stderr_suffix(self) -> str:
        """Recent stderr, sanitised and capped per line, for appending to an exception.

        Each quoted line is diagnostic chrome whose length the proxy chose, and
        `STDERR_MAX_LINE_BYTES` lets a 64 KB one into the ring buffer, so six of
        them used to mean 384 KB under a one-line message.
        """
        quoted = [sanitize_diagnostic_line(line) for line in self.recent_stderr().splitlines()]
        stderr_text = "\n".join(line for line in quoted if line)
        return f"\nProxy stderr:\n{stderr_text}" if stderr_text else ""

    def studio_reported_no_tools(self) -> bool:
        """True when the proxy logged its "no tools" WARN, meaning Studio never attached."""
        return PROXY_NO_TOOLS_STDERR_MARKER in self.recent_stderr(
            max_lines=STDERR_RING_BUFFER_LINES
        )
