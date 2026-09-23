"""Tests for reading an MCP server's payloads: tool entries, results, images, errors.

The image tests are the security-relevant ones. A tool result decides both the
extension a file gets and the bytes inside it, so an unknown MIME type and a
payload that does not match its declared type are both refusals, not guesses.
"""

import base64
import json
import logging
import select

import pytest

from roblox_studio_cli import mcp_payloads as mcp_payloads_module
from roblox_studio_cli.errors import (
    MAX_ERROR_CODE_CHARS,
    UNKNOWN_ERROR_CODE,
    StudioMcpError,
    StudioMcpProtocolError,
)
from roblox_studio_cli.mcp_payloads import (
    MAX_ECHOED_REQUEST_ID_CHARS,
    METHOD_NOT_FOUND_CODE,
    METHOD_NOT_FOUND_MESSAGE,
    ToolDefinition,
    ToolImage,
    build_tool_definitions,
    method_not_found_reply,
    parse_tool_call_result,
    raise_for_rpc_error,
)
from roblox_studio_cli.terminal import MAX_DIAGNOSTIC_TEXT_CHARS

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload"
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode()
# Padding wide enough that quoting it raw is unmistakable in a terminal, and
# small enough to build in a test: the measured leak was 2,000,103 characters.
PADDED_MIME_TYPE_SPACES = 2_000_000


def test_tool_definition_tolerates_a_junk_schema():
    tool = ToolDefinition(name="x", description="d", input_schema={"properties": "nonsense"})
    assert tool.argument_names == []
    assert tool.required_argument_names == []
    assert tool.property_schema("anything") == {}


def test_description_preview_takes_the_first_line_and_truncates():
    tool = ToolDefinition(name="x", description="first line\nsecond line", input_schema={})
    assert tool.description_preview() == "first line"
    long_tool = ToolDefinition(name="x", description="a" * 200, input_schema={})
    assert long_tool.description_preview(20).endswith("...")
    assert len(long_tool.description_preview(20)) == 20


def test_description_preview_sanitises_before_it_truncates():
    """Truncating first spent the budget on characters that were about to be stripped.

    A description opening with a few escape sequences previewed as nearly
    nothing, with the text a reader came for sitting just past the cut.
    """
    padded = "\x1b]0;title\x07" * 5 + "what the tool actually does"
    tool = ToolDefinition(name="x", description=padded, input_schema={})
    assert tool.description_preview(30) == "what the tool actually does"


def test_build_tool_definitions_drops_malformed_entries():
    entries = [
        {"name": "good", "description": "d", "inputSchema": {"properties": {}}},
        {"name": 123},
        {"description": "no name"},
        "not an object",
        {"name": "coerced", "description": 5, "inputSchema": "nonsense"},
    ]
    definitions = build_tool_definitions(entries)
    assert [tool.name for tool in definitions] == ["good", "coerced"]
    assert definitions[1].description == ""
    assert definitions[1].input_schema == {}


def test_decoded_bytes_accepts_line_wrapped_base64():
    wrapped = "\n".join([PNG_BASE64[:6], PNG_BASE64[6:]])
    assert ToolImage("image/png", wrapped).decoded_bytes() == PNG_BYTES


def test_unknown_mime_type_is_refused_rather_than_called_a_png():
    for mime_type in ("application/x-sh", ""):
        with pytest.raises(StudioMcpError, match="unsupported type"):
            ToolImage(mime_type, PNG_BASE64).file_extension()


def test_a_payload_that_is_not_the_declared_format_is_refused():
    script = base64.b64encode(b"#!/bin/sh\nrm -rf ~").decode()
    with pytest.raises(StudioMcpError, match="signature mismatch"):
        ToolImage("image/png", script).decoded_bytes()


def test_undecodable_base64_is_refused():
    with pytest.raises(StudioMcpError, match="undecodable"):
        ToolImage("image/png", "not base64 at all!!").decoded_bytes()


def test_parse_tool_call_result_flattens_mixed_content():
    """Text, image and embedded-resource items all land where callers expect."""
    result = parse_tool_call_result(
        {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "image", "data": PNG_BASE64, "mimeType": "image/png"},
                {"type": "resource", "resource": {"uri": "x://y", "text": "second"}},
                {"type": "audio", "data": "ignored"},
            ],
            "isError": False,
        }
    )
    assert result.text == "first\nsecond"
    assert len(result.images) == 1
    assert result.raw["content"][0]["text"] == "first"


def test_parse_tool_call_result_defaults_to_success():
    assert parse_tool_call_result({}).is_error is False
    assert parse_tool_call_result({"content": "not a list"}).text == ""


@pytest.mark.parametrize(
    "flag, expected, reason",
    [
        (True, True, "the spec's own failure value"),
        (False, False, "the spec's own success value"),
        (None, False, "absent means the call worked"),
        ("false", True, "a string is not a boolean, and a reader would call this a failure"),
        ("true", True, "a server that stringified its flag still reported a failure"),
        (1, True, "the conventional true"),
        (0, False, "the conventional false"),
    ],
)
def test_is_error_is_read_as_the_boolean_the_spec_says_it_is(flag, expected, reason):
    """`bool("false")` is True and `bool(0)` is False, so neither can decide this alone.

    Only a real `True` is honoured as written. Anything else truthy is still a
    failure, because a server that put something in this field is not reporting
    success, and calling `"false"` a success would hide the one case that matters.
    """
    assert parse_tool_call_result({"isError": flag}).is_error is expected, reason


@pytest.fixture
def never_warned(monkeypatch):
    """Forget which types this process already warned about, one warning each."""
    monkeypatch.setattr(mcp_payloads_module, "warned_is_error_type_names", set())


def test_a_non_boolean_is_error_is_logged_as_the_protocol_oddity_it_is(caplog, never_warned):
    """The outcome is the same either way; the point is that somebody can find out why.

    A build that starts sending `"isError": "false"` is a Studio-side protocol
    change, and every call from it would read as a failure with no clue on the
    wire. The type goes to the log, never the value: a log record printed by the
    last-resort handler would put server text on a terminal unsanitised.
    """
    with caplog.at_level(logging.WARNING, logger="roblox_studio_cli.mcp_payloads"):
        assert parse_tool_call_result({"isError": "false"}).is_error is True
    assert "isError" in caplog.text and "str" in caplog.text
    assert "false" not in caplog.text, "server text reached a log record"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="roblox_studio_cli.mcp_payloads"):
        parse_tool_call_result({"isError": True})
    assert caplog.text == "", "an ordinary boolean warned about nothing"


def test_the_protocol_warning_is_said_once_for_the_process_not_once_per_result(
    caplog, never_warned
):
    """One attach poll is two dozen calls, and a stuck build makes every one of them odd.

    The fact is about the build, so it is worth saying once. Said per result, a
    12-second attach wait against a server with a string `isError` printed the
    same warning two dozen times over whatever the command was reporting.
    """
    with caplog.at_level(logging.WARNING, logger="roblox_studio_cli.mcp_payloads"):
        for _ in range(24):
            assert parse_tool_call_result({"isError": "false"}).is_error is True
    assert len(caplog.records) == 1, f"warned {len(caplog.records)} times"


def test_a_second_odd_is_error_type_is_a_second_fact_and_gets_said(caplog, never_warned):
    """One latch for every type meant the second oddity was swallowed by the first.

    A build sending `"false"` from one tool and `1` from another has two things
    wrong with it, and the one that got logged was whichever ran first.
    """
    with caplog.at_level(logging.WARNING, logger="roblox_studio_cli.mcp_payloads"):
        parse_tool_call_result({"isError": "false"})
        parse_tool_call_result({"isError": 2})
        parse_tool_call_result({"isError": [1]})
        parse_tool_call_result({"isError": "true"})
    logged_types = [record.args[0] for record in caplog.records]
    assert logged_types == ["str", "int", "list"], "a different type went unsaid"


@pytest.mark.parametrize("answered_id", [2, "2"])
def test_a_request_the_server_makes_of_us_is_answered_method_not_found(answered_id):
    """MCP servers call `roots/list` and friends on their clients; we implement none."""
    reply = method_not_found_reply(
        {"jsonrpc": "2.0", "id": answered_id, "method": "roots/list"}
    )
    assert reply == {
        "jsonrpc": "2.0",
        "id": answered_id,
        "error": {"code": METHOD_NOT_FOUND_CODE, "message": METHOD_NOT_FOUND_MESSAGE},
    }


@pytest.mark.parametrize(
    "message, reason",
    [
        ({"id": 2, "result": {}}, "a response is an answer, not a question"),
        ({"id": 2, "error": {"code": -1}}, "so is an error response"),
        ({"method": "notifications/message"}, "a notification wants no reply"),
        ({"id": None, "method": "roots/list"}, "and neither does a null id"),
        ({"id": True, "method": "roots/list"}, "a bool is not a JSON-RPC id"),
        ({"id": 1.5, "method": "roots/list"}, "nor is a fraction"),
        ({"id": "x" * (MAX_ECHOED_REQUEST_ID_CHARS + 1), "method": "roots/list"},
         "the server would be choosing the size of our write"),
        ({"id": 10 ** (MAX_ECHOED_REQUEST_ID_CHARS + 1), "method": "roots/list"},
         "a number chooses the size of our write the same way a string does"),
    ],
)
def test_nothing_is_replied_to_a_frame_that_asked_nothing(message, reason):
    assert method_not_found_reply(message) is None, reason


@pytest.mark.parametrize("answered_id", [10**8, -(10**8), "x" * MAX_ECHOED_REQUEST_ID_CHARS])
def test_an_echoed_id_within_the_cap_keeps_the_reply_atomically_writable(answered_id):
    """The decline is written once, unretried, so it has to fit one atomic pipe write.

    PIPE_BUF is 512 bytes on macOS, and anything at or under it reaches the
    proxy whole or not at all. An id is echoed verbatim, so the cap on it is
    what keeps that true; `client.decline_server_request` measures the encoded
    reply as well, for an id whose escapes are longer than its characters.
    """
    reply = method_not_found_reply({"id": answered_id, "method": "roots/list"})
    assert reply is not None
    assert len(json.dumps(reply).encode()) < select.PIPE_BUF


def test_raise_for_rpc_error_reads_both_shapes():
    raise_for_rpc_error({"result": {}})
    raise_for_rpc_error({"error": None, "result": {}})

    with pytest.raises(StudioMcpProtocolError) as structured:
        raise_for_rpc_error({"error": {"code": -32602, "message": "Unknown tool: x"}})
    assert structured.value.code == -32602

    with pytest.raises(StudioMcpProtocolError) as bare:
        raise_for_rpc_error({"error": "boom"})
    assert bare.value.code == -1 and "boom" in str(bare.value)


def test_a_boolean_error_code_is_not_the_integer_it_equals():
    """`True` is an `int` in Python, so an isinstance check read this as error 1."""
    with pytest.raises(StudioMcpProtocolError) as raised:
        raise_for_rpc_error({"error": {"code": True, "message": "boom"}})
    assert raised.value.code == UNKNOWN_ERROR_CODE
    assert f"JSON-RPC error {UNKNOWN_ERROR_CODE}" in str(raised.value)


def test_a_giant_error_code_is_capped_where_it_prints():
    """A Python integer has no width limit, and the server picks this one.

    Measured: `10 ** 4000` put 4,028 characters of chrome in front of the
    message the caller needs. The value survives on the exception; the line
    that gets printed does not carry it.
    """
    enormous = 10**4000
    with pytest.raises(StudioMcpProtocolError) as raised:
        raise_for_rpc_error({"error": {"code": enormous, "message": "boom"}})
    assert raised.value.code == enormous, "the code itself was thrown away"
    capped = "1" + "0" * (MAX_ERROR_CODE_CHARS - 1)
    assert str(raised.value) == f"JSON-RPC error {capped}...: boom"


def test_rpc_error_text_cannot_carry_escape_sequences():
    with pytest.raises(StudioMcpProtocolError) as raised:
        raise_for_rpc_error({"error": {"code": 1, "message": "bad\x1b]0;title\x07"}})
    assert "\x1b" not in str(raised.value)


@pytest.mark.parametrize(
    "envelope",
    [
        {"error": {"code": 1, "message": "x" * 5_000_000}},
        {"error": "x" * 5_000_000},
    ],
)
def test_a_giant_rpc_error_message_is_capped(envelope):
    """The message is chrome around a failure, and the server chooses its length.

    Measured: a 5 MB message printed as 5 MB of stderr, scrolling away the
    command that caused it.
    """
    with pytest.raises(StudioMcpProtocolError) as raised:
        raise_for_rpc_error(envelope)
    assert len(raised.value.rpc_message) == MAX_DIAGNOSTIC_TEXT_CHARS
    assert raised.value.rpc_message.endswith("...")


def test_a_padded_mime_type_is_capped_on_the_signature_mismatch_path_too():
    """The type that reaches the mismatch is RECOGNISED, and still server-sized.

    `file_extension` strips and folds the declared type before looking it up, so
    "image/png" with two million spaces after it resolves to `.png` and sails
    past the unsupported-type cap next door. The mismatch error then quoted the
    field as sent: 2,000,103 characters on stderr, with the sentence saying the
    file was refused pushed off the screen ahead of them.
    """
    padded = ToolImage(
        mime_type="image/png" + " " * PADDED_MIME_TYPE_SPACES,
        data_base64=base64.b64encode(b"#!/bin/sh").decode(),
    )

    assert padded.file_extension() == ".png", "the padding has to survive the lookup"
    with pytest.raises(StudioMcpError) as raised:
        padded.decoded_bytes()

    assert len(str(raised.value)) < MAX_DIAGNOSTIC_TEXT_CHARS * 2, len(str(raised.value))
    assert "signature mismatch" in str(raised.value)
    assert "image/png" in str(raised.value), "the caller still needs the type it declared"


def test_an_unsupported_mime_type_is_capped_before_it_is_quoted():
    """The type a tool declared is server-chosen text, and this error quotes it back.

    Unlike the MIME type in the extension warning next door, this one is the
    UNRECOGNISED type, so its length is whatever the server felt like: 500 KB of
    it buried the advice under the quote, which is the line telling the caller
    how to get their payload anyway.
    """
    giant = ToolImage(mime_type="image/" + "x" * 500_000, data_base64="")

    with pytest.raises(StudioMcpError) as raised:
        giant.file_extension()

    assert len(str(raised.value)) < MAX_DIAGNOSTIC_TEXT_CHARS * 3, len(str(raised.value))
    assert "--json and no --out" in str(raised.value)
