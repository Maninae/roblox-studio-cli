"""Frame the proxy's stdout into JSON-RPC messages, under fixed byte budgets.

`client` owns the process, the handshake and the request/response exchange. This
module owns the stretch between "some bytes arrived on stdout" and "here is a
parsed message", which is a job with its own hazards:

- Line buffering, because one read can carry two frames, or half of one. The
  scan resumes where the last one stopped, so a chunk costs a search of its own
  bytes rather than of everything buffered so far.
- Parsing, defensively. A frame that is not JSON is noise and is skipped; a frame
  that is JSON-shaped but unreadable (undecodable bytes, nesting past the
  parser's limit, an integer too long to convert) is a protocol error rather than
  a traceback.
- Byte budgets. A cap on one frame is not a cap on memory, because parsing JSON
  multiplies size many times over, so a request carries a total parse budget as
  well, and a line that never terminates is refused outright.
- The id peek. Past `LARGE_FRAME_BYTES` a frame is worth identifying before it is
  parsed: one that positively answers a DIFFERENT request is dropped for the cost
  of a regex over both ends of the line instead of a full parse.

Nothing here touches the process or the file descriptors, so a framing bug can be
reproduced by handing `feed()` some bytes.
"""

import json
import logging
import re
from collections import deque
from functools import lru_cache

from roblox_studio_cli.errors import StudioMcpError, StudioMcpProtocolError

logger = logging.getLogger(__name__)

# One JSON-RPC line cannot legitimately be this big; a screen capture, the
# largest thing Studio sends, is a few hundred KB of base64. The old 64 MB cap
# bounded one frame and not the process: ten 4 MB frames parsed into roughly a
# gigabyte of Python objects, because JSON parsing multiplies size by 20 or more.
MAX_MESSAGE_BYTES = 8 * 2**20
# Total bytes parsed while answering one request, which is the cap the per-frame
# limit above is not. tools/list gets its own, smaller budget across all pages.
MAX_REQUEST_TOTAL_BYTES = 16 * 2**20
MAX_TOOLS_LIST_TOTAL_BYTES = 4 * 2**20
# Over this, a frame is worth identifying before parsing it: if it answers a
# different request id, dropping it costs a regex over both ends of the line
# instead of a full parse.
LARGE_FRAME_BYTES = 64 * 1024
FRAME_ID_PEEK_BYTES = 4096
# Bounded digits: an id long enough to trip Python's integer conversion limit
# must not match, so such a frame falls through to the ordinary parse path.
FRAME_ID_PATTERN = re.compile(rb'"id"\s*:\s*([0-9]{1,18})(?![0-9])')
NON_JSON_PREVIEW_CHARS = 200
# One pattern per in-flight request id, and ids only ever count upward.
AWAITED_ID_PATTERN_CACHE_SIZE = 32


@lru_cache(maxsize=AWAITED_ID_PATTERN_CACHE_SIZE)
def awaited_id_pattern(awaited_id: int) -> re.Pattern:
    """Match one specific id the way a JSON-RPC frame writes it, bare or quoted.

    A server echoes a numeric request id as `"id": 7` or as `"id": "7"`, and the
    peek has to recognise both, because mistaking our own answer for somebody
    else's costs the caller the entire timeout. The bare form ends on a
    non-digit, so a wait for 7 is not satisfied by 71.
    """
    return re.compile(rb'"id"\s*:\s*(?:%d(?![0-9])|"%d")' % (awaited_id, awaited_id))


class StdoutFrameReader:
    """Byte buffer over the proxy's stdout that yields parsed JSON-RPC messages.

    Usage is two calls: `feed()` whatever `os.read` returned, then `next_message()`
    until it answers None. `begin_request()` starts a fresh parse budget, which is
    what makes "bytes a server spent on one request" a bounded quantity.
    """

    def __init__(self):
        """Start empty, with the default per-request parse budget."""
        self.buffer = bytearray()
        self.scan_position = 0
        self.pending_messages: deque[dict] = deque()
        self.bytes_consumed = 0
        self.byte_budget = MAX_REQUEST_TOTAL_BYTES
        self.skipped_large_frames = 0

    def begin_request(self, byte_budget: int) -> None:
        """Charge what follows against a fresh budget, for one request."""
        self.bytes_consumed = 0
        self.byte_budget = byte_budget

    def feed(self, chunk: bytes, awaited_id: int | None = None) -> None:
        """Take one read from stdout and queue every whole frame it completed.

        `awaited_id` is the request currently in flight, and lets an oversized
        frame belonging to some other request be dropped unparsed.
        """
        self.buffer += chunk
        self.consume_buffered_lines(awaited_id)

    def next_message(self) -> dict | None:
        """Pop the next queued message, or None when no whole frame is ready."""
        return self.pending_messages.popleft() if self.pending_messages else None

    def consume_buffered_lines(self, awaited_id: int | None = None) -> None:
        """Split whole lines off the buffer and queue the ones that parse as JSON.

        Scanning restarts where the previous scan stopped. A large frame addressed
        elsewhere is dropped before it is parsed, and only what is parsed is
        charged against the request's byte budget.
        """
        while True:
            newline_index = self.buffer.find(b"\n", self.scan_position)
            if newline_index < 0:
                self.scan_position = len(self.buffer)
                return
            line = bytes(self.buffer[:newline_index]).strip()
            del self.buffer[: newline_index + 1]
            self.scan_position = 0
            if not line:
                continue
            if self.frame_answers_another_request(line, awaited_id):
                self.skipped_large_frames += 1
                logger.debug("dropped a %d byte frame addressed to another request", len(line))
                continue
            self.charge_request_bytes(len(line))
            message = self.parse_frame(line)
            if message is not None:
                self.pending_messages.append(message)

    def parse_frame(self, line: bytes) -> dict | None:
        """Parse one stdout line into a JSON-RPC message, or None when it is noise.

        Decoding is explicit and lossy on purpose. Handing raw bytes to
        `json.loads` let invalid UTF-8 surface as `UnicodeDecodeError`, which is
        a `ValueError` and not a `JSONDecodeError`, so it escaped this method and
        came out as a traceback. Two more hostile frames did the same: JSON
        nested thousands deep (`RecursionError`) and an integer long enough to
        trip Python's string-to-int limit (`ValueError`).

        Raises:
            StudioMcpProtocolError: the frame is JSON-shaped but unreadable.
        """
        text = line.decode("utf-8", errors="replace")
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            # The proxy occasionally prints non-protocol noise on stdout; it is
            # not fatal, so log it and keep reading for real messages.
            logger.debug("skipping non-JSON stdout line: %r", text[:NON_JSON_PREVIEW_CHARS])
            return None
        except (ValueError, RecursionError) as frame_error:
            raise StudioMcpProtocolError(
                code=-1,
                message=(
                    f"the proxy sent a {len(line)} byte frame this build cannot read "
                    f"({type(frame_error).__name__})"
                ),
            ) from frame_error
        if not isinstance(message, dict):
            logger.debug("skipping non-object JSON-RPC frame: %r", text[:NON_JSON_PREVIEW_CHARS])
            return None
        return message

    def frame_answers_another_request(self, line: bytes, awaited_id: int | None) -> bool:
        """True when a large frame positively answers a request that is not ours.

        Only large frames are worth the check, and both ends of the line are
        searched, because servers put `id` at either. The AWAITED id is looked for
        first, and wins wherever it turns up: a large legitimate answer routinely
        quotes some other numeric id inside its payload (a place id, an asset id,
        an instance record) thousands of bytes before its own `id` member in the
        tail. Taking the first id seen as the frame's own dropped exactly those
        answers, and since nothing else was coming, the call then burned the
        whole timeout.

        So a frame is dropped only when some other id is positively identified
        AND the awaited id appears in neither window. Everything else, including
        a frame with no readable id at all, falls through to an ordinary parse:
        the cost of parsing a frame we did not need is one wasted parse, and the
        cost of dropping one we did need is the caller's entire deadline.
        """
        if awaited_id is None or len(line) <= LARGE_FRAME_BYTES:
            return False
        windows = (line[:FRAME_ID_PEEK_BYTES], line[-FRAME_ID_PEEK_BYTES:])
        if any(awaited_id_pattern(awaited_id).search(window) for window in windows):
            return False
        return any(FRAME_ID_PATTERN.search(window) for window in windows)

    def charge_request_bytes(self, consumed: int) -> None:
        """Count bytes parsed for the current request, and stop when the budget is gone.

        The per-frame cap does not bound memory: parsing JSON multiplies size
        many times over, so a server can stay under it and still walk this
        process into a gigabyte with a handful of frames.
        """
        self.bytes_consumed += consumed
        if self.bytes_consumed <= self.byte_budget:
            return
        raise StudioMcpError(
            f"the Studio MCP proxy sent {self.bytes_consumed} bytes answering one "
            f"request (budget {self.byte_budget}); refusing to parse more."
        )

    def holds_an_oversized_line(self) -> bool:
        """True when the buffer is past `MAX_MESSAGE_BYTES` with no line break in it.

        The buffer holds only the tail after the last newline, so this trips on
        one absurd message, never on a busy session. The caller decides what to do
        about it, because killing the proxy is process business.
        """
        return len(self.buffer) > MAX_MESSAGE_BYTES

    def oversized_line_message(self) -> str:
        """The refusal text for a line that never ended, quoting both numbers."""
        return (
            f"the Studio MCP proxy sent {len(self.buffer)} bytes with no line break "
            f"(limit {MAX_MESSAGE_BYTES}); killed it rather than buffer more."
        )

    def reset(self) -> None:
        """Drop every buffered byte and queued message, for a client shutting down."""
        self.buffer = bytearray()
        self.scan_position = 0
        self.pending_messages.clear()
