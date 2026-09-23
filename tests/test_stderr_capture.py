"""Unit tests for the proxy's stderr side: the ring buffer and what it quotes.

The drain thread itself is exercised end to end in `test_client.py`, against a
fake server that floods the pipe. What is left here needs no process at all: the
buffer is a deque a test can fill by hand, and the question is how much of it an
exception message is allowed to print.
"""

from roblox_studio_cli.stderr_capture import (
    STDERR_MAX_LINE_BYTES,
    STDERR_QUOTE_LINES,
    ProxyStderrCapture,
)
from roblox_studio_cli.terminal import MAX_DIAGNOSTIC_TEXT_CHARS


def test_quoted_stderr_is_capped_one_line_at_a_time():
    """Six lines the proxy chose the length of used to be six lines of 64 KB.

    The ring buffer bounds how many lines are kept and how long each may be on
    the way in; this is the second half, bounding what an exception message
    prints out of them.
    """
    capture = ProxyStderrCapture()
    for _ in range(STDERR_QUOTE_LINES + 4):
        capture.stderr_lines.append("x" * STDERR_MAX_LINE_BYTES)

    suffix = capture.stderr_suffix()
    assert "Proxy stderr:" in suffix
    assert len(suffix) < STDERR_QUOTE_LINES * (MAX_DIAGNOSTIC_TEXT_CHARS + 1) + 32, len(suffix)
