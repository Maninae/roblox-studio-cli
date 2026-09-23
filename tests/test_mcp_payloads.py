"""Tests for reading an MCP server's payloads: tool entries, results, images, errors.

The image tests are the security-relevant ones. A tool result decides both the
extension a file gets and the bytes inside it, so an unknown MIME type and a
payload that does not match its declared type are both refusals, not guesses.
"""

import base64
import logging

import pytest

from roblox_studio_cli import mcp_payloads as mcp_payloads_module
from roblox_studio_cli.errors import StudioMcpError, StudioMcpProtocolError
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
    """Forget that this process already warned, since the warning is a one-shot."""
    monkeypatch.setattr(mcp_payloads_module, "warned_about_non_boolean_is_error", False)


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
    ],
)
def test_nothing_is_replied_to_a_frame_that_asked_nothing(message, reason):
    assert method_not_found_reply(message) is None, reason


def test_raise_for_rpc_error_reads_both_shapes():
    raise_for_rpc_error({"result": {}})
    raise_for_rpc_error({"error": None, "result": {}})

    with pytest.raises(StudioMcpProtocolError) as structured:
        raise_for_rpc_error({"error": {"code": -32602, "message": "Unknown tool: x"}})
    assert structured.value.code == -32602

    with pytest.raises(StudioMcpProtocolError) as bare:
        raise_for_rpc_error({"error": "boom"})
    assert bare.value.code == -1 and "boom" in str(bare.value)


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
