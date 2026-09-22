"""Typer application for `roblox-studio`.

    doctor      full health report: binary, handshake, tools, Studio instances
                (`status` is a hidden alias)
    instances   which Roblox Studio processes the bridge can see
    tools       what Studio currently exposes, with each tool's argument names
    call        generic `tools/call` with a JSON argument blob
    luau        run Luau source (argument, `-` for stdin, or `--file`)
    screenshot  capture the viewport to a PNG
    state       Studio's current state
    play        start or stop a play session

Every tool-calling subcommand resolves three things at runtime rather than
hardcoding them: the tool name, the argument keys, and which Studio instance to
target. Tool and argument lookup live in `discovery`; this module is the CLI
surface and the output formatting.
"""

import json
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer

from roblox_studio_cli.client import (
    DEFAULT_CALL_TOOL_TIMEOUT_SECONDS,
    DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS,
    STUDIO_MCP_BINARY_ENV_VAR,
    STUDIO_NOT_ENABLED_MESSAGE,
    StudioMcpClient,
    StudioMcpError,
    StudioNotConnectedError,
    ToolCallResult,
    ToolDefinition,
    ToolImage,
    resolve_studio_binary_path,
)
from roblox_studio_cli.discovery import (
    CAPTURE_ID_ARGUMENT_NAMES,
    LUAU_CODE_ARGUMENT_NAMES,
    LUAU_CONTEXT_ARGUMENT_NAMES,
    LUAU_TOOL_NAME_KEYWORDS,
    NO_STUDIO_INSTANCE_MESSAGE,
    PLAY_START_ARGUMENT_NAMES,
    PLAY_TOOL_NAME_KEYWORDS,
    SCREENSHOT_TOOL_NAME_KEYWORDS,
    STUDIO_STATE_TOOL_NAME_KEYWORDS,
    ToolDiscoveryError,
    apply_studio_id,
    check_required_arguments,
    find_argument_name,
    find_tool_by_keywords,
    list_studio_instances,
    parse_arguments_option,
)

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_USAGE_ERROR = 2

# `doctor` must outwait the proxy's own ~20s "no tools" timeout, otherwise the
# CLI gives up first and never sees the WARN that explains what is wrong.
DOCTOR_TIMEOUT_SECONDS = 25.0
DEFAULT_LUAU_CONTEXT = "Edit"
STDIN_SOURCE_MARKER = "-"
CAPTURE_ID_RANDOM_CHARS = 12

TOOL_NAME_COLUMN_WIDTH = 26
DOCTOR_LABEL_COLUMN_WIDTH = 11
DESCRIPTION_PREVIEW_CHARS = 100

app = typer.Typer(
    name="roblox-studio",
    help="CLI over Roblox Studio's built-in MCP server, for agents and humans.",
    no_args_is_help=True,
)

STUDIO_OPTION = typer.Option(
    None, "--studio", help="Target Studio instance id or place name (default: the only one open)."
)
ARGS_OPTION = typer.Option(None, "--args", help="Extra tool arguments as a JSON object.")
JSON_OPTION = typer.Option(False, "--json", help="Print the raw result as JSON.")
CALL_TIMEOUT_OPTION = typer.Option(
    DEFAULT_CALL_TOOL_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for the result."
)


@contextmanager
def studio_client():
    """Open a started client, translating transport failures into CLI exits.

    Exceptions raised inside the `with` body land here too, so every command
    gets the same exit-code mapping: 1 when Studio is not connected, 2 for a
    discovery, protocol or transport failure.
    """
    client = StudioMcpClient()
    try:
        client.start()
        yield client
    except StudioNotConnectedError as not_connected:
        typer.echo(str(not_connected), err=True)
        raise typer.Exit(EXIT_NOT_READY)
    except StudioMcpError as client_error:
        typer.echo(f"error: {client_error}", err=True)
        raise typer.Exit(EXIT_USAGE_ERROR)
    finally:
        client.close()


def build_image_path(tool_name: str, image: ToolImage, out_path: Optional[Path]) -> Path:
    """Where one returned image should be written, honouring `--out` when given."""
    if out_path is not None:
        return out_path
    timestamp = int(time.time())
    return Path(tempfile.gettempdir()) / f"studio_{tool_name}_{timestamp}{image.file_extension()}"


def save_images(images: list[ToolImage], tool_name: str, out_path: Optional[Path]) -> list[Path]:
    """Write every image content item to disk and return the paths written.

    A tool returning several images gets `-2`, `-3` suffixes so the extra frames
    cannot silently overwrite the first one.
    """
    written: list[Path] = []
    for index, image in enumerate(images):
        path = build_image_path(tool_name, image, out_path)
        if index:
            path = path.with_name(f"{path.stem}-{index + 1}{path.suffix}")
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image.decoded_bytes())
        written.append(path)
    return written


def emit_result(
    result: ToolCallResult, tool_name: str, as_json: bool, out_path: Optional[Path] = None
) -> None:
    """Print a tool result, save its images, and exit non-zero when the tool failed.

    Images are written in both output modes, because `--out` is an explicit
    request for the file; `--json` only changes what goes to stdout.
    """
    written = save_images(result.images, tool_name, out_path)

    if as_json:
        typer.echo(json.dumps(result.raw, indent=2))
    else:
        if result.text:
            typer.echo(result.text)
        for path in written:
            typer.echo(str(path))
        if not result.text and not written:
            typer.echo("(tool returned no content)")

    if result.is_error:
        raise typer.Exit(EXIT_NOT_READY)


def generate_capture_id() -> str:
    """A fresh id for one screen capture, unique enough for back-to-back calls."""
    return f"roblox-studio-{uuid.uuid4().hex[:CAPTURE_ID_RANDOM_CHARS]}"


def read_luau_source(code: Optional[str], file_path: Optional[Path]) -> str:
    """Resolve Luau source from the argument, stdin, or a file, whichever was given."""
    if file_path is not None:
        if code:
            raise ToolDiscoveryError("pass Luau source as an argument or with --file, not both")
        if not file_path.is_file():
            raise ToolDiscoveryError(f"no such Luau file: {file_path}")
        return file_path.read_text(encoding="utf-8")
    if code is None:
        raise ToolDiscoveryError(
            "provide Luau source as an argument, `-` to read stdin, or --file PATH"
        )
    if code == STDIN_SOURCE_MARKER:
        return sys.stdin.read()
    return code


@app.command("doctor")
@app.command("status", hidden=True)
def doctor(
    timeout: float = typer.Option(
        DOCTOR_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for Studio to expose its tools."
    ),
    as_json: bool = JSON_OPTION,
):
    """Report whether Studio is reachable, and what is missing when it is not.

    Checks in order: the proxy binary, the MCP handshake, whether tools became
    available, and which Studio instances are registered. Exits 0 only when at
    least one instance is registered, so this doubles as a readiness gate.

    Expect it to take the full timeout when the Studio toggle is off: the proxy
    waits silently before logging its own warning.
    """
    report: dict = {
        "binary": resolve_studio_binary_path(),
        "binary_exists": Path(resolve_studio_binary_path()).exists(),
        "server_info": None,
        "tools_available": False,
        "tool_count": 0,
        "instances": [],
        "problem": None,
        "ok": False,
    }

    if not report["binary_exists"]:
        report["problem"] = (
            f"Studio MCP binary not found at {report['binary']}. It ships inside "
            f"RobloxStudio.app, so a Studio update can move it; set {STUDIO_MCP_BINARY_ENV_VAR} "
            "to its current path."
        )
        emit_doctor_report(report, as_json)
        raise typer.Exit(EXIT_NOT_READY)

    client = StudioMcpClient()
    try:
        handshake = client.start()
        report["server_info"] = handshake.get("serverInfo", {})
        tool_definitions = client.list_tools(timeout=timeout)
        report["tools_available"] = True
        report["tool_count"] = len(tool_definitions)
        instances = list_studio_instances(client, tool_definitions)
        report["instances"] = [
            {"id": instance.identifier, "name": instance.name} for instance in instances
        ]
        report["ok"] = bool(instances)
        if not instances:
            report["problem"] = NO_STUDIO_INSTANCE_MESSAGE
    except StudioNotConnectedError:
        report["problem"] = STUDIO_NOT_ENABLED_MESSAGE
    except StudioMcpError as client_error:
        report["problem"] = str(client_error)
    finally:
        client.close()

    emit_doctor_report(report, as_json)
    raise typer.Exit(EXIT_OK if report["ok"] else EXIT_NOT_READY)


def emit_doctor_report(report: dict, as_json: bool) -> None:
    """Render the doctor report as aligned text, or as the raw dict under `--json`."""
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return

    label_width = DOCTOR_LABEL_COLUMN_WIDTH
    found = "found" if report["binary_exists"] else "MISSING"
    typer.echo(f"{'Binary:':<{label_width}}{report['binary']} ({found})")

    server_info = report["server_info"]
    if server_info:
        name = server_info.get("name", "unknown")
        version = server_info.get("version", "?")
        typer.echo(f"{'Handshake:':<{label_width}}{name} {version}")
    else:
        typer.echo(f"{'Handshake:':<{label_width}}no response")

    if report["tools_available"]:
        typer.echo(f"{'Tools:':<{label_width}}{report['tool_count']} available")
    else:
        typer.echo(f"{'Tools:':<{label_width}}not available")

    instances = report["instances"]
    if instances:
        first = instances[0]
        typer.echo(f"{'Instances:':<{label_width}}{describe_instance_entry(first)}")
        for entry in instances[1:]:
            typer.echo(f"{'':<{label_width}}{describe_instance_entry(entry)}")
    else:
        typer.echo(f"{'Instances:':<{label_width}}none registered")

    # The verdict goes to stdout because it is the answer; the remediation goes
    # to stderr so a script can read the verdict without parsing advice. NOT
    # READY and NOT CONNECTED are different faults with different fixes: the
    # bridge answering with zero instances means open a place, while no tools at
    # all means the Studio toggle is off.
    typer.echo(f"\n{doctor_verdict(report)}")
    if report["problem"]:
        typer.echo(report["problem"], err=True)


def doctor_verdict(report: dict) -> str:
    """One-line summary of the report, naming which half of the bridge is missing."""
    if report["ok"]:
        instance_count = len(report["instances"])
        plural = "" if instance_count == 1 else "s"
        return f"CONNECTED ({report['tool_count']} tools, {instance_count} instance{plural})"
    if report["tools_available"]:
        return "NOT READY (bridge is up, no Studio instance registered)"
    return "NOT CONNECTED"


def describe_instance_entry(entry: dict) -> str:
    """`id (name)` for one instance dict, or the bare id when it has no name."""
    return f"{entry['id']} ({entry['name']})" if entry.get("name") else entry["id"]


@app.command()
def instances(as_json: bool = JSON_OPTION):
    """List the Roblox Studio instances registered with the MCP bridge."""
    with studio_client() as client:
        tool_definitions = client.list_tools()
        found = list_studio_instances(client, tool_definitions)

    if as_json:
        typer.echo(
            json.dumps([{"id": item.identifier, "name": item.name} for item in found], indent=2)
        )
    else:
        for item in found:
            typer.echo(item.describe())

    if not found:
        typer.echo(NO_STUDIO_INSTANCE_MESSAGE, err=True)
        raise typer.Exit(EXIT_USAGE_ERROR)


@app.command()
def tools(
    timeout: float = typer.Option(
        DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for tools/list."
    ),
    as_json: bool = JSON_OPTION,
):
    """List the tools Studio exposes, with their argument names."""
    with studio_client() as client:
        definitions = client.list_tools(timeout=timeout)

    if as_json:
        payload = [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
            for tool in definitions
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    if not definitions:
        typer.echo("Studio is connected but exposes no tools.")
        return

    for tool in definitions:
        typer.echo(f"{tool.name:<{TOOL_NAME_COLUMN_WIDTH}} {summarize_description(tool)}")
        if tool.argument_names:
            required = set(tool.required_argument_names)
            rendered = ", ".join(
                f"{name}*" if name in required else name for name in tool.argument_names
            )
            typer.echo(f"{'':<{TOOL_NAME_COLUMN_WIDTH}} args: {rendered}")
    typer.echo(f"\n{len(definitions)} tools (* = required)")


def summarize_description(tool: ToolDefinition) -> str:
    """First line of a tool's description, truncated to fit one terminal row."""
    description = tool.description.strip()
    if not description:
        return ""
    first_line = description.splitlines()[0]
    if len(first_line) > DESCRIPTION_PREVIEW_CHARS:
        return first_line[: DESCRIPTION_PREVIEW_CHARS - 3] + "..."
    return first_line


@app.command()
def call(
    tool: str = typer.Argument(..., help="Tool name exactly as `roblox-studio tools` reports it."),
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    out: Optional[Path] = typer.Option(
        None, "--out", help="Where to write returned image content."
    ),
    as_json: bool = JSON_OPTION,
):
    """Call any tool by name. The escape hatch when no convenience command fits."""
    with studio_client() as client:
        arguments = parse_arguments_option(args)
        definitions = client.list_tools()
        definition = next((entry for entry in definitions if entry.name == tool), None)
        if definition is None:
            available = ", ".join(entry.name for entry in definitions) or "(none)"
            raise ToolDiscoveryError(f"no tool named {tool!r}. Available tools: {available}")
        apply_studio_id(client, definitions, definition, arguments, studio)
        check_required_arguments(definition, arguments)
        result = client.call_tool(tool, arguments, timeout=timeout)

    emit_result(result, tool, as_json, out)


@app.command()
def luau(
    code: Optional[str] = typer.Argument(
        None, help="Luau source, or `-` to read it from stdin."
    ),
    file: Optional[Path] = typer.Option(None, "--file", help="Read Luau source from this file."),
    context: str = typer.Option(
        DEFAULT_LUAU_CONTEXT, "--context", help="Where to run it: Edit, Client or Server."
    ),
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Run Luau inside Studio and print what it returns."""
    with studio_client() as client:
        extra_arguments = parse_arguments_option(args)
        source = read_luau_source(code, file)
        definitions = client.list_tools()
        definition = find_tool_by_keywords(definitions, LUAU_TOOL_NAME_KEYWORDS, "Luau execution")

        code_argument = find_argument_name(
            definition, LUAU_CODE_ARGUMENT_NAMES, fall_back_to_required=True
        )
        if code_argument is None:
            raise ToolDiscoveryError(
                f"tool {definition.name!r} has no argument that looks like Luau source "
                f"(declared: {', '.join(definition.argument_names) or 'none'})"
            )
        arguments: dict = {code_argument: source}

        context_argument = find_argument_name(
            definition, LUAU_CONTEXT_ARGUMENT_NAMES, fall_back_to_required=False
        )
        if context_argument is not None:
            arguments[context_argument] = context
        elif context != DEFAULT_LUAU_CONTEXT:
            typer.echo(
                f"warning: {definition.name!r} takes no execution-context argument; "
                "ignoring --context",
                err=True,
            )

        # Explicit --args wins, so a caller can always supply what discovery missed.
        arguments.update(extra_arguments)
        apply_studio_id(client, definitions, definition, arguments, studio)
        check_required_arguments(definition, arguments)
        result = client.call_tool(definition.name, arguments, timeout=timeout)

    emit_result(result, definition.name, as_json)


@app.command()
def screenshot(
    out: Optional[Path] = typer.Option(
        None, "--out", help="Where to write the capture (default: temp dir)."
    ),
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Capture the Studio viewport and write it to a file.

    Camera placement is a per-build extra rather than a flag here; pass it
    through, for example `--args '{"camera_position": [0, 20, 40]}'`.
    """
    with studio_client() as client:
        arguments = parse_arguments_option(args)
        definitions = client.list_tools()
        definition = find_tool_by_keywords(
            definitions, SCREENSHOT_TOOL_NAME_KEYWORDS, "screen capture"
        )
        # Studio requires a caller-supplied capture id that nobody could guess,
        # so generate one rather than making every invocation pass --args.
        capture_id_argument = find_argument_name(
            definition, CAPTURE_ID_ARGUMENT_NAMES, fall_back_to_required=False
        )
        if capture_id_argument is not None:
            arguments.setdefault(capture_id_argument, generate_capture_id())
        apply_studio_id(client, definitions, definition, arguments, studio)
        check_required_arguments(definition, arguments)
        result = client.call_tool(definition.name, arguments, timeout=timeout)

    emit_result(result, definition.name, as_json, out)


@app.command()
def state(
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Report Studio's current state (open place, play mode, selection)."""
    with studio_client() as client:
        arguments = parse_arguments_option(args)
        definitions = client.list_tools()
        definition = find_tool_by_keywords(
            definitions, STUDIO_STATE_TOOL_NAME_KEYWORDS, "Studio state"
        )
        apply_studio_id(client, definitions, definition, arguments, studio)
        check_required_arguments(definition, arguments)
        result = client.call_tool(definition.name, arguments, timeout=timeout)

    emit_result(result, definition.name, as_json)


@app.command()
def play(
    start: bool = typer.Option(False, "--start", help="Enter play mode."),
    stop: bool = typer.Option(False, "--stop", help="Leave play mode."),
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Start or stop a play session in Studio."""
    if start == stop:
        typer.echo("error: pass exactly one of --start or --stop", err=True)
        raise typer.Exit(EXIT_USAGE_ERROR)

    with studio_client() as client:
        arguments = parse_arguments_option(args)
        definitions = client.list_tools()
        definition = find_tool_by_keywords(definitions, PLAY_TOOL_NAME_KEYWORDS, "play control")

        start_argument = find_argument_name(
            definition, PLAY_START_ARGUMENT_NAMES, fall_back_to_required=False
        )
        if start_argument is None:
            raise ToolDiscoveryError(
                f"tool {definition.name!r} has no start/stop argument "
                f"(declared: {', '.join(definition.argument_names) or 'none'})"
            )
        arguments.setdefault(start_argument, start)
        apply_studio_id(client, definitions, definition, arguments, studio)
        check_required_arguments(definition, arguments)
        result = client.call_tool(definition.name, arguments, timeout=timeout)

    emit_result(result, definition.name, as_json)


def main():
    """Entry point: run the CLI with a catch-all so users never see a raw traceback."""
    try:
        app()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(EXIT_OK)
    except StudioMcpError as client_error:
        typer.echo(f"error: {client_error}", err=True)
        raise SystemExit(EXIT_USAGE_ERROR)


if __name__ == "__main__":
    main()
