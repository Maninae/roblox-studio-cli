"""The payload shapes an MCP server answers with, and how to read them safely.

Tool definitions, tool call results, the JSON-RPC error envelope, and the one
frame that is not an answer at all: a request the SERVER makes of its client.

Split out of `client` so the transport stays about bytes on a pipe while these
stay about payloads, and so anything that needs a `ToolDefinition` (discovery,
the CLI, the tests) can import it without pulling in `subprocess`.

Two things here are load-bearing for safety:

- An image's extension comes only from a MIME type this module knows. An
  unrecognised type is an error, never a `.png` guess, because the extension
  decides the filename a caller then opens.
- `decoded_bytes` checks the payload's magic bytes against the type it claims,
  so a tool cannot hand back a shell script labelled `image/png`.
"""

import base64
import logging
from dataclasses import dataclass, field

from roblox_studio_cli.errors import (
    UNKNOWN_ERROR_CODE,
    StudioMcpError,
    StudioMcpProtocolError,
)
from roblox_studio_cli.terminal import sanitize_terminal_text, truncate_display_text

logger = logging.getLogger(__name__)

IMAGE_MIME_TYPE_TO_FILE_EXTENSION: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# (offset, magic) pairs that must ALL match for a payload to count as that type.
# WebP needs two: "RIFF" at 0 and "WEBP" at 8, with the file size in between.
IMAGE_SIGNATURES_BY_FILE_EXTENSION: dict[str, tuple[tuple[int, bytes], ...]] = {
    ".png": ((0, b"\x89PNG\r\n\x1a\n"),),
    ".jpg": ((0, b"\xff\xd8\xff"),),
    ".webp": ((0, b"RIFF"), (8, b"WEBP")),
    ".gif": ((0, b"GIF8"),),
}

DESCRIPTION_PREVIEW_CHARS = 100

# The JSON-RPC code for a method this peer does not implement, which is the
# whole of this client's answer to a server-to-client request.
METHOD_NOT_FOUND_CODE = -32601
METHOD_NOT_FOUND_MESSAGE = "this client implements no server-to-client requests"
# How long an id we will echo back into such a reply. The server chose the id,
# the reply is a courtesy, and a courtesy is not worth writing a megabyte-long
# id back down the pipe for.
MAX_ECHOED_REQUEST_ID_CHARS = 128

# A non-boolean `isError` is a fact about the BUILD, not about one result, and
# the attach poll alone calls a tool two dozen times inside one command. Said
# per result, the warning printed over whatever the command was reporting.
warned_about_non_boolean_is_error = False


@dataclass(frozen=True)
class ToolDefinition:
    """One entry from `tools/list`: what the tool is called and what it accepts."""

    name: str
    description: str
    input_schema: dict

    @property
    def argument_names(self) -> list[str]:
        """Every argument key the tool's JSON Schema declares, in schema order."""
        properties = self.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            return []
        return [name for name in properties if isinstance(name, str)]

    @property
    def required_argument_names(self) -> list[str]:
        """The subset of arguments the schema marks required."""
        required = self.input_schema.get("required", [])
        if not isinstance(required, list):
            return []
        return [name for name in required if isinstance(name, str)]

    def property_schema(self, argument_name: str) -> dict:
        """JSON Schema fragment for one argument, or `{}` when there is no such argument."""
        properties = self.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            return {}
        schema = properties.get(argument_name, {})
        return schema if isinstance(schema, dict) else {}

    def description_preview(self, max_chars: int = DESCRIPTION_PREVIEW_CHARS) -> str:
        """First line of the description, truncated to fit one terminal row."""
        description = self.description.strip()
        if not description:
            return ""
        first_line = description.splitlines()[0]
        if len(first_line) > max_chars:
            return first_line[: max_chars - 3] + "..."
        return first_line


@dataclass(frozen=True)
class ToolImage:
    """An `{"type": "image"}` content item from a tool result."""

    mime_type: str
    data_base64: str

    def file_extension(self) -> str:
        """Filename suffix for the declared MIME type.

        Raises:
            StudioMcpError: the tool declared no type, or one this build does
                not recognise. Guessing `.png` here would put a wrong extension
                on a file someone then opens, so an unknown type is fatal. The
                advice names the one invocation that does not need a filename,
                which is `--json` with no `--out`.
        """
        declared = self.mime_type.strip().lower()
        extension = IMAGE_MIME_TYPE_TO_FILE_EXTENSION.get(declared)
        if extension is None:
            known = ", ".join(sorted(IMAGE_MIME_TYPE_TO_FILE_EXTENSION))
            described = declared or "(none declared)"
            raise StudioMcpError(
                f"tool returned image content of unsupported type {described!r}. "
                f"Known types: {known}. Rerun with --json and no --out for the payload "
                "as sent."
            )
        return extension

    def decoded_bytes(self) -> bytes:
        """Raw image bytes, checked against the format the tool claimed.

        Whitespace and line wrapping in the base64 are tolerated (both are legal
        in MIME base64 and some servers wrap at 76 columns).

        Raises:
            StudioMcpError: undecodable base64, an unknown MIME type, or a
                payload whose magic bytes do not match the declared type.
        """
        extension = self.file_extension()
        compact = "".join(self.data_base64.split())
        try:
            data = base64.b64decode(compact, validate=True)
        except (ValueError, TypeError) as decode_error:
            raise StudioMcpError(
                f"tool returned undecodable image data: {decode_error}"
            ) from decode_error

        for offset, signature in IMAGE_SIGNATURES_BY_FILE_EXTENSION[extension]:
            if data[offset : offset + len(signature)] != signature:
                raise StudioMcpError(
                    f"tool declared {self.mime_type!r} but the payload is not a "
                    f"{extension.lstrip('.')} file (signature mismatch); refusing to write it."
                )
        return data


@dataclass(frozen=True)
class ToolCallResult:
    """A flattened `tools/call` result: the text, the images, and the untouched payload."""

    is_error: bool
    text: str
    images: list[ToolImage] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def parse_tool_call_result(result: dict) -> ToolCallResult:
    """Flatten an MCP `tools/call` result into text, images, and the raw payload.

    Handles the three 2024-11-05 content shapes: `text`, `image` (base64 plus
    mimeType), and embedded `resource` (whose `text` member, when present, reads
    as more text). Unknown content types are ignored rather than fatal, so a
    future Roblox content type cannot break an otherwise good call.
    """
    text_parts: list[str] = []
    images: list[ToolImage] = []

    content = result.get("content", [])
    if not isinstance(content, list):
        logger.debug("tool result content was %s, not a list", type(content).__name__)
        content = []

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text_parts.append(str(item.get("text", "")))
        elif item_type == "image":
            images.append(
                ToolImage(
                    mime_type=str(item.get("mimeType", "")),
                    data_base64=str(item.get("data", "")),
                )
            )
        elif item_type == "resource":
            resource = item.get("resource", {})
            if isinstance(resource, dict) and resource.get("text"):
                text_parts.append(str(resource["text"]))
        else:
            logger.debug("ignoring unknown content type %r", item_type)

    return ToolCallResult(
        is_error=read_is_error_flag(result),
        text="\n".join(text_parts),
        images=images,
        raw=result,
    )


def read_is_error_flag(result: dict) -> bool:
    """Whether the tool reported its own failure, from a field the spec says is boolean.

    Only a real `True` is honoured as written. Anything else truthy still counts
    as a failure, because a server that put something in this field is not
    reporting success and `bool("false")` would otherwise have to decide it, but
    it is logged: a build that starts sending `"isError": "false"` is a protocol
    change worth being able to find. Once per process, because that is a fact
    about the build and one attach poll is two dozen calls. Only the TYPE is
    logged, never the value, since logging's last-resort handler prints to
    stderr without sanitising.
    """
    global warned_about_non_boolean_is_error
    flag = result.get("isError", False)
    if isinstance(flag, bool):
        return flag
    if flag:
        if not warned_about_non_boolean_is_error:
            warned_about_non_boolean_is_error = True
            logger.warning(
                "tool result carried a non-boolean isError of type %s; reading it as a failure",
                type(flag).__name__,
            )
        return True
    return False


def build_tool_definitions(entries: list) -> list[ToolDefinition]:
    """Turn raw `tools/list` entries into `ToolDefinition`s, skipping malformed ones.

    A server that sends `{"name": 123}` gets that entry dropped with a log line
    rather than taking the command down with an AttributeError three layers up.
    """
    definitions: list[ToolDefinition] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            logger.debug("skipping malformed tools/list entry: %r", entry)
            continue
        description = entry.get("description", "")
        input_schema = entry.get("inputSchema", {})
        definitions.append(
            ToolDefinition(
                name=entry["name"],
                description=description if isinstance(description, str) else "",
                input_schema=input_schema if isinstance(input_schema, dict) else {},
            )
        )
    return definitions


def method_not_found_reply(message: dict) -> dict | None:
    """The error reply for a request the BRIDGE made of us, or None if it made none.

    MCP is bidirectional. A server may call `roots/list`,
    `sampling/createMessage` or `elicitation/create` on its client, and it
    numbers those requests from 1 exactly the way we number ours, so
    `{"jsonrpc": "2.0", "id": 2, "method": "roots/list"}` can arrive while we
    are waiting on our own id 2. It is a request, not an answer (a JSON-RPC
    response never carries `method`), and leaving it unanswered parks a
    conformant server until its own timeout. This client implements none of
    those methods, so -32601 is the honest reply.

    Returns None when there is nothing to answer: a response, a notification (a
    method with no id), or an id shaped like nothing worth echoing back, which
    is a bool, a float, or a string whose length the server would be choosing
    for our write.
    """
    if not isinstance(message.get("method"), str):
        return None
    answered_id = message.get("id")
    if isinstance(answered_id, bool) or not isinstance(answered_id, (int, str)):
        return None
    if isinstance(answered_id, str) and len(answered_id) > MAX_ECHOED_REQUEST_ID_CHARS:
        return None
    return {
        "jsonrpc": "2.0",
        "id": answered_id,
        "error": {"code": METHOD_NOT_FOUND_CODE, "message": METHOD_NOT_FOUND_MESSAGE},
    }


def raise_for_rpc_error(response: dict) -> None:
    """Raise `StudioMcpProtocolError` when a JSON-RPC response carries an error.

    Tolerates a server that sends a bare string where the spec wants an object,
    which is the difference between a readable message and a traceback. The
    message is chrome around a failure and the server picks its length, so it is
    capped like every other diagnostic field: a 5 MB message was 5 MB of stderr.
    """
    error = response.get("error")
    if error is None:
        return
    if not isinstance(error, dict):
        raise StudioMcpProtocolError(
            code=UNKNOWN_ERROR_CODE, message=display_error_message(error)
        )
    message = error.get("message")
    raise StudioMcpProtocolError(
        # Whatever the server put there: the exception normalises the type and
        # bounds the length, so both rules live in one place.
        code=error.get("code"),
        message=display_error_message(message if message is not None else "unknown error"),
        data=error.get("data"),
    )


def display_error_message(message: object) -> str:
    """Server-chosen failure text, sanitised and capped, ready to print.

    Line breaks survive the cap, because a bridge error is sometimes a short
    stack and the shape of it is part of the diagnosis.
    """
    return truncate_display_text(sanitize_terminal_text(str(message)))
