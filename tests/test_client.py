"""Transport tests for `StudioMcpClient`, against the fake server in this directory.

None of these need Roblox installed. `tests/fake_studio_mcp_server.py` speaks the
same wire protocol, including the one behaviour that is hardest to get right by
inspection: a proxy that answers `initialize` and then goes silent on
`tools/list` because Studio never attached.
"""

import sys
from pathlib import Path

import pytest

from roblox_studio_cli.client import (
    PROXY_NO_TOOLS_STDERR_MARKER,
    STUDIO_NOT_ENABLED_MESSAGE,
    StudioMcpClient,
    StudioMcpError,
    StudioMcpProtocolError,
    StudioNotConnectedError,
    parse_tool_call_result,
)

FAKE_SERVER_PATH = Path(__file__).resolve().parent / "fake_studio_mcp_server.py"
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"
SILENT_SERVER_TIMEOUT_SECONDS = 2.0
STUDIO_ID = "studio-1"


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
    result = fake_client().call_tool(
        "screen_capture", {"studio_id": STUDIO_ID, "capture_id": "c1"}
    )
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


def test_silent_tools_list_names_the_studio_toggle(fake_client):
    """The whole point of the client: silence must read as "turn the toggle on"."""
    client = fake_client("no-tools")
    with pytest.raises(StudioNotConnectedError) as raised:
        client.list_tools(timeout=SILENT_SERVER_TIMEOUT_SECONDS)

    message = str(raised.value)
    assert STUDIO_NOT_ENABLED_MESSAGE in message
    assert "Manage MCP Servers" in message
    assert PROXY_NO_TOOLS_STDERR_MARKER in message, "the proxy's own WARN was not surfaced"


def test_context_manager_reaps_the_process(monkeypatch):
    monkeypatch.setenv("FAKE_STUDIO_MODE", "connected")
    with StudioMcpClient(command=[sys.executable, str(FAKE_SERVER_PATH)]) as client:
        process = client.process
        assert client.list_tools()
    assert process.poll() is not None, "the proxy process outlived the context manager"
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


def test_parse_tool_call_result_flattens_mixed_content():
    """Text, image and embedded-resource items all land where callers expect."""
    result = parse_tool_call_result(
        {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "image", "data": "Zm9v", "mimeType": "image/png"},
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
