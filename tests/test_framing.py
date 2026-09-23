"""Unit tests for the stdout frame reader, with no subprocess anywhere in sight.

Framing used to live inside the client, where every one of these behaviours cost
a spawned fake server to reach. They are pure functions over bytes: hand `feed()`
a chunk, take messages back, and assert what was parsed, what was skipped, and
what was charged against the budget.

The hostile cases in here each ended in a traceback or a hang before they were
handled: undecodable bytes, JSON nested past the parser's own limits, an integer
too long to convert, and large frames answering a request nobody sent.
"""

import json
import signal
import time
import tracemalloc
from contextlib import contextmanager

import pytest

from roblox_studio_cli import framing as framing_module
from roblox_studio_cli.errors import StudioMcpError, StudioMcpProtocolError
from roblox_studio_cli.framing import (
    LARGE_FRAME_BYTES,
    MAX_FRAME_CONTAINER_DEPTH,
    MAX_MESSAGE_BYTES,
    MAX_TOOLS_LIST_TOTAL_BYTES,
    StdoutFrameReader,
    exceeds_container_depth,
    response_matches_request,
)
from roblox_studio_cli.mcp_payloads import method_not_found_reply

AWAITED_ID = 7
OTHER_ID = 999_999
PADDING_BYTES = LARGE_FRAME_BYTES * 3
# What the client hands `feed()` per read, mirrored here because a frame arrives
# in pipe-sized pieces and the buffer's growth is part of what a frame costs.
STDOUT_CHUNK_BYTES = 65536
# Peak allocation a frame may cost, as a multiple of its own length. The floor
# is 2x: the buffer holds it, and the line is taken out of the buffer. Measured
# from 128 KB to 8 MB: 3.1x when the bytearray was sliced before `bytes()`
# copied it again, 2.1x through a memoryview.
MAX_PEAK_BYTES_PER_FRAME = 2.5
# What the depth scan may spend on a frame built to make it spend. Both are
# orders of magnitude over the real cost (about 1 ms and 40 ms here), because
# the failure they guard against is measured in minutes and CI is shared.
HOSTILE_FRAME_SECONDS = 1.0
LARGE_HOSTILE_FRAME_SECONDS = 2.0
# How much cheaper the BOUNDED trim has to be than the walk it replaced. An
# absolute budget cannot see that regression at all: with the bound reverted,
# 4 MB of padding still walks in 0.098 s against the 1.0 s above, so the test
# passed on the code it exists to fail. Measured here: 48x to 77x.
MIN_TRIM_SPEEDUP = 10


def drain(reader: StdoutFrameReader) -> list[dict]:
    """Every message the reader has queued, in order."""
    messages = []
    while True:
        message = reader.next_message()
        if message is None:
            return messages
        messages.append(message)


def large_frame(frame: dict) -> bytes:
    """`frame` padded past the peek threshold, as one newline-terminated line."""
    padded = dict(frame)
    padded["result"] = {"padding": "x" * PADDING_BYTES, **padded.get("result", {})}
    return json.dumps(padded).encode("utf-8") + b"\n"


def frame_quoting_another_id_first(own_id) -> bytes:
    """A large answer that mentions someone else's id in its payload, then carries its own.

    The shape every bulk Studio answer has: records with their own numeric `id`
    fields at the head (place ids, asset ids, instance records), the payload in
    the middle, and the JSON-RPC `id` last, because that is where a server that
    streams its result writes it.
    """
    body = {
        "jsonrpc": "2.0",
        "result": {"records": [{"id": OTHER_ID}], "padding": "x" * PADDING_BYTES},
        "id": own_id,
    }
    return json.dumps(body).encode("utf-8") + b"\n"


def unbounded_trim_seconds(frame: bytes) -> float:
    """Wall time for the trim `strip_frame_bytes` would do with its bound removed.

    The same per-byte loop over the same bytes, through a memoryview, which is
    what the bounded version walks. It is measured in the test rather than
    written down as a number, because a wall-clock constant is a fact about one
    machine: the assertion has to mean the same thing on a loaded CI runner,
    where both sides slow down together and only the ratio between them holds.
    """
    started = time.monotonic()
    with memoryview(frame) as view:
        index = 0
        end = len(view)
        while index < end and view[index] in framing_module.FRAME_WHITESPACE_BYTES:
            index += 1
    return time.monotonic() - started


@contextmanager
def wall_clock_budget(seconds: float, description: str):
    """Fail the block inside at `seconds`, whatever it is spending them on.

    Timing a call and asserting afterwards cannot fail a regression that does
    not return, and not returning is the exact shape of the one below: the
    regex this scan replaced is quadratic, so 4 MB of the hostile frame runs for
    hours. A test that hangs is worse than one that fails, because it holds a CI
    runner to the runner's own limit instead of printing a red line.

    A daemon thread and `join(budget)` do not bound it either, measured on the
    regex this replaced: `join(1.0)` came back after 37.65 s, because `re` holds
    the GIL for the whole of one `sub()` call and nothing can preempt it. SIGALRM
    can: the regex engine checks for signals as it runs, so the alarm turns the
    hang into a failure after 1.08 s. Main thread only, which is where pytest
    runs a test.

    A process has one real-time timer, and this takes it: adding pytest-timeout
    in its `signal` method (SIGALRM, armed per test) would have its timer
    cancelled by the `setitimer(0)` below and its handler put back without one,
    so that test would run unbounded and say nothing about it. The suite has no
    such plugin today, and the pinned test extra is where it would arrive.
    """
    disarmed = False

    def ring(signal_number, frame):
        # The timer is one-shot, and the moment between the block's last
        # statement and the cancellation below belongs to nobody: an alarm
        # landing there would fail a block that finished inside its budget.
        if disarmed:
            return
        raise TimeoutError(f"{description} spent more than {seconds:.1f}s")

    previous_handler = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        disarmed = True
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def unterminated_string_frame(total_bytes: int) -> bytes:
    """Open a string, pad it with escaped quotes, never close it, then over-open.

    The tail is one container past the cap, so the cheap opener count cannot
    clear the frame and the scan has to walk it. Everything before that tail
    sits inside a string with no end, which is the shape the old blanking regex
    backtracked over: one candidate start, an escaped quote at every position
    after it, and no closing quote to ever stop the search.
    """
    openers = b"{" * (MAX_FRAME_CONTAINER_DEPTH + 1)
    escaped_quotes = (total_bytes - len(openers) - 1) // 2
    return b'"' + rb"\"" * escaped_quotes + openers


@pytest.mark.parametrize(
    ("frame_bytes", "budget_seconds"),
    [
        (128 * 1024, HOSTILE_FRAME_SECONDS),
        (4 * 2**20, LARGE_HOSTILE_FRAME_SECONDS),
    ],
)
def test_an_unterminated_string_costs_time_linear_in_its_length(frame_bytes, budget_seconds):
    """The depth check runs where no timeout can reach it, so it has to be linear.

    Measured against the regex this replaced, on exactly this frame: 2.9 s at
    32 KB, 42 s at the 128 KB below, quadratic from there, and an 8 MB frame
    outliving the caller. `parse_frame` runs inside the read loop, so none of
    that was a slow command: `tools --timeout 3` ran for 85 seconds.
    """
    line = unterminated_string_frame(frame_bytes)

    started = time.perf_counter()
    with wall_clock_budget(budget_seconds, f"the depth scan of {len(line)} bytes"):
        verdict = exceeds_container_depth(line)
    elapsed = time.perf_counter() - started

    assert elapsed < budget_seconds, f"{len(line)} bytes took {elapsed:.1f}s"
    assert verdict is False, "the openers are inside the string, so this frame nests nothing"


def peak_bytes_feeding(frame: bytes) -> int:
    """Peak traced allocation while one frame arrives in pipe-sized reads."""
    reader = StdoutFrameReader()
    reader.begin_request(MAX_TOOLS_LIST_TOTAL_BYTES)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        for start in range(0, len(frame), STDOUT_CHUNK_BYTES):
            reader.feed(frame[start : start + STDOUT_CHUNK_BYTES], awaited_id=AWAITED_ID)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert reader.skipped_large_frames == 1, "this frame is meant to be dropped unparsed"
    return peak


def test_a_frame_dropped_unparsed_is_copied_once_rather_than_twice():
    """The peek saves the parse; it should not hand back the saving in copies.

    A frame still has to come off the buffer before the peek can read it, and
    slicing a bytearray builds a bytearray that `bytes()` then copies again. So
    a frame nobody asked for cost three copies of itself, and a flood of 8 MB
    ones was the shape that showed it: 24.5 MB of peak allocation per frame,
    against 16.5 MB through a memoryview.
    """
    frame = large_frame({"jsonrpc": "2.0", "id": OTHER_ID})

    peak = peak_bytes_feeding(frame)

    assert peak < len(frame) * MAX_PEAK_BYTES_PER_FRAME, (
        f"{len(frame)} bytes peaked at {peak} ({peak / len(frame):.1f}x)"
    )


def test_the_budget_helper_does_not_fail_a_block_that_already_finished():
    """The alarm and the cancellation race, and the alarm is allowed to win.

    Delivery cannot be scheduled into that window from a test, so this asks the
    handler what it would do if it were: armed, it raises into whatever is
    running when the signal lands, which by then is the teardown of a block that
    came in under its budget. Disarmed, it returns and the one-shot timer that
    nobody cancelled in time costs nothing.
    """
    with wall_clock_budget(HOSTILE_FRAME_SECONDS, "a block that finishes"):
        armed_handler = signal.getsignal(signal.SIGALRM)

    assert armed_handler(signal.SIGALRM, None) is None, "a late alarm failed a finished block"


def test_a_frame_that_ends_in_crlf_is_copied_once_as_well():
    """`bytes(line).strip()` is one copy only while there is nothing to strip.

    CPython hands `strip()` back the same object when it removes nothing, which
    is why the frame above measures at 2.1x. A carriage return before the
    newline is all it takes to make that false, it costs the sender one byte,
    and then every frame pays the copy the memoryview was introduced to save:
    measured on this frame, 3.1x against 2.1x. Trimming the view before the
    copy holds the claim whatever the line ends with.
    """
    frame = large_frame({"jsonrpc": "2.0", "id": OTHER_ID}).replace(b"\n", b"\r\n")

    peak = peak_bytes_feeding(frame)

    assert peak < len(frame) * MAX_PEAK_BYTES_PER_FRAME, (
        f"{len(frame)} bytes peaked at {peak} ({peak / len(frame):.1f}x)"
    )


def test_a_frame_padded_with_whitespace_stays_as_cheap_as_a_newline_flood():
    """Trimming by index is interpreted, so a frame of nothing but padding is not.

    A whitespace-only frame is charged one byte of budget, exactly as a bare
    newline is, so a flood of them is bounded by nothing but the pipe and has
    to stay linear with a small constant. Walking 8 MB of spaces one byte at a
    time takes 234 ms against 2.3 ms in C, which is why the walk stops early
    and hands a frame padded past the window back to `strip()`.

    The assertion is a RATIO against that unbounded walk, measured alongside it
    on the same bytes, because the absolute budget this used to carry does not
    discriminate: revert the bound and 4 MB of padding still walks in 0.098 s,
    well inside a 1.0 s budget, so the test passed on exactly the code it was
    written to fail. The budget stays as well, to catch a bounded path that
    went slow in some way the ratio cannot see.
    """
    frame = b" " * (MAX_MESSAGE_BYTES // 2) + b"\n"
    reader = StdoutFrameReader()
    reader.begin_request(MAX_TOOLS_LIST_TOTAL_BYTES)
    unbounded = unbounded_trim_seconds(frame)

    started = time.monotonic()
    reader.feed(frame, awaited_id=AWAITED_ID)
    elapsed = time.monotonic() - started

    assert drain(reader) == [], "whitespace is not a frame"
    assert reader.bytes_consumed == framing_module.EMPTY_FRAME_BUDGET_BYTES
    assert elapsed < HOSTILE_FRAME_SECONDS, f"{len(frame)} bytes of padding took {elapsed:.2f}s"
    assert elapsed * MIN_TRIM_SPEEDUP < unbounded, (
        f"{len(frame)} bytes of padding took {elapsed * 1000:.0f} ms, against "
        f"{unbounded * 1000:.0f} ms walked unbounded: the bound is not being hit"
    )


def test_an_unterminated_string_is_noise_rather_than_a_depth_error():
    """It is not valid JSON, so it is skipped the way any other garbage line is."""
    assert StdoutFrameReader().parse_frame(unterminated_string_frame(4096)) is None


def test_the_cap_is_the_last_depth_accepted_and_openers_in_a_string_do_not_count():
    """512 containers parse and 513 do not, whatever the string literals hold.

    Both frames carry the same opener bytes, so the cheap count cannot tell them
    apart and the walk has to: what differs is which side of the quotes they are
    on, and that the walk leaves the string it entered.
    """
    decoys = b'"' + b"{" * MAX_FRAME_CONTAINER_DEPTH + b'"'

    assert exceeds_container_depth(decoys + b"[" * MAX_FRAME_CONTAINER_DEPTH) is False
    assert exceeds_container_depth(decoys + b"[" * (MAX_FRAME_CONTAINER_DEPTH + 1)) is True


def test_closers_with_nothing_open_do_not_bank_credit_against_later_nesting():
    """Depth stops at zero on the way down, or a tail of closers buys nesting.

    Allowed to go negative, 600 closers ahead of 513 openers read as depth -87:
    exactly the frame the cap exists for, waved through.
    """
    assert exceeds_container_depth(b"}" * 600 + b"{" * (MAX_FRAME_CONTAINER_DEPTH + 1)) is True


def test_a_run_of_backslashes_decides_whether_the_quote_after_it_closes_the_string():
    """Four escaped backslashes end with the string still open; a fifth escapes the quote.

    Tracking "the previous byte was a backslash" is what gets a run right.
    Asking whether a backslash merely appears before the quote reads the first
    frame as still inside its string and lets 513 real containers through.
    """
    run = b"\\\\" * 4
    closed = b'"' + run + b'"' + b"{" * (MAX_FRAME_CONTAINER_DEPTH + 1)
    still_open = b'"' + run + b'\\"' + b"{" * (MAX_FRAME_CONTAINER_DEPTH + 1)

    assert exceeds_container_depth(closed) is True
    assert exceeds_container_depth(still_open) is False


def test_two_frames_in_one_chunk_both_arrive():
    """A single read carrying two messages must not strand the second one."""
    reader = StdoutFrameReader()
    reader.feed(b'{"id": 1}\n{"id": 2}\n')
    assert drain(reader) == [{"id": 1}, {"id": 2}]


def test_a_frame_split_across_two_chunks_is_reassembled():
    reader = StdoutFrameReader()
    reader.feed(b'{"id": 1, "resu')
    assert reader.next_message() is None
    reader.feed(b'lt": {"ok": true}}\n')
    assert drain(reader) == [{"id": 1, "result": {"ok": True}}]


def test_blank_lines_and_non_object_frames_are_skipped():
    reader = StdoutFrameReader()
    reader.feed(b'\n\n[1, 2]\nnot json at all\n{"id": 1}\n')
    assert drain(reader) == [{"id": 1}]


def test_a_frame_with_no_readable_json_is_skipped_not_fatal():
    reader = StdoutFrameReader()
    assert reader.parse_frame(b"[1, 2]") is None
    assert reader.parse_frame(b"not json at all") is None
    assert reader.parse_frame(b'{"id": 1}') == {"id": 1}


def test_an_undecodable_frame_is_skipped_rather_than_raising_a_traceback():
    """Invalid UTF-8 reached json.loads as bytes and came back as a UnicodeDecodeError."""
    reader = StdoutFrameReader()
    reader.feed(b'{"jsonrpc": "2.0", "note": "\x80\xfe\x81"}\n{"id": 1}\n')
    assert {"id": 1} in drain(reader)


def test_a_frame_that_exhausts_the_parser_is_a_protocol_error(monkeypatch):
    """Deeply nested JSON raises RecursionError on 3.10 to 3.13, where json still recurses."""

    def exhausted(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(framing_module.json, "loads", exhausted)
    with pytest.raises(StudioMcpProtocolError, match="cannot read"):
        StdoutFrameReader().parse_frame(b"[[[[1]]]]")


def test_an_integer_too_long_to_convert_is_a_protocol_error():
    """Python refuses int() past 4300 digits; the frame must not take the process down."""
    with pytest.raises(StudioMcpProtocolError, match="cannot read"):
        StdoutFrameReader().parse_frame(b'{"id": ' + b"9" * 5000 + b"}")


def test_a_request_cannot_parse_more_than_its_byte_budget():
    reader = StdoutFrameReader()
    reader.begin_request(byte_budget=100)
    with pytest.raises(StudioMcpError, match="budget"):
        reader.feed(b'{"id": 1, "pad": "' + b"x" * 200 + b'"}\n')


def test_a_flood_of_bare_newlines_ends_at_the_budget_like_everything_else():
    """Nothing is queued for an empty line, so the budget is the only thing that can end it.

    Each one still costs a find, a slice and a delete, and they were charged
    nothing at all, so the one bound the documented limits name did not apply.
    """
    reader = StdoutFrameReader()
    reader.begin_request(byte_budget=100)
    with pytest.raises(StudioMcpError, match="budget"):
        reader.feed(b"\n" * 200)


def test_a_handful_of_blank_lines_costs_almost_nothing():
    """The charge is per newline, so ordinary trailing whitespace stays free enough."""
    reader = StdoutFrameReader()
    reader.begin_request(byte_budget=100)
    reader.feed(b"\n\n\n")
    assert reader.bytes_consumed == 3


def test_each_request_starts_its_budget_over():
    reader = StdoutFrameReader()
    reader.begin_request(byte_budget=100)
    reader.feed(b'{"id": 1}\n')
    consumed = reader.bytes_consumed
    assert consumed > 0
    reader.begin_request(byte_budget=100)
    assert reader.bytes_consumed == 0


def test_an_unterminated_line_past_the_cap_is_reported_not_buffered():
    """The guard that kept a runaway proxy from turning 64 MB into 2.5 GB of RSS."""
    reader = StdoutFrameReader()
    assert reader.holds_an_oversized_line() is False
    reader.buffer = bytearray(b"x" * (MAX_MESSAGE_BYTES + 1))
    assert reader.holds_an_oversized_line() is True
    assert "no line break" in reader.oversized_line_message()


def test_the_per_frame_cap_is_small_enough_to_bound_memory():
    """A viewport capture is a few hundred KB; 64 MB per frame bounded nothing."""
    assert MAX_MESSAGE_BYTES == 8 * 2**20
    assert MAX_TOOLS_LIST_TOTAL_BYTES < MAX_MESSAGE_BYTES


def test_a_large_frame_answering_another_request_is_dropped_unparsed():
    reader = StdoutFrameReader()
    reader.feed(large_frame({"jsonrpc": "2.0", "id": OTHER_ID}), awaited_id=AWAITED_ID)
    assert drain(reader) == []
    assert reader.skipped_large_frames == 1
    assert reader.bytes_consumed == 0, "a dropped frame was still charged for"


def test_a_small_frame_for_another_request_is_parsed_and_left_to_the_caller():
    """The peek is for large frames only; notifications and stray small ids go the old way."""
    reader = StdoutFrameReader()
    reader.feed(b'{"jsonrpc": "2.0", "id": 999999}\n', awaited_id=AWAITED_ID)
    assert drain(reader) == [{"jsonrpc": "2.0", "id": OTHER_ID}]
    assert reader.skipped_large_frames == 0


def test_a_large_frame_with_no_readable_id_is_parsed_rather_than_guessed_at():
    reader = StdoutFrameReader()
    reader.feed(large_frame({"jsonrpc": "2.0", "method": "notifications/message"}),
                awaited_id=AWAITED_ID)
    assert len(drain(reader)) == 1
    assert reader.skipped_large_frames == 0


def test_nothing_is_dropped_when_no_request_is_in_flight():
    reader = StdoutFrameReader()
    reader.feed(large_frame({"jsonrpc": "2.0", "id": OTHER_ID}), awaited_id=None)
    assert len(drain(reader)) == 1


def test_a_large_answer_quoting_another_id_in_its_payload_is_still_ours():
    """The decoy case: `"id": 999999` in the first window, our own id in the last.

    Dropping this frame cost the caller the entire timeout (measured at 6.09 s
    against a 64 KB+ answer), because nothing else was ever going to arrive.
    """
    reader = StdoutFrameReader()
    reader.feed(frame_quoting_another_id_first(AWAITED_ID), awaited_id=AWAITED_ID)
    assert [message["id"] for message in drain(reader)] == [AWAITED_ID]
    assert reader.skipped_large_frames == 0, "a frame carrying the awaited id was dropped"


def test_a_quoted_string_id_counts_as_the_awaited_id():
    """Some servers echo a numeric request id as a string; that is still our answer."""
    reader = StdoutFrameReader()
    reader.feed(frame_quoting_another_id_first(str(AWAITED_ID)), awaited_id=AWAITED_ID)
    assert [message["id"] for message in drain(reader)] == [str(AWAITED_ID)]
    assert reader.skipped_large_frames == 0


def test_a_neighbouring_id_does_not_pass_for_the_awaited_one():
    """`"id": 71` must not satisfy a wait for id 7, in either direction."""
    reader = StdoutFrameReader()
    reader.feed(frame_quoting_another_id_first(AWAITED_ID * 10 + 1), awaited_id=AWAITED_ID)
    assert drain(reader) == []
    assert reader.skipped_large_frames == 1


@pytest.mark.parametrize(
    "answered, matches, reason",
    [
        (AWAITED_ID, True, "our own id, exactly as we sent it"),
        (str(AWAITED_ID), True, "the same id, echoed as a string"),
        (AWAITED_ID * 10 + 1, False, "a neighbouring id is somebody else's"),
        (str(AWAITED_ID * 10 + 1), False, "the same, as a string"),
        (None, False, "a notification carries no id"),
        ("", False, "an empty id answers nothing"),
    ],
)
def test_a_string_shaped_id_that_equals_ours_is_ours(answered, matches, reason):
    """The large-frame peek accepts `"id": "7"` as ours, so the matcher has to agree.

    Disagreeing cost the whole timeout: the peek kept the frame, `==` against an
    int refused it, and nothing else was ever going to arrive.
    """
    assert response_matches_request({"id": answered}, AWAITED_ID) is matches, reason


def test_a_boolean_id_is_not_the_integer_it_equals():
    """`True == 1` in Python, and a frame answering `"id": true` is not answering id 1."""
    assert response_matches_request({"id": True}, 1) is False


@pytest.mark.parametrize("answered", [AWAITED_ID, str(AWAITED_ID)])
def test_a_frame_carrying_a_method_is_a_question_not_our_answer(answered):
    """A JSON-RPC response never carries `method`, whatever id it wears."""
    question = {"jsonrpc": "2.0", "id": answered, "method": "roots/list"}
    assert response_matches_request(question, AWAITED_ID) is False


@pytest.mark.parametrize(
    "frame, reason",
    [
        (b'{"id": 1, "result": {"n": NaN}}', "NaN is Python's extension, not JSON"),
        (b'{"id": 1, "result": {"n": Infinity}}', "and neither is Infinity"),
        (b'{"id": 1, "result": {"n": -Infinity}}', "in either direction"),
        (b'{"id": 1, "result": {"n": 1e400}}', "a literal that overflows to inf is the same bug"),
    ],
)
def test_a_number_json_cannot_hold_is_a_protocol_error(frame, reason):
    """`json.loads` accepts these and `json.dumps` writes them straight back out.

    Which meant `--json` emitted `NaN` and `Infinity` for a consumer whose
    parser refuses both, from a frame this CLI had accepted as valid. Refusing
    them here is what lets `json_output.compact_json` promise strict JSON.
    """
    with pytest.raises(StudioMcpProtocolError, match="cannot read"):
        StdoutFrameReader().parse_frame(frame)


def test_an_ordinary_float_still_parses():
    """The hook refuses non-finite numbers, not numbers."""
    assert StdoutFrameReader().parse_frame(b'{"id": 1, "result": {"n": 1.5e3}}') == {
        "id": 1,
        "result": {"n": 1500.0},
    }


def test_a_frame_nested_past_the_depth_cap_is_refused_unparsed():
    """Depth is free to send, and costs us an object per level and `--json` per line.

    Measured before the cap: a 4 KB frame nested 2,000 deep printed 8 MB of
    `--json`, and the same shape at 20,000 deep printed 800 MB, because
    `indent=2` wrote two spaces per level on every line. Both frames sat far
    under every byte budget here.
    """
    too_deep = b"[" * (MAX_FRAME_CONTAINER_DEPTH + 1) + b"]" * (MAX_FRAME_CONTAINER_DEPTH + 1)
    with pytest.raises(StudioMcpProtocolError, match="nested past"):
        StdoutFrameReader().parse_frame(too_deep)


def test_nesting_up_to_the_cap_is_still_parsed():
    at_the_cap = b"[" * MAX_FRAME_CONTAINER_DEPTH + b"]" * MAX_FRAME_CONTAINER_DEPTH
    assert StdoutFrameReader().parse_frame(at_the_cap) is None, "a list is not a JSON-RPC frame"


def test_braces_inside_tool_output_are_not_structure():
    """The depth scan reads strings the way JSON does, so Luau source cannot trip it.

    A tool result holding a thousand braces (a minified table, an escaped quote
    next to one) is an ordinary answer: every one of them arrives while the walk
    is inside a string literal, so none of them is structure. Counting raw bytes
    would have refused the frame. The scan used to blank the string literals with
    a regex before counting, which reached the same verdict and backtracked
    quadratically to get there; it now tracks quote and escape state as it walks.
    """
    source = '{' * (MAX_FRAME_CONTAINER_DEPTH * 2) + '\\" }'
    frame = json.dumps({"id": 1, "result": {"content": source}}).encode("utf-8")
    assert StdoutFrameReader().parse_frame(frame)["result"]["content"] == source


@pytest.mark.parametrize("method", [None, 7, ["roots/list"], {"name": "roots/list"}])
def test_a_frame_whose_method_is_not_a_name_is_a_protocol_error(method):
    """Neither reader claimed this frame, so it cost the caller the whole deadline.

    `response_matches_request` refused it for carrying a `method` at all, and
    `method_not_found_reply` declined to answer something whose method is not a
    name. Between the two it was read as nothing, while wearing the id the
    caller was waiting on. Both now apply `isinstance(method, str)`, and the
    frame that made them disagree does not get past parsing.
    """
    frame = json.dumps({"jsonrpc": "2.0", "id": AWAITED_ID, "method": method}).encode("utf-8")
    with pytest.raises(StudioMcpProtocolError, match="neither a request nor a response"):
        StdoutFrameReader().parse_frame(frame)


def test_the_two_readings_of_method_agree_on_every_frame_that_parses():
    """One rule, checked from both sides: a frame is our answer or a question, never both."""
    answer = {"jsonrpc": "2.0", "id": AWAITED_ID, "result": {}}
    question = {"jsonrpc": "2.0", "id": AWAITED_ID, "method": "roots/list"}

    assert response_matches_request(answer, AWAITED_ID) is True
    assert method_not_found_reply(answer) is None
    assert response_matches_request(question, AWAITED_ID) is False
    assert method_not_found_reply(question) is not None
