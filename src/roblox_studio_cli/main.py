"""Typer application for `roblox-studio`.

    doctor      full health report: binary, handshake, tools, Studio instances
                (`status` is a hidden alias)
    instances   which Roblox Studio processes the bridge can see
    tools       what Studio currently exposes, with each tool's argument names
    call        generic `tools/call` with a JSON argument blob
    luau        run Luau source (argument, `-` for stdin, or `--file`)
    screenshot  capture the viewport to an image file
    state       Studio's current state
    play        start or stop a play session

Every tool-calling subcommand resolves three things at runtime rather than
hardcoding them: the tool name, the argument keys, and which Studio instance to
target. That lookup lives in `discovery`; this module is the command surface,
the output, and the exit codes.

Exit codes come from one rule, applied in `exit_code_for` and documented in
`errors`: a malformed request exits 2, an environment that is not ready exits 1.

Anything the bridge said reaches the terminal through
`terminal.echo_server_text`, never through a bare `typer.echo`.
"""

import functools
import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer

from roblox_studio_cli.client import (
    DEFAULT_CALL_TOOL_TIMEOUT_SECONDS,
    DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS,
    StudioMcpClient,
)
from roblox_studio_cli.discovery import (
    CAPTURE_ID_ARGUMENT_NAMES,
    LUAU_CODE_ARGUMENT_NAMES,
    LUAU_CONTEXT_ARGUMENT_NAMES,
    LUAU_INTENT,
    PLAY_INTENT,
    PLAY_START_ARGUMENT_NAMES,
    SCREENSHOT_INTENT,
    STUDIO_STATE_INTENT,
    ToolDiscoveryError,
    apply_studio_id,
    attach_failure_message,
    check_required_arguments,
    find_argument_name,
    find_tool,
    parse_arguments_option,
    wait_for_studio_instances,
)
from roblox_studio_cli.doctor_report import gather_doctor_report, render_doctor_report
from roblox_studio_cli.errors import (
    StudioMcpError,
    StudioNotAttachedError,
    StudioNotConnectedError,
    StudioRequestError,
)
from roblox_studio_cli.image_output import save_images
from roblox_studio_cli.mcp_payloads import ToolCallResult, ToolDefinition
from roblox_studio_cli.terminal import echo_server_text, sanitize_single_line

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_REQUEST_ERROR = 2

# `doctor` must outwait the proxy's own ~20s "no tools" timeout, otherwise the
# CLI gives up first and never sees the WARN that explains what is wrong.
DOCTOR_TIMEOUT_SECONDS = 25.0
DEFAULT_LUAU_CONTEXT = "Edit"
STDIN_SOURCE_MARKER = "-"
CAPTURE_ID_RANDOM_CHARS = 12
TOOL_NAME_COLUMN_WIDTH = 26

app = typer.Typer(
    name="roblox-studio",
    help="CLI over Roblox Studio's built-in MCP server, for agents and humans.",
    no_args_is_help=True,
)

STUDIO_OPTION = typer.Option(
    None, "--studio", help="Target Studio instance id or place name (default: the only one open)."
)
ARGS_OPTION = typer.Option(None, "--args", help="Extra tool arguments as a JSON object.")
JSON_OPTION = typer.Option(
    False, "--json", help="Print machine-readable JSON instead of formatted text."
)
CALL_TIMEOUT_OPTION = typer.Option(
    DEFAULT_CALL_TOOL_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for the result."
)
FORCE_OPTION = typer.Option(False, "--force", help="Overwrite the --out file if it exists.")


def exit_code_for(error: StudioMcpError) -> int:
    """The one place an exception class becomes an exit status."""
    return EXIT_REQUEST_ERROR if isinstance(error, StudioRequestError) else EXIT_NOT_READY


def report_error(error: StudioMcpError) -> None:
    """Print a failure on stderr, sanitised. The two "not ready" messages are
    already written as advice, so they skip the `error:` prefix."""
    advice = isinstance(error, (StudioNotConnectedError, StudioNotAttachedError))
    echo_server_text(str(error) if advice else f"error: {error}", err=True)


def handles_studio_errors(command):
    """Map every `StudioMcpError` the command raises onto the exit-code rule.

    This sits on the commands rather than inside `main()`, so a command behaves
    identically however it was invoked: from a shell, from `python -m`, or from
    a test runner that calls the Typer app directly.
    """

    @functools.wraps(command)
    def run_command(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except StudioMcpError as command_error:
            report_error(command_error)
            raise typer.Exit(exit_code_for(command_error)) from command_error

    return run_command


@contextmanager
def studio_client():
    """Open a started client and guarantee the proxy is reaped afterwards.

    Failures propagate to `handles_studio_errors`, which owns the exit code.
    """
    client = StudioMcpClient()
    try:
        client.start()
        yield client
    finally:
        client.close()


def call_discovered_tool(
    client: StudioMcpClient,
    definitions: list[ToolDefinition],
    tool: ToolDefinition,
    arguments: dict,
    studio: Optional[str],
    timeout: float,
) -> ToolCallResult:
    """Fill in `studio_id`, check the schema, call. The tail of every convenience command."""
    apply_studio_id(client, definitions, tool, arguments, studio, timeout)
    check_required_arguments(tool, arguments)
    return client.call_tool(tool.name, arguments, timeout=timeout)


def emit_result(
    result: ToolCallResult,
    tool_name: str,
    as_json: bool,
    out_path: Optional[Path] = None,
    force: bool = False,
) -> None:
    """Print a tool result, save its images, and exit non-zero when the tool failed.

    Images are written in both output modes, because `--out` is an explicit
    request for the file; `--json` only changes what goes to stdout.
    """
    written = save_images(result.images, tool_name, out_path, force)

    if as_json:
        typer.echo(json.dumps(result.raw, indent=2))
    else:
        if result.text:
            echo_server_text(result.text)
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
    """Resolve Luau source from the argument, stdin, or a file, whichever was given.

    Called before the proxy is spawned, so a typo costs nothing but a message.
    """
    if file_path is not None:
        if code:
            raise StudioRequestError("pass Luau source as an argument or with --file, not both")
        try:
            return file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as read_error:
            raise StudioRequestError(f"cannot read {file_path}: {read_error}") from read_error
    if code is None:
        raise StudioRequestError(
            "provide Luau source as an argument, `-` to read stdin, or --file PATH"
        )
    if code == STDIN_SOURCE_MARKER:
        return sys.stdin.read()
    return code


@app.command("doctor")
@app.command("status", hidden=True)
@handles_studio_errors
def doctor(
    timeout: float = typer.Option(
        DOCTOR_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for Studio to expose its tools."
    ),
    as_json: bool = JSON_OPTION,
):
    """Report whether Studio is reachable, and what is missing when it is not.

    Checks in order: the proxy binary, the MCP handshake, whether tools became
    available, and whether a Studio instance attaches (reporting how long that
    took). Exits 0 only when one did, so this doubles as a readiness gate.

    Expect it to take the full timeout when the Studio toggle is off: the proxy
    waits silently before logging its own warning.
    """
    report = gather_doctor_report(timeout)
    render_doctor_report(report, as_json)
    raise typer.Exit(EXIT_OK if report.ok else EXIT_NOT_READY)


@app.command()
@handles_studio_errors
def instances(timeout: float = CALL_TIMEOUT_OPTION, as_json: bool = JSON_OPTION):
    """List the Roblox Studio instances registered with the MCP bridge."""
    with studio_client() as client:
        definitions = client.list_tools()
        outcome = wait_for_studio_instances(client, definitions, timeout=timeout)

    if as_json:
        typer.echo(
            json.dumps(
                [{"id": item.identifier, "name": item.name} for item in outcome.instances],
                indent=2,
            )
        )
    else:
        for item in outcome.instances:
            echo_server_text(item.describe())

    if not outcome.attached:
        echo_server_text(attach_failure_message(outcome), err=True)
        raise typer.Exit(EXIT_NOT_READY)


@app.command()
@handles_studio_errors
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
        # Every field here is server-controlled and printed as a fixed-width row,
        # so fold each one onto a single line BEFORE padding it: a newline inside
        # a tool name would otherwise break the column and forge an extra row.
        name = sanitize_single_line(tool.name)
        echo_server_text(f"{name:<{TOOL_NAME_COLUMN_WIDTH}} "
                         f"{sanitize_single_line(tool.description_preview())}")
        if tool.argument_names:
            required = set(tool.required_argument_names)
            rendered = ", ".join(
                f"{name}*" if name in required else name for name in tool.argument_names
            )
            echo_server_text(
                f"{'':<{TOOL_NAME_COLUMN_WIDTH}} args: {sanitize_single_line(rendered)}"
            )
    typer.echo(f"\n{len(definitions)} tools (* = required)")


@app.command()
@handles_studio_errors
def call(
    tool: str = typer.Argument(..., help="Tool name exactly as `roblox-studio tools` reports it."),
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    out: Optional[Path] = typer.Option(
        None, "--out", help="Where to write returned image content."
    ),
    force: bool = FORCE_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Call any tool by name. The escape hatch when no convenience command fits."""
    arguments = parse_arguments_option(args)
    with studio_client() as client:
        definitions = client.list_tools()
        definition = next((entry for entry in definitions if entry.name == tool), None)
        if definition is None:
            available = ", ".join(sorted(entry.name for entry in definitions)) or "(none)"
            raise ToolDiscoveryError(f"no tool named {tool!r}. Available tools: {available}")
        result = call_discovered_tool(client, definitions, definition, arguments, studio, timeout)

    emit_result(result, tool, as_json, out, force)


@app.command()
@handles_studio_errors
def luau(
    code: Optional[str] = typer.Argument(None, help="Luau source, or `-` to read it from stdin."),
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
    extra_arguments = parse_arguments_option(args)
    source = read_luau_source(code, file)

    with studio_client() as client:
        definitions = client.list_tools()
        match = find_tool(definitions, LUAU_INTENT)

        code_argument = find_argument_name(
            match.tool, LUAU_CODE_ARGUMENT_NAMES, match.matched_by_exact_name
        )
        if code_argument is None:
            raise ToolDiscoveryError(
                f"tool {match.tool.name!r} has no argument that looks like Luau source "
                f"(declared: {', '.join(match.tool.argument_names) or 'none'})"
            )
        arguments: dict = {code_argument: source}

        context_argument = find_argument_name(
            match.tool, LUAU_CONTEXT_ARGUMENT_NAMES, fall_back_to_required=False
        )
        if context_argument is not None:
            arguments[context_argument] = context
        elif context != DEFAULT_LUAU_CONTEXT:
            typer.echo("warning: this build's Luau tool takes no execution-context "
                       "argument; ignoring --context", err=True)

        # Explicit --args wins, so a caller can always supply what discovery missed.
        arguments.update(extra_arguments)
        result = call_discovered_tool(client, definitions, match.tool, arguments, studio, timeout)

    emit_result(result, match.tool.name, as_json)


@app.command()
@handles_studio_errors
def screenshot(
    out: Optional[Path] = typer.Option(
        None, "--out", help="Where to write the capture (default: a temp file)."
    ),
    force: bool = FORCE_OPTION,
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Capture the Studio viewport and write it to a file.

    Camera placement is a per-build extra rather than a flag here; pass it
    through, for example `--args '{"camera_position": [0, 20, 40]}'`.
    """
    arguments = parse_arguments_option(args)
    with studio_client() as client:
        definitions = client.list_tools()
        match = find_tool(definitions, SCREENSHOT_INTENT)
        # Studio requires a caller-supplied capture id that nobody could guess,
        # so generate one rather than making every invocation pass --args.
        capture_id_argument = find_argument_name(
            match.tool, CAPTURE_ID_ARGUMENT_NAMES, fall_back_to_required=False
        )
        if capture_id_argument is not None:
            arguments.setdefault(capture_id_argument, generate_capture_id())
        result = call_discovered_tool(client, definitions, match.tool, arguments, studio, timeout)

    emit_result(result, match.tool.name, as_json, out, force)


@app.command()
@handles_studio_errors
def state(
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Report Studio's current state (mode, available data models, focused data model)."""
    arguments = parse_arguments_option(args)
    with studio_client() as client:
        definitions = client.list_tools()
        match = find_tool(definitions, STUDIO_STATE_INTENT)
        result = call_discovered_tool(client, definitions, match.tool, arguments, studio, timeout)

    emit_result(result, match.tool.name, as_json)


@app.command()
@handles_studio_errors
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
        raise typer.Exit(EXIT_REQUEST_ERROR)

    arguments = parse_arguments_option(args)
    with studio_client() as client:
        definitions = client.list_tools()
        match = find_tool(definitions, PLAY_INTENT)
        start_argument = find_argument_name(
            match.tool, PLAY_START_ARGUMENT_NAMES, fall_back_to_required=False
        )
        if start_argument is None:
            raise ToolDiscoveryError(
                f"tool {match.tool.name!r} has no start/stop argument "
                f"(declared: {', '.join(match.tool.argument_names) or 'none'})"
            )
        arguments.setdefault(start_argument, start)
        result = call_discovered_tool(client, definitions, match.tool, arguments, studio, timeout)

    emit_result(result, match.tool.name, as_json)


def main():
    """Entry point: run the CLI with a catch-all so users never see a raw traceback."""
    try:
        app()
    except StudioMcpError as client_error:
        report_error(client_error)
        raise SystemExit(exit_code_for(client_error))


if __name__ == "__main__":
    main()
