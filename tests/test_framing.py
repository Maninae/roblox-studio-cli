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

import pytest

from roblox_studio_cli import framing as framing_module
from roblox_studio_cli.errors import StudioMcpError, StudioMcpProtocolError
from roblox_studio_cli.framing import (
    LARGE_FRAME_BYTES,
    MAX_FRAME_CONTAINER_DEPTH,
    MAX_MESSAGE_BYTES,
    MAX_TOOLS_LIST_TOTAL_BYTES,
    StdoutFrameReader,
    response_matches_request,
)

AWAITED_ID = 7
OTHER_ID = 999_999
PADDING_BYTES = LARGE_FRAME_BYTES * 3


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
    """The depth scan blanks string literals first, so Luau source cannot trip it.

    A tool result holding a thousand braces (a minified table, an escaped
    quote next to one) is an ordinary answer, and counting raw bytes would
    have refused it.
    """
    source = '{' * (MAX_FRAME_CONTAINER_DEPTH * 2) + '\\" }'
    frame = json.dumps({"id": 1, "result": {"content": source}}).encode("utf-8")
    assert StdoutFrameReader().parse_frame(frame)["result"]["content"] == source
