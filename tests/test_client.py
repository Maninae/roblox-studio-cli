"""Transport tests for `StudioMcpClient`, against the fake server in this directory.

None of these need Roblox installed. `tests/fake_studio_mcp_server.py` speaks the
same wire protocol, including the behaviours that are hardest to get right by
inspection: a proxy that answers `initialize` and then goes silent because
Studio never attached, notifications sharing a write with a response, a response
split mid-JSON, a stderr flood, and frames that are malformed outright.

The last group came out of a pen-test round, and each one used to end in a
traceback or a hang rather than an error: undecodable bytes, a flood of large
frames answering nobody, a page bigger than the whole list is allowed to be, and
a proxy that accepts a connection and then stops reading its stdin. The framing
those exercise is unit-tested in `test_framing.py`; here they run end to end,
through a real pipe and a real deadline.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from roblox_studio_cli import client as client_module
from roblox_studio_cli.client import (
    PROXY_NO_TOOLS_STDERR_MARKER,
    STUDIO_NOT_ENABLED_MESSAGE,
    StudioMcpClient,
)
from roblox_studio_cli.errors import (
    StudioMcpError,
    StudioMcpProtocolError,
    StudioMcpTimeoutError,
    StudioNotConnectedError,
)
from roblox_studio_cli.framing import MAX_MESSAGE_BYTES

FAKE_SERVER_PATH = Path(__file__).resolve().parent / "fake_studio_mcp_server.py"
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"
SILENT_SERVER_TIMEOUT_SECONDS = 2.0
DEAF_SERVER_TIMEOUT_SECONDS = 2.0
STUDIO_ID = "studio-1"
STRAY_FRAME_COUNT = 6
DECOY_ANSWER_TIMEOUT_SECONDS = 3.0

# Answers the handshake, then swallows everything else without logging a thing:
# silence that is NOT the Studio toggle, and must not be reported as the toggle.
SILENT_AFTER_HANDSHAKE_SCRIPT = (
    "import json, sys;"
    "request = json.loads(sys.stdin.readline());"
    "sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': "
    "{'protocolVersion': '2024-11-05', 'capabilities': {}, "
    "'serverInfo': {'name': 'Silent', 'version': '0'}}}) + '\\n');"
    "sys.stdout.flush();"
    "[sys.stdin.readline() for _ in range(64)]"
)


@pytest.fixture
def fake_client(monkeypatch):
    """Factory for a started client wired to the fake server in a given mode."""
    opened: list[StudioMcpClient] = []

    def open_client(mode: str = "connected") -> StudioMcpClient:
        monkeypatch.setenv("FAKE_STUDIO_MODE", mode)
        client = StudioMcpClient(command=[sys.executable, str(FAKE_SERVER_PATH)])
        client.start()
        opened.append(client)
        return client

    yield open_client
    for client in opened:
        client.close()


def test_initialize_reports_server_info(fake_client):
    client = fake_client()
    assert client.server_info["name"] == "FakeRobloxStudio"
    assert "Studio MCP Proxy" in client.server_instructions


def test_start_is_idempotent(fake_client):
    client = fake_client()
    process = client.process
    client.start()
    assert client.process is process


def test_list_tools_follows_pagination(fake_client):
    """The fake server splits its tools across two cursor pages; both must arrive."""
    tools = fake_client().list_tools()
    names = [tool.name for tool in tools]
    assert "execute_luau" in names
    assert "start_stop_play" in names, "second cursor page was dropped"
    assert len(names) == len(set(names)), "a page was fetched twice"


def test_tool_definition_exposes_schema(fake_client):
    tools = {tool.name: tool for tool in fake_client().list_tools()}
    luau = tools["execute_luau"]
    assert luau.argument_names == ["code", "datamodel_type", "studio_id"]
    assert set(luau.required_argument_names) == {"code", "datamodel_type", "studio_id"}
    assert luau.property_schema("datamodel_type")["enum"] == ["Edit", "Client", "Server"]
    assert luau.property_schema("nonexistent") == {}


def test_call_tool_returns_text(fake_client):
    result = fake_client().call_tool(
        "execute_luau", {"code": "return 1 + 1", "datamodel_type": "Edit", "studio_id": STUDIO_ID}
    )
    assert result.is_error is False
    assert "luau ok:" in result.text
    assert "return 1 + 1" in result.text


def test_call_tool_returns_decodable_image(fake_client):
    result = fake_client().call_tool("screen_capture", {"studio_id": STUDIO_ID, "capture_id": "c1"})
    assert result.text == ""
    assert len(result.images) == 1
    image = result.images[0]
    assert image.mime_type == "image/png"
    assert image.file_extension() == ".png"
    assert image.decoded_bytes().startswith(PNG_MAGIC_BYTES)


def test_call_tool_reports_tool_error_without_raising(fake_client):
    """An `isError` result is data, not an exception: the text is the diagnosis."""
    result = fake_client().call_tool("boom_tool", {"studio_id": STUDIO_ID})
    assert result.is_error is True
    assert "blew up" in result.text


def test_missing_required_argument_raises_protocol_error(fake_client):
    """The fake server validates like the real one, so CLI fill-in bugs cannot hide."""
    with pytest.raises(StudioMcpProtocolError, match="capture_id"):
        fake_client().call_tool("screen_capture", {"studio_id": STUDIO_ID})


def test_unknown_tool_raises_protocol_error(fake_client):
    with pytest.raises(StudioMcpProtocolError) as raised:
        fake_client().call_tool("no_such_tool", {})
    assert raised.value.code == -32602
    assert "no_such_tool" in raised.value.rpc_message


def test_interleaved_notifications_do_not_strand_the_response(fake_client):
    """chatty mode puts two notifications and the response in ONE write."""
    tools = fake_client("chatty").list_tools()
    assert "execute_luau" in [tool.name for tool in tools]


def test_response_split_across_two_writes_is_reassembled(fake_client):
    """partial mode cuts every frame in half mid-JSON, with a pause between."""
    result = fake_client("partial").call_tool("get_studio_state", {"studio_id": STUDIO_ID})
    assert "Baseplate" in result.text


def test_stderr_flood_neither_wedges_nor_is_kept(fake_client):
    """A 200 KB stderr line is drained and dropped; the ordinary line after it survives."""
    client = fake_client("noisy-stderr")
    assert client.list_tools()
    recent = client.recent_stderr()
    assert "back to normal" in recent
    assert len(recent) < 10_000, "the over-long line was buffered instead of discarded"


def test_malformed_frames_are_survivable(fake_client):
    """A non-object frame, a numeric tool name, and a string `error` member."""
    client = fake_client("malformed")
    names = [tool.name for tool in client.list_tools()]
    assert "execute_luau" in names, "a stray non-object stdout frame broke the read"
    assert 123 not in names and "123" not in names, "a numeric tool name was accepted"

    with pytest.raises(StudioMcpProtocolError) as raised:
        client.call_tool("execute_luau", {"studio_id": STUDIO_ID})
    assert "boom" in str(raised.value)


def test_oversized_line_is_refused_instead_of_buffered(fake_client):
    """The guard that kept a runaway proxy from turning 64 MB into 2.5 GB of RSS."""
    client = fake_client()
    client.frames.buffer = bytearray(b"x" * (MAX_MESSAGE_BYTES + 1))
    process = client.process
    with pytest.raises(StudioMcpError, match="no line break"):
        client.enforce_message_size_limit()
    assert process.poll() is not None, "the runaway proxy was left running"


def test_silent_tools_list_names_the_studio_toggle(fake_client):
    """The whole point of the client: silence PLUS the WARN reads as "turn the toggle on"."""
    client = fake_client("no-tools")
    with pytest.raises(StudioNotConnectedError) as raised:
        client.list_tools(timeout=SILENT_SERVER_TIMEOUT_SECONDS)

    message = str(raised.value)
    assert STUDIO_NOT_ENABLED_MESSAGE in message
    assert "MCP Servers" in message
    assert PROXY_NO_TOOLS_STDERR_MARKER in message, "the proxy's own WARN was not surfaced"


def test_silence_without_the_warning_stays_a_timeout():
    """Without the proxy's marker, a timeout is a timeout and must not blame the toggle."""
    client = StudioMcpClient(command=[sys.executable, "-c", SILENT_AFTER_HANDSHAKE_SCRIPT])
    try:
        client.start()
        with pytest.raises(StudioMcpTimeoutError):
            client.list_tools(timeout=1.0)
    finally:
        client.close()


def test_context_manager_reaps_the_process(monkeypatch):
    monkeypatch.setenv("FAKE_STUDIO_MODE", "connected")
    with StudioMcpClient(command=[sys.executable, str(FAKE_SERVER_PATH)]) as client:
        process = client.process
        assert client.list_tools()
    assert process.poll() is not None, "the proxy process outlived the context manager"
    assert client.process is None


def test_failed_handshake_does_not_leak_the_child(monkeypatch):
    """A proxy that never answers `initialize` must still be reaped, not orphaned."""
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(client_module.subprocess, "Popen", recording_popen)
    client = StudioMcpClient(command=[sys.executable, "-c", "import sys; sys.stdin.read()"])
    with pytest.raises(StudioMcpTimeoutError):
        client.start(timeout=0.5)
    assert spawned and spawned[0].poll() is not None, "the child outlived the failed handshake"
    assert client.process is None


def test_close_is_safe_to_repeat(monkeypatch):
    monkeypatch.setenv("FAKE_STUDIO_MODE", "connected")
    client = StudioMcpClient(command=[sys.executable, str(FAKE_SERVER_PATH)])
    client.start()
    client.close()
    client.close()


def test_calls_before_start_fail_loudly():
    client = StudioMcpClient(command=[sys.executable, str(FAKE_SERVER_PATH)])
    with pytest.raises(StudioMcpError, match="not started"):
        client.list_tools()


def test_missing_binary_names_the_env_override():
    client = StudioMcpClient(command=["/nonexistent/StudioMCP"])
    with pytest.raises(StudioMcpError, match="ROBLOX_STUDIO_MCP_BIN"):
        client.start()


def test_an_undecodable_frame_is_skipped_rather_than_raising_a_traceback(fake_client):
    """Invalid UTF-8 reached json.loads as bytes and came back as a UnicodeDecodeError."""
    client = fake_client("invalid-utf8")
    assert "execute_luau" in [tool.name for tool in client.list_tools()]


def test_large_frames_for_other_requests_are_dropped_before_they_are_parsed(fake_client):
    """Six 200 KB frames with stray ids: skipped unparsed, and the real answer still lands."""
    client = fake_client("stray-flood")
    assert "execute_luau" in [tool.name for tool in client.list_tools()]
    assert client.frames.skipped_large_frames == STRAY_FRAME_COUNT


def test_a_small_frame_for_another_request_is_still_parsed_and_skipped_normally(fake_client):
    """The peek is for large frames only; notifications and stray small ids go the old way."""
    client = fake_client("chatty")
    assert client.list_tools()
    assert client.frames.skipped_large_frames == 0


def test_a_large_answer_that_quotes_another_id_still_reaches_the_caller(fake_client):
    """The frame is ours; the other id is in its payload. Dropping it burned the timeout.

    Measured before the fix: this call took 6.09 s and ended in a timeout, because
    the peek read the record id in the head window as the frame's own.
    """
    client = fake_client("decoy-id")
    started = time.monotonic()
    result = client.call_tool(
        "execute_luau",
        {"code": "return 1", "datamodel_type": "Edit", "studio_id": STUDIO_ID},
        timeout=DECOY_ANSWER_TIMEOUT_SECONDS,
    )
    assert "luau ok:" in result.text
    assert client.frames.skipped_large_frames == 0, "our own answer was dropped"
    assert time.monotonic() - started < DECOY_ANSWER_TIMEOUT_SECONDS, "the answer was waited out"


def test_one_request_cannot_parse_more_than_its_byte_budget(fake_client):
    """A 5 MB tools/list stays under the per-frame cap and still must not be parsed."""
    client = fake_client("huge-list")
    with pytest.raises(StudioMcpError, match="budget"):
        client.list_tools(timeout=SILENT_SERVER_TIMEOUT_SECONDS)


def test_a_server_that_stops_reading_its_input_times_out_instead_of_wedging(fake_client):
    """A 300 KB request against a deaf proxy used to park in write() past every deadline."""
    client = fake_client("deaf-stdin")
    started = time.monotonic()
    with pytest.raises(StudioMcpTimeoutError, match="stopped reading its input"):
        client.send_request(
            "tools/call",
            {"name": "execute_luau", "arguments": {"code": "x" * 300_000}},
            timeout=DEAF_SERVER_TIMEOUT_SECONDS,
        )
    assert time.monotonic() - started < DEAF_SERVER_TIMEOUT_SECONDS * 3, "the write outlived it"
