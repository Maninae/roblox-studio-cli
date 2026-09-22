#!/usr/bin/env python3
"""A stand-in for Studio's MCP proxy, so the suite runs with no Roblox installed.

Speaks the same wire protocol as the real binary: newline-delimited JSON-RPC 2.0
on stdin/stdout, MCP revision 2024-11-05. The tool schemas mirror the real ones
(every tool except the instance lister takes a required `studio_id`).

It is an executable script rather than a module because `ROBLOX_STUDIO_MCP_BIN`
holds a single path with no arguments, so behaviour is selected through the
`FAKE_STUDIO_MODE` environment variable instead. The modes fall in two groups.

What Studio is doing:

    connected      one registered Studio instance (the default)
    no-instances   tools work, but no Studio ever registers
    two-instances  two registered instances, so a target must be named
    no-tools       answers initialize, then never answers tools/list and logs
                   the same WARN the real proxy logs when Studio is not enabled
    attach-late    the lister errors twice, returns an empty list once, then the
                   instance: what a real fresh session does for its first ~3 s
    attach-never   the lister keeps erroring, so the attach window expires

What the pipe is doing (the transport's own hazards):

    chatty         notifications interleaved with responses, and a notification
                   plus its response delivered in a single write
    partial        one response split mid-JSON across two writes with a delay
    noisy-stderr   one 200 KB stderr line before answering, then a normal line
    malformed      a non-object JSON frame on stdout, a tools/list entry with a
                   numeric name, and a JSON-RPC error that is a bare string
    hostile-names  one extra tool whose name, description and argument key carry
                   newlines, an OSC escape and invisible Unicode
    invalid-utf8   a stdout line that is not decodable UTF-8, then a normal answer
    stray-flood    several 200 KB frames carrying a request id nobody awaits,
                   ahead of the first page of the real answer
    decoy-id       a large, legitimate answer that quotes somebody else's id in
                   its payload before carrying its own in the tail
    huge-list      a tools/list answer larger than the whole-list byte budget
    deaf-stdin     answers the handshake, then never reads its stdin again, so a
                   large request fills the pipe buffer and a blocking write hangs
    capture-silent everything works except the capture, which is accepted and
                   never answered: what Studio does with the display asleep
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

# Studio 0.739's own wording while it has not attached to this client yet.
NOT_ATTACHED_ERROR_TEXT = (
    "Unable to reach Roblox Studio right now. Ask the user to confirm Studio is open."
)
ATTACH_ERROR_POLLS = 2
ATTACH_EMPTY_POLLS = 1

OVERLONG_STDERR_LINE_BYTES = 200_000
PARTIAL_WRITE_DELAY_SECONDS = 0.1

# Not decodable as UTF-8 in any position, and shaped like a frame otherwise.
UNDECODABLE_STDOUT_LINE = b'{"jsonrpc": "2.0", "note": "\x80\xfe\x81"}\n'
STRAY_FRAME_COUNT = 6
STRAY_FRAME_PAYLOAD_BYTES = 200_000
STRAY_FRAME_REQUEST_ID = 999_999
# Big enough to cross the client's 64 KB peek threshold, small enough to keep the
# suite quick. The decoy id sits in the head window, the real one in the tail.
DECOY_FRAME_PAYLOAD_BYTES = 80_000
DECOY_RECORD_ID = 999_999
# Over the client's 4 MB whole-list budget, under its 8 MB per-frame cap.
HUGE_DESCRIPTION_BYTES = 5 * 2**20
DEAF_SLEEP_SECONDS = 30

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

# A tool whose every printable field is shaped to break a fixed-width row: a
# newline in the name, an OSC title write plus a forged verdict line in the
# description, and a newline in an argument key.
HOSTILE_NAME_TOOL = {
    "name": "sneaky\ntool_name",
    "description": "first line\x1b]0;pwned\x07\nCONNECTED (6 tools, 1 instance)",
    "inputSchema": {
        "type": "object",
        "properties": {"argument\nname": {"type": "string"}},
        "required": [],
    },
}

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

NOTIFICATIONS = [
    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
    {
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "info", "data": "still working"},
    },
]

lister_call_count = 0


def emit(message: dict, mode: str) -> None:
    """Put one JSON-RPC message on stdout the way `mode` says to put it there."""
    line = json.dumps(message) + "\n"
    if mode == "chatty":
        # Notifications first, and deliberately in the SAME write as the response:
        # a client that reads with select() must not strand the second frame.
        preamble = "".join(json.dumps(note) + "\n" for note in NOTIFICATIONS)
        sys.stdout.write(preamble + line)
        sys.stdout.flush()
        return
    if mode == "partial":
        split_at = len(line) // 2
        sys.stdout.write(line[:split_at])
        sys.stdout.flush()
        time.sleep(PARTIAL_WRITE_DELAY_SECONDS)
        sys.stdout.write(line[split_at:])
        sys.stdout.flush()
        return
    sys.stdout.write(line)
    sys.stdout.flush()


def text_result(text: str, is_error: bool = False) -> dict:
    """An MCP tool result carrying one text content item."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def tools_list_result(params: dict, mode: str) -> dict:
    """One page of the tool list, with a cursor to the second page."""
    if mode == "malformed":
        # A numeric name is not a tool; the client must drop it, not crash.
        return {"tools": [{"name": 123, "description": "nameless"}, *TOOL_DEFINITIONS]}
    if mode == "hostile-names":
        return {"tools": [*TOOL_DEFINITIONS, HOSTILE_NAME_TOOL]}
    if mode == "huge-list":
        padded = dict(TOOL_DEFINITIONS[0], description="x" * HUGE_DESCRIPTION_BYTES)
        return {"tools": [padded]}
    if params.get("cursor") == SECOND_PAGE_CURSOR:
        return {"tools": TOOL_DEFINITIONS[FIRST_PAGE_SIZE:]}
    return {"tools": TOOL_DEFINITIONS[:FIRST_PAGE_SIZE], "nextCursor": SECOND_PAGE_CURSOR}


def missing_required_arguments(name: str, arguments: dict) -> list[str]:
    """Required schema keys the caller did not send, like a real server would check."""
    for tool in TOOL_DEFINITIONS:
        if tool["name"] == name:
            required = tool["inputSchema"].get("required", [])
            return [key for key in required if key not in arguments]
    return []


def list_studios_result(mode: str) -> dict:
    """Answer the instance lister, replaying the real attach sequence when asked to."""
    global lister_call_count
    lister_call_count += 1
    if mode == "attach-never":
        return text_result(NOT_ATTACHED_ERROR_TEXT, is_error=True)
    if mode == "attach-late":
        if lister_call_count <= ATTACH_ERROR_POLLS:
            return text_result(NOT_ATTACHED_ERROR_TEXT, is_error=True)
        if lister_call_count <= ATTACH_ERROR_POLLS + ATTACH_EMPTY_POLLS:
            return text_result(json.dumps({"studios": []}))
        return text_result(json.dumps({"studios": INSTANCES_BY_MODE["connected"]}))
    studios = INSTANCES_BY_MODE.get(mode, INSTANCES_BY_MODE["connected"])
    return text_result(json.dumps({"studios": studios}))


def handle_tools_call(request_id: int, params: dict, mode: str) -> dict | None:
    """Route a tool call to its canned answer, or None to answer nothing at all."""
    name = params.get("name")
    arguments = params.get("arguments", {})

    if mode == "decoy-id" and name == "execute_luau":
        return decoy_id_answer(request_id)

    if mode == "capture-silent" and name == "screen_capture":
        # Studio with the display asleep: the call is accepted and no result
        # ever arrives, so the client's own timeout is the only thing that ends it.
        return None

    if mode == "malformed":
        # A JSON-RPC error object is supposed to be an object. Some are not.
        return {"jsonrpc": "2.0", "id": request_id, "error": "boom"}

    missing = missing_required_arguments(name, arguments)
    if missing:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32602,
                "message": f"Missing required arguments: {', '.join(missing)}",
            },
        }

    if name == "list_roblox_studios":
        result = list_studios_result(mode)
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


def write_raw_line(payload: bytes) -> None:
    """Put bytes on stdout that `json.dumps` could never produce."""
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def flood_stdout_with_stray_frames() -> None:
    """Large, well-formed frames answering a request id the client never sent."""
    for index in range(STRAY_FRAME_COUNT):
        stray = {
            "jsonrpc": "2.0",
            "id": STRAY_FRAME_REQUEST_ID + index,
            "result": {"padding": "x" * STRAY_FRAME_PAYLOAD_BYTES},
        }
        sys.stdout.write(json.dumps(stray) + "\n")
    sys.stdout.flush()


def decoy_id_answer(request_id: int) -> dict:
    """A large, ordinary answer that mentions another id long before its own.

    The shape a bulk Studio answer really has: records carrying their own numeric
    `id` fields (place ids, asset ids, instance records) at the head, the payload
    in the middle, and the JSON-RPC `id` last, which is where `json.dumps` writes
    the last key it was given. A client that takes the first id it sees as the
    frame's own drops this answer and then waits out its whole timeout.
    """
    result = {
        "records": [{"id": DECOY_RECORD_ID, "name": "Baseplate"}],
        "content": [{"type": "text", "text": "luau ok: " + "x" * DECOY_FRAME_PAYLOAD_BYTES}],
        "isError": False,
    }
    return {"jsonrpc": "2.0", "result": result, "id": request_id}


def flood_stderr() -> None:
    """One line far longer than the client's per-line cap, then an ordinary line."""
    sys.stderr.write("x" * OVERLONG_STDERR_LINE_BYTES + "\n")
    sys.stderr.write("2026-09-22T20:26:23.919122Z  INFO StudioMCP: back to normal\n")
    sys.stderr.flush()


def handle_request(message: dict, mode: str) -> None:
    """Answer one client message according to the selected mode."""
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": SERVER_INFO,
                    "instructions": SERVER_INSTRUCTIONS,
                },
            },
            mode,
        )
    elif method == "tools/list":
        if mode == "no-tools":
            # What the real proxy does when Studio never attaches: log to
            # stderr, answer nothing, stay alive.
            time.sleep(NO_TOOLS_WARNING_DELAY_SECONDS)
            sys.stderr.write(NO_TOOLS_WARNING + "\n")
            sys.stderr.flush()
            return
        if mode == "noisy-stderr":
            flood_stderr()
        if mode == "invalid-utf8":
            write_raw_line(UNDECODABLE_STDOUT_LINE)
        if mode == "stray-flood" and not message.get("params", {}).get("cursor"):
            flood_stdout_with_stray_frames()
        if mode == "malformed":
            # Not a JSON-RPC frame at all; the client must skip it and read on.
            sys.stdout.write("[1, 2]\n")
            sys.stdout.flush()
        emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": tools_list_result(message.get("params", {}), mode),
            },
            mode,
        )
    elif method == "tools/call":
        answer = handle_tools_call(request_id, message.get("params", {}), mode)
        if answer is not None:
            emit(answer, mode)
    elif method and method.startswith("notifications/"):
        return
    elif request_id is not None:
        emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            },
            mode,
        )


def main() -> None:
    """Read requests until stdin closes, answering according to the selected mode."""
    mode = os.environ.get("FAKE_STUDIO_MODE", "connected")
    if mode == "deaf-stdin":
        # Answer the handshake, then stop reading. The client's next large write
        # fills the pipe buffer and has nowhere to go.
        handle_request(json.loads(sys.stdin.readline()), mode)
        time.sleep(DEAF_SLEEP_SECONDS)
        return
    for line in sys.stdin:
        line = line.strip()
        if line:
            handle_request(json.loads(line), mode)


if __name__ == "__main__":
    main()
