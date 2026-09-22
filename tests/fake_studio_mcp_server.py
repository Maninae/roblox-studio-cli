#!/usr/bin/env python3
"""A stand-in for Studio's MCP proxy, so the suite runs with no Roblox installed.

Speaks the same wire protocol as the real binary: newline-delimited JSON-RPC 2.0
on stdin/stdout, MCP revision 2024-11-05. The tool schemas mirror the real ones
(every tool except the instance lister takes a required `studio_id`).

It is an executable script rather than a module because `ROBLOX_STUDIO_MCP_BIN`
holds a single path with no arguments, so the behaviour is selected through the
`FAKE_STUDIO_MODE` environment variable instead:

    connected      one registered Studio instance (the default)
    no-instances   tools work, but no Studio has registered
    two-instances  two registered instances, so a target must be named
    no-tools       answers initialize, then never answers tools/list and logs
                   the same WARN the real proxy logs when Studio is not enabled
"""

import json
import os
import sys
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "FakeRobloxStudio", "version": "1.0.0"}
SERVER_INSTRUCTIONS = "Studio MCP Proxy - bridges MCP clients with Roblox Studio"

# Byte-identical to the real proxy's warning, because the client quotes it back.
NO_TOOLS_WARNING = (
    "2026-09-22T20:26:23.919122Z  WARN StudioMCP::proxy_server::handler: "
    "Timed out waiting for tools to become available"
)
NO_TOOLS_WARNING_DELAY_SECONDS = 0.2

# 1x1 transparent PNG, small enough to inline and still a real decodable image.
ONE_PIXEL_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

STUDIO_ID_PROPERTY = {"studio_id": {"type": "string", "description": "Target Studio instance."}}

TOOL_DEFINITIONS = [
    {
        "name": "list_roblox_studios",
        "description": "List the Roblox Studio instances connected to the bridge.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "execute_luau",
        "description": "Run Luau code in Studio's DataModel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "datamodel_type": {"type": "string", "enum": ["Edit", "Client", "Server"]},
                **STUDIO_ID_PROPERTY,
            },
            "required": ["code", "datamodel_type", "studio_id"],
        },
    },
    {
        "name": "get_studio_state",
        "description": "Report the open place and play state.",
        "inputSchema": {
            "type": "object",
            "properties": dict(STUDIO_ID_PROPERTY),
            "required": ["studio_id"],
        },
    },
    {
        "name": "screen_capture",
        "description": "Capture the Studio viewport.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "camera_position": {"type": "array", "items": {"type": "number"}},
                "look_at_position": {"type": "array", "items": {"type": "number"}},
                "capture_id": {"type": "string"},
                **STUDIO_ID_PROPERTY,
            },
            "required": ["capture_id", "studio_id"],
        },
    },
    {
        "name": "start_stop_play",
        "description": "Enter or leave play mode.",
        "inputSchema": {
            "type": "object",
            "properties": {"is_start": {"type": "boolean"}, **STUDIO_ID_PROPERTY},
            "required": ["is_start", "studio_id"],
        },
    },
    {
        "name": "boom_tool",
        "description": "Always fails, for exercising the isError path.",
        "inputSchema": {
            "type": "object",
            "properties": dict(STUDIO_ID_PROPERTY),
            "required": ["studio_id"],
        },
    },
]

# Served in two pages so the client's nextCursor handling is exercised.
FIRST_PAGE_SIZE = 3
SECOND_PAGE_CURSOR = "page-2"

INSTANCES_BY_MODE = {
    "connected": [{"id": "studio-1", "name": "Baseplate"}],
    "no-instances": [],
    "two-instances": [
        {"id": "studio-1", "name": "Baseplate"},
        {"id": "studio-2", "name": "Obby"},
    ],
}


def write_message(message: dict) -> None:
    """Emit one JSON-RPC message as a single newline-terminated line."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def text_result(text: str, is_error: bool = False) -> dict:
    """An MCP tool result carrying one text content item."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle_tools_list(request_id: int, params: dict) -> dict:
    """Serve one page of the tool list, with a cursor to the second page."""
    if params.get("cursor") == SECOND_PAGE_CURSOR:
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOL_DEFINITIONS[FIRST_PAGE_SIZE:]}}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "tools": TOOL_DEFINITIONS[:FIRST_PAGE_SIZE],
            "nextCursor": SECOND_PAGE_CURSOR,
        },
    }


def missing_required_arguments(name: str, arguments: dict) -> list[str]:
    """Required schema keys the caller did not send, like a real server would check."""
    for tool in TOOL_DEFINITIONS:
        if tool["name"] == name:
            required = tool["inputSchema"].get("required", [])
            return [key for key in required if key not in arguments]
    return []


def handle_tools_call(request_id: int, params: dict, mode: str) -> dict:
    """Route a tool call to its canned answer, mirroring the real result shapes."""
    name = params.get("name")
    arguments = params.get("arguments", {})

    missing = missing_required_arguments(name, arguments)
    if missing:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"Missing required arguments: {', '.join(missing)}"},
        }

    if name == "list_roblox_studios":
        studios = INSTANCES_BY_MODE.get(mode, INSTANCES_BY_MODE["connected"])
        result = text_result(json.dumps({"studios": studios}))
    elif name == "execute_luau":
        # Echo the arguments so tests can assert what discovery filled in.
        result = text_result("luau ok: " + json.dumps(arguments, sort_keys=True))
    elif name == "get_studio_state":
        result = text_result(json.dumps({"place": "Baseplate", "is_playing": False}))
    elif name == "start_stop_play":
        result = text_result("play is_start=" + json.dumps(arguments.get("is_start")))
    elif name == "screen_capture":
        result = {
            "content": [{"type": "image", "data": ONE_PIXEL_PNG_BASE64, "mimeType": "image/png"}],
            "isError": False,
        }
    elif name == "boom_tool":
        result = text_result("the tool blew up", is_error=True)
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"Unknown tool: {name}"},
        }

    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> None:
    """Read requests until stdin closes, answering according to the selected mode."""
    mode = os.environ.get("FAKE_STUDIO_MODE", "connected")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": SERVER_INFO,
                        "instructions": SERVER_INSTRUCTIONS,
                    },
                }
            )
        elif method == "tools/list":
            if mode == "no-tools":
                # What the real proxy does when Studio never attaches: log to
                # stderr, answer nothing, stay alive.
                time.sleep(NO_TOOLS_WARNING_DELAY_SECONDS)
                sys.stderr.write(NO_TOOLS_WARNING + "\n")
                sys.stderr.flush()
                continue
            write_message(handle_tools_list(request_id, message.get("params", {})))
        elif method == "tools/call":
            write_message(handle_tools_call(request_id, message.get("params", {}), mode))
        elif method and method.startswith("notifications/"):
            continue
        elif request_id is not None:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
