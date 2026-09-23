"""Frame the proxy's stdout into JSON-RPC messages, under fixed byte budgets.

`client` owns the process, the handshake and the request/response exchange. This
module owns the stretch between "some bytes arrived on stdout" and "here is a
parsed message", which is a job with its own hazards:

- Line buffering, because one read can carry two frames, or half of one. The
  scan resumes where the last one stopped, so a chunk costs a search of its own
  bytes rather than of everything buffered so far.
- Parsing, defensively. A frame that is not JSON is noise and is skipped; a frame
  that is JSON-shaped but unreadable (undecodable bytes, an integer too long to
  convert) is a protocol error rather than a traceback. Three things Python's
  parser accepts and this one will not: `NaN` and `Infinity`, which are not JSON
  and which `--json` would re-emit for a strict consumer to choke on; a literal
  like `1e400` that overflows to infinity; and nesting past
  `MAX_FRAME_CONTAINER_DEPTH`, because depth is free to send and expensive to
  hold or to print. The scan that decides that runs before the parse and has a
  window of its own (`MAX_DEPTH_SCAN_BYTES`), because it is the one pass here
  that reads a frame one byte at a time. Length INSIDE a number is not a hazard
  of its own and needs
  no bound: a literal parses in time linear in its digits, measured here at 4 M
  of them in about 7 ms, whether it reaches `parse_float` or the integer
  conversion limit that refuses it.
- Byte budgets. A cap on one frame is not a cap on memory, because parsing JSON
  multiplies size many times over, so a request carries a total parse budget as
  well, and a line that never terminates is refused outright.
- The id peek. Past `LARGE_FRAME_BYTES` a frame is worth identifying before it is
  parsed: one that positively answers a DIFFERENT request is dropped for the cost
  of a regex over both ends of the line instead of a full parse.
- Which frame is ours. The peek's rule (`awaited_id_pattern`) and the parsed
  frame's rule (`response_matches_request`) have to agree about what answers a
  request, so they live side by side: when they disagreed about a string-shaped
  id, the peek kept the frame and the matcher refused it, and the caller waited
  out a deadline against a server that had already answered.

Nothing here touches the process or the file descriptors, so a framing bug can be
reproduced by handing `feed()` some bytes.
"""

import json
import logging
import math
import re
from collections import deque
from functools import lru_cache

from roblox_studio_cli.errors import (
    UNKNOWN_ERROR_CODE,
    StudioMcpError,
    StudioMcpProtocolError,
)

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
# How deep a frame may nest arrays and objects. Depth costs a server nothing to
# send and costs us twice: parsing builds a Python object per level, and
# `json.dumps(indent=2)` used to print two spaces per level per line, so 40 KB
# nested 20,000 deep was 800 MB of `--json`. Real MCP payloads are a handful of
# levels; 512 is far past anything Studio sends and far short of the parser's
# own recursion limit.
MAX_FRAME_CONTAINER_DEPTH = 512
OPENING_CONTAINER_BYTES = b"[{"
CLOSING_CONTAINER_BYTES = b"]}"
STRING_QUOTE = b'"'
STRING_QUOTE_BYTE = ord('"')
# The only bytes that can change a frame's nesting or say where a string starts
# and ends. Everything else is deleted before the walk, so what the walk reads
# is the frame's structure rather than its length.
STRUCTURAL_BYTES = STRING_QUOTE + OPENING_CONTAINER_BYTES + CLOSING_CONTAINER_BYTES
NON_STRUCTURAL_BYTES = bytes(value for value in range(256) if value not in STRUCTURAL_BYTES)
# The two escape pairs that decide whether the quote after them closes a string.
# Both are replaced by bytes of the SAME LENGTH that mean nothing to the scan,
# so the walk needs no escape state and a string can be skipped in one `find`.
ESCAPED_BACKSLASH = b"\\\\"
ESCAPED_QUOTE = b'\\"'
NEUTRALISED_ESCAPE = b"xx"
# How much of a frame's STRUCTURE the interpreted walk will read before refusing
# the frame outright. Everything else about the scan runs in C: the opener count
# that clears an ordinary frame, the compaction below, and the `find` that skips
# a string literal whole. What is left is the walk, and it runs inside
# `parse_frame` where `--timeout` cannot reach, so it needs a window of its own:
# measured here, 8 MiB of "[]" pairs is 0.21 s of walking, and a frame of `{}`
# the same. A megabyte of structure is past anything real by orders of
# magnitude (a 46-tool tools/list carries 241 openers in 10 KB, and a Luau
# answer of 120,000 JSON records carries its brackets INSIDE a string, where
# they cost one skip), so a frame that spends it is megabytes of brackets.
MAX_DEPTH_SCAN_BYTES = 2**20
# What an empty line costs the request's budget. Nothing is parsed or queued for
# one, but each still costs a find, a slice and a delete, and charged nothing at
# all a flood of them was the one hazard no bound applied to.
EMPTY_FRAME_BUDGET_BYTES = 1
# ASCII whitespace, which is what a frame is trimmed of before anything reads
# it, and how far the trim will walk by index before handing the job back to C.
# A line ending is a byte or two; anything padded past this window is a server
# making a point, and 8 MB of spaces walks in 234 ms against 2.3 ms in `strip`.
FRAME_WHITESPACE_BYTES = frozenset(b" \t\n\r\x0b\x0c")
MAX_TRIMMED_FRAME_PADDING_BYTES = 64
# One pattern per in-flight request id, and ids only ever count upward.
AWAITED_ID_PATTERN_CACHE_SIZE = 32


def strip_frame_bytes(frame: memoryview) -> bytes:
    """One copy of `frame`, trimmed of the whitespace around it.

    `bytes(frame).strip()` is the obvious spelling, and it is one copy only
    while there is nothing to strip: CPython hands `strip()` back the same
    object then. A carriage return before the newline costs a server one byte
    and makes it two copies for every frame, which is the 3x peak allocation
    the memoryview was introduced to remove (measured on a 192 KB frame: 3.1x
    against 2.1x). Trimming the view first and copying once holds the claim
    whatever the line ends with.

    The walk is interpreted, so it is bounded. Past
    `MAX_TRIMMED_FRAME_PADDING_BYTES` the frame goes back to `strip()`, because
    a whitespace-only frame is charged one byte of budget exactly as a bare
    newline is: a flood of them is bounded by nothing but the pipe, and must
    stay linear with a small constant rather than 100x one.

    The caller must not bind the returned view's slice to a name of its own: an
    export of the bytearray still alive at `del self.buffer[...]` makes that
    resize raise BufferError.
    """
    limit = MAX_TRIMMED_FRAME_PADDING_BYTES
    start = 0
    end = len(frame)
    while start < end and start < limit and frame[start] in FRAME_WHITESPACE_BYTES:
        start += 1
    while end > start and len(frame) - end < limit and frame[end - 1] in FRAME_WHITESPACE_BYTES:
        end -= 1
    if start == limit or len(frame) - end == limit:
        return bytes(frame).strip()
    return bytes(frame[start:end])


def refuse_json_constant(literal: str) -> float:
    """Refuse `NaN`, `Infinity` and `-Infinity`, which JSON does not have.

    Python's parser accepts all three as an extension and its writer emits them
    back, so one in a tool result rode through `--json` into output that a
    strict parser refuses. The literal is one of those three words, so quoting
    it carries nothing the server chose.
    """
    raise ValueError(f"a JSON-RPC frame carried {literal}, which is not JSON")


def refuse_non_finite_number(literal: str) -> float:
    """Parse a JSON number, refusing one that is only finite on paper.

    `1e400` is legal JSON syntax and `float()` answers `inf`, which `--json`
    then writes as `Infinity`: the same unparseable output as above, reached
    without using a word JSON lacks.
    """
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError("a JSON-RPC frame carried a number that is not finite")
    return value


def compact_frame_structure(line: bytes) -> bytes:
    """`line` reduced to the bytes that can change its nesting, in their original order.

    Three C-level passes and no interpretation:

    - `\\\\` becomes two inert bytes, spending an escaped backslash exactly as
      JSON's own reader spends it, so every backslash still standing introduces
      the escape after it.
    - `\\"` becomes two inert bytes, so every quote still standing opens or
      closes a string literal. Order matters: run this first and `"a\\\\"` reads
      as an unterminated string.
    - Everything that is not a quote or a bracket is deleted. Order is
      preserved, which is all the depth walk reads, and a `\\u0022` or `\\u005B`
      goes with the rest: the parser reads those as characters inside a string,
      and so, by deleting them, does this.

    What comes back is usually a few hundred bytes for a frame of megabytes,
    which is the point: the walk then reads structure instead of length.
    Replacement is length-preserving only so the two passes cannot disturb each
    other; the offsets are not used afterwards. CPython hands back the same
    object when a replacement matches nothing, so a frame with no escapes in it
    is copied once rather than three times.
    """
    neutralised = line.replace(ESCAPED_BACKSLASH, NEUTRALISED_ESCAPE)
    neutralised = neutralised.replace(ESCAPED_QUOTE, NEUTRALISED_ESCAPE)
    return neutralised.translate(None, NON_STRUCTURAL_BYTES)


def exceeds_container_depth(line: bytes) -> bool:
    """True when a frame nests arrays and objects deeper than we will parse.

    Two passes, and the first answers every ordinary frame. Depth can never
    exceed the number of openers, so counting them is a sound way to say
    "definitely not too deep" without looking at order at all: that is one
    C-level pass (4 ms on 8 MB) and it clears a 700 KB base64 capture.

    Past that, the frame is compacted to its structure and walked once, tracking
    two things: whether we are inside a string literal, because a brace in Luau
    source or a minified table is text and not structure, and the depth itself,
    which ends the walk the moment it passes the cap. A closer with nothing open
    leaves the depth at zero rather than taking it negative, so unbalanced tails
    cannot bank credit against nesting that comes later. A string is skipped in
    one `find`, so a tool result carrying a hundred thousand brackets costs the
    walk one step, and an unterminated string swallows whatever follows it here
    exactly as it does for the parser, which refuses the frame a moment later.

    Linear is not enough on its own, because this runs inside `parse_frame`,
    where nothing preempts it: a frame of `[]` pairs never nests past 1 and
    never ends early, so 8 MiB of them walked for 0.21 s here and the per-request
    budget bought two of those. So the walk has a window as well as a rule, and
    a frame with more structure in it than `MAX_DEPTH_SCAN_BYTES` is refused
    rather than read, the way `strip_frame_bytes` hands a frame padded past its
    own window back to C.

    Reading content at C speed is the point, and so is never handing a frame to
    a regex. This walked the raw bytes tracking quote and escape state, which is
    correct and charges tool output exactly what it charges structure; before
    that it blanked string literals with `"[^"\\]*(?:\\.[^"\\]*)*"`, which a
    string that opens, pads itself with escaped quotes and never closes
    backtracks quadratically: 128 KB of it took 42 s, and `tools --timeout 3`
    ran for 85 seconds.

    Raises:
        StudioMcpProtocolError: the frame carries more structure than the window,
            which is megabytes of brackets and nothing a real bridge sends.
    """
    if line.count(b"[") + line.count(b"{") <= MAX_FRAME_CONTAINER_DEPTH:
        return False
    structure = compact_frame_structure(line)
    depth = 0
    position = 0
    examined = 0
    length = len(structure)
    while position < length:
        byte = structure[position]
        position += 1
        examined += 1
        if examined > MAX_DEPTH_SCAN_BYTES:
            raise StudioMcpProtocolError(
                code=UNKNOWN_ERROR_CODE,
                message=(
                    f"the proxy sent a {len(line)} byte frame carrying more than "
                    f"{MAX_DEPTH_SCAN_BYTES} bytes of JSON structure; refusing to scan more of it"
                ),
            )
        if byte == STRING_QUOTE_BYTE:
            closing_quote = structure.find(STRING_QUOTE, position)
            position = length if closing_quote < 0 else closing_quote + 1
        elif byte in OPENING_CONTAINER_BYTES:
            depth += 1
            if depth > MAX_FRAME_CONTAINER_DEPTH:
                return True
        elif depth:
            # Nothing but quotes and brackets survives the compaction, so this
            # is a closer. Zero is the floor: 600 closers ahead of 513 openers
            # read as depth -87 once, which is the frame the cap exists for.
            depth -= 1
    return False


@lru_cache(maxsize=AWAITED_ID_PATTERN_CACHE_SIZE)
def awaited_id_pattern(awaited_id: int) -> re.Pattern:
    """Match one specific id the way a JSON-RPC frame writes it, bare or quoted.

    A server echoes a numeric request id as `"id": 7` or as `"id": "7"`, and the
    peek has to recognise both, because mistaking our own answer for somebody
    else's costs the caller the entire timeout. The bare form ends on a
    non-digit, so a wait for 7 is not satisfied by 71.
    """
    return re.compile(rb'"id"\s*:\s*(?:%d(?![0-9])|"%d")' % (awaited_id, awaited_id))


def response_matches_request(message: dict, request_id: int) -> bool:
    """True when this parsed message is the answer to `request_id`.

    Three rules, each one a frame that looked like our answer and was not:

    - A frame carrying a string `method` is a REQUEST, never a response. MCP
      servers ask their clients things (`roots/list`, `sampling/createMessage`,
      `elicitation/create`) and number those requests from 1 like the client's
      own counter, so one can wear the id we await. Taken as the answer it has
      no `result`: `tools/list` died with "returned a non-object result
      (NoneType)" while the real answer sat one frame behind it. The test is
      `isinstance(method, str)`, the same one `mcp_payloads.method_not_found_reply`
      applies when it decides to answer: written differently, the two disagreed
      about `"method": null`, which was then neither answered nor declined and
      cost the caller the deadline instead. `parse_frame` refuses that frame
      outright now, so both readings agree by construction.
    - A string id that reads as ours IS ours. JSON-RPC allows it and
      `awaited_id_pattern` above keeps such a frame, so refusing it here with
      `==` against an int burned the whole deadline against a server that had
      already answered.
    - A boolean id is not the integer it equals: `True == 1`, and `"id": true`
      is the answer to nothing.
    """
    if isinstance(message.get("method"), str):
        return False
    answered = message.get("id")
    if isinstance(answered, bool):
        return False
    if isinstance(answered, int):
        return answered == request_id
    if isinstance(answered, str):
        return answered == str(request_id)
    return False


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
        elsewhere is dropped before it is parsed; everything else is charged
        against the request's byte budget, an empty line included, so a flood of
        bare newlines ends the same way a flood of frames does.
        """
        while True:
            newline_index = self.buffer.find(b"\n", self.scan_position)
            if newline_index < 0:
                self.scan_position = len(self.buffer)
                return
            # One copy of the frame, not two. Slicing the bytearray builds a
            # bytearray copy that `bytes()` then copies again, and a frame the
            # id peek is about to drop unparsed paid for both: 3.1x its own
            # length in peak allocation, against 2.1x through a memoryview.
            # The trim runs on the view for the same reason, since `strip()`
            # after the copy is a second copy of every frame that ends in CRLF.
            with memoryview(self.buffer) as buffered:
                line = strip_frame_bytes(buffered[:newline_index])
            del self.buffer[: newline_index + 1]
            self.scan_position = 0
            if not line:
                self.charge_request_bytes(EMPTY_FRAME_BUDGET_BYTES)
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

        The depth check runs BEFORE the parse, because refusing a 20,000-deep
        frame is the point of it: parsed, it is an object per level and 800 MB
        of `--json`. The two number hooks refuse what Python accepts and JSON
        does not, so `NaN`, `Infinity` and `1e400` are protocol errors here
        rather than output no strict parser will read.

        Raises:
            StudioMcpProtocolError: the frame is JSON-shaped but unreadable.
        """
        if exceeds_container_depth(line):
            raise StudioMcpProtocolError(
                code=UNKNOWN_ERROR_CODE,
                message=(
                    f"the proxy sent a {len(line)} byte frame nested past "
                    f"{MAX_FRAME_CONTAINER_DEPTH} containers; refusing to parse it"
                ),
            )
        text = line.decode("utf-8", errors="replace")
        try:
            message = json.loads(
                text,
                parse_constant=refuse_json_constant,
                parse_float=refuse_non_finite_number,
            )
        except json.JSONDecodeError:
            # The proxy occasionally prints non-protocol noise on stdout; it is
            # not fatal, so log it and keep reading for real messages.
            logger.debug("skipping non-JSON stdout line: %r", text[:NON_JSON_PREVIEW_CHARS])
            return None
        except (ValueError, RecursionError) as frame_error:
            raise StudioMcpProtocolError(
                code=UNKNOWN_ERROR_CODE,
                message=(
                    f"the proxy sent a {len(line)} byte frame this build cannot read "
                    f"({type(frame_error).__name__})"
                ),
            ) from frame_error
        if not isinstance(message, dict):
            logger.debug("skipping non-object JSON-RPC frame: %r", text[:NON_JSON_PREVIEW_CHARS])
            return None
        if "method" in message and not isinstance(message["method"], str):
            # `method` is what tells a request from a response, and JSON-RPC
            # says it is a string. A frame with `"method": null` was read as
            # neither: not our answer (it has a method) and not a question
            # worth declining (the method is not a name), so it silently cost
            # the caller their deadline.
            raise StudioMcpProtocolError(
                code=UNKNOWN_ERROR_CODE,
                message=(
                    "the proxy sent a frame whose `method` is a "
                    f"{type(message['method']).__name__}, which is neither a request nor a response"
                ),
            )
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
