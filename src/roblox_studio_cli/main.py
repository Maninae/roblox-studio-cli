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
import math
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
from roblox_studio_cli.display_wake import DISPLAY_ASLEEP_HINT, display_kept_awake
from roblox_studio_cli.doctor_report import gather_doctor_report, render_doctor_report
from roblox_studio_cli.errors import (
    StudioMcpError,
    StudioMcpTimeoutError,
    StudioNotAttachedError,
    StudioNotConnectedError,
    StudioRequestError,
)
from roblox_studio_cli.image_output import save_images
from roblox_studio_cli.json_output import compact_json
from roblox_studio_cli.luau_source import read_luau_source
from roblox_studio_cli.mcp_payloads import ToolCallResult, ToolDefinition
from roblox_studio_cli.terminal import (
    capped_display_names,
    echo_server_text,
    sanitize_diagnostic_line,
    sanitize_single_line,
)

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_REQUEST_ERROR = 2

# `doctor` must outwait the proxy's own ~20s "no tools" timeout, otherwise the
# CLI gives up first and never sees the WARN that explains what is wrong.
DOCTOR_TIMEOUT_SECONDS = 25.0
DEFAULT_LUAU_CONTEXT = "Edit"
CAPTURE_ID_RANDOM_CHARS = 12
TOOL_NAME_COLUMN_WIDTH = 26

# Typer's rich traceback renders the frames of an unexpected exception, and with
# locals enabled it prints the variables in them: on this CLI those hold whole
# server-controlled frames. Both are off, and `main` catches what gets that far.
app = typer.Typer(
    name="roblox-studio",
    help="CLI over Roblox Studio's built-in MCP server, for agents and humans.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)

STUDIO_OPTION = typer.Option(
    None, "--studio", help="Target Studio instance id or place name (default: the only one open)."
)
ARGS_OPTION = typer.Option(None, "--args", help="Extra tool arguments as a JSON object.")
JSON_OPTION = typer.Option(
    False, "--json", help="Print machine-readable JSON instead of formatted text."
)


def validate_timeout_seconds(value: float) -> float:
    """Reject a `--timeout` that is not a real, forward-going number of seconds.

    Click parses "nan" and "inf" happily, and both poison every deadline built
    from them: every comparison against NaN is False, so the read loop neither
    times out nor proceeds.

    Zero and negative values parse too, and they are worse, because they look
    like an answer. A deadline already in the past makes the first `select()`
    return nothing, so `luau --timeout 0` reported "the proxy stopped reading
    its input" and `doctor --timeout -1` printed NOT CONNECTED, both against a
    bridge that was working. The caller's number is the bug, so it exits 2.
    """
    if not math.isfinite(value):
        raise typer.BadParameter("--timeout must be a finite number of seconds")
    if value <= 0:
        raise typer.BadParameter("--timeout must be a positive number of seconds")
    return value


# `--out` is a path to WRITE. Typer hands a `Path` parameter to `click.Path`,
# whose `readable` defaults to True and is checked whenever the path exists, so
# `--out` at mode 0o000 was refused as "is not readable" by a CLI that was never
# going to read it. Turning it off lets `image_output` answer instead, which is
# where every other refusal about this path is decided and worded.
OUT_PATH_IS_NOT_READ = False

CALL_TIMEOUT_OPTION = typer.Option(
    DEFAULT_CALL_TOOL_TIMEOUT_SECONDS,
    "--timeout",
    help="Seconds to wait for the result.",
    callback=validate_timeout_seconds,
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

    The failure check comes FIRST, and a failed call writes nothing: a tool that
    reports `isError` can still attach content, and writing it handed the caller
    a file for a capture that never happened, having spent the single `--force`
    they granted on overwriting the good copy that was already there.

    `--out` is the request for a file, so it is honoured in both output modes;
    `--json` only changes what goes to stdout, and a file whose contents do not
    match the extension the caller asked for is reported on stderr in both, and
    never renamed. `--json` with NO `--out` writes nothing at all: the payload
    is in the JSON, and the temp file the CLI used to write went into every
    such call's output as a path the JSON did not mention.
    """
    if result.is_error:
        emit_failed_result(result, as_json)
        raise typer.Exit(EXIT_NOT_READY)

    if as_json and out_path is None:
        typer.echo(compact_json(result.raw))
        return

    written = save_images(result.images, tool_name, out_path, force)
    for image in written:
        if image.warning:
            echo_server_text(image.warning, err=True)

    if as_json:
        typer.echo(compact_json(result.raw))
    else:
        if result.text:
            echo_server_text(result.text)
        for image in written:
            typer.echo(str(image.path))
        if not result.text and not written:
            typer.echo("(tool returned no content)")


def emit_failed_result(result: ToolCallResult, as_json: bool) -> None:
    """Print what a failing tool said, and say so when that cost the caller a file.

    Nothing has been written by the time this runs, so the second line is the
    only thing standing between the caller and a `--out` path they believe holds
    a fresh capture.
    """
    if as_json:
        typer.echo(compact_json(result.raw))
    else:
        echo_server_text(result.text or "(the tool reported a failure with no message)")
    if result.images:
        typer.echo(
            f"error: the tool reported a failure, so the {len(result.images)} image(s) "
            "it returned were not written",
            err=True,
        )


def declared_arguments(tool: ToolDefinition) -> str:
    """The argument names a tool declares, for a message that says it lacks one.

    Server-chosen names, so the list is capped the way every other enumeration
    of them is.
    """
    return ", ".join(capped_display_names(tool.argument_names)) or "none"


def generate_capture_id() -> str:
    """A fresh id for one screen capture, unique enough for back-to-back calls."""
    return f"roblox-studio-{uuid.uuid4().hex[:CAPTURE_ID_RANDOM_CHARS]}"


@app.command("doctor")
@app.command("status", hidden=True)
@handles_studio_errors
def doctor(
    timeout: float = typer.Option(
        DOCTOR_TIMEOUT_SECONDS,
        "--timeout",
        help="Seconds to wait for Studio to expose its tools.",
        callback=validate_timeout_seconds,
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
            compact_json([{"id": item.identifier, "name": item.name} for item in outcome.instances])
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
        DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS,
        "--timeout",
        help="Seconds to wait for tools/list.",
        callback=validate_timeout_seconds,
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
        typer.echo(compact_json(payload))
        return

    if not definitions:
        typer.echo("Studio is connected but exposes no tools.")
        return

    for tool in definitions:
        # Every field here is server-controlled and printed as a fixed-width row,
        # so each is folded onto one line BEFORE padding (a newline inside a tool
        # name would break the column and forge an extra row) and capped the way
        # every other row and enumeration is: 100 KB of clean name scrolls the
        # listing away without a single control character in it.
        name = sanitize_diagnostic_line(tool.name)
        echo_server_text(f"{name:<{TOOL_NAME_COLUMN_WIDTH}} "
                         f"{sanitize_single_line(tool.description_preview())}")
        if tool.argument_names:
            required = set(tool.required_argument_names)
            marked = [
                f"{name}*" if name in required else name for name in tool.argument_names
            ]
            rendered = ", ".join(capped_display_names(marked))
            echo_server_text(f"{'':<{TOOL_NAME_COLUMN_WIDTH}} args: {rendered}")
    typer.echo(f"\n{len(definitions)} tools (* = required)")


@app.command()
@handles_studio_errors
def call(
    tool: str = typer.Argument(..., help="Tool name exactly as `roblox-studio tools` reports it."),
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    out: Optional[Path] = typer.Option(
        None, "--out", readable=OUT_PATH_IS_NOT_READ, help="Where to write returned image content."
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
            # Server-chosen names, so the list is capped: the point of the
            # message is the name the caller typed, not the catalogue.
            names = capped_display_names(sorted(entry.name for entry in definitions))
            available = ", ".join(names) or "(none)"
            raise ToolDiscoveryError(f"no tool named {tool!r}. Available tools: {available}")
        result = call_discovered_tool(client, definitions, definition, arguments, studio, timeout)

    emit_result(result, tool, as_json, out, force)


# Unknown options reach the `code` argument instead of failing the parse, so
# that source starting with a dash (`-- a comment`) is usable without `--`.
# `luau_source.refuse_option_lookalike` is the other half: it catches the
# mistyped flag that this setting would otherwise send to Studio as a script.
@app.command(context_settings={"ignore_unknown_options": True})
@handles_studio_errors
def luau(
    code: Optional[str] = typer.Argument(
        None, help="Luau source, `-` to read it from stdin, or `--` first if it starts with a dash."
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
    """Run Luau inside Studio and print what it returns.

    Source that starts with a dash goes after `--`, since everything before it
    is read as flags: `roblox-studio luau -- '-- a comment'`. A heredoc through
    `-`, or `--file`, needs no such thing.
    """
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
                f"tool {sanitize_single_line(match.tool.name)!r} has no argument that looks "
                f"like Luau source (declared: {declared_arguments(match.tool)})"
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
        None, "--out", readable=OUT_PATH_IS_NOT_READ, help="Where to write the capture (default: a temp file)."
    ),
    force: bool = FORCE_OPTION,
    wake_display: bool = typer.Option(
        False, "--wake-display", help="Wake the Mac's display first (macOS; a dark display "
        "captures nothing)."
    ),
    args: Optional[str] = ARGS_OPTION,
    studio: Optional[str] = STUDIO_OPTION,
    timeout: float = CALL_TIMEOUT_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Capture the Studio viewport and write it to a file.

    Camera placement is a per-build extra rather than a flag here; pass it
    through, for example `--args '{"camera_position": [0, 20, 40]}'`.

    With the Mac's display asleep, Studio accepts the call and never answers, so
    a timeout here is quite likely to be that rather than a broken bridge.
    `--wake-display` rules it out, and a timeout without the flag says so.
    """
    arguments = parse_arguments_option(args)
    try:
        with display_kept_awake(wake_display, timeout) as warning:
            if warning:
                typer.echo(warning, err=True)
            with studio_client() as client:
                definitions = client.list_tools()
                match = find_tool(definitions, SCREENSHOT_INTENT)
                # Studio requires a caller-supplied capture id that nobody could
                # guess, so generate one rather than making every invocation
                # pass --args.
                capture_id_argument = find_argument_name(
                    match.tool, CAPTURE_ID_ARGUMENT_NAMES, fall_back_to_required=False
                )
                if capture_id_argument is not None:
                    arguments.setdefault(capture_id_argument, generate_capture_id())
                result = call_discovered_tool(
                    client, definitions, match.tool, arguments, studio, timeout
                )
    except StudioMcpTimeoutError as timeout_error:
        if wake_display:
            raise
        raise StudioMcpTimeoutError(f"{timeout_error}\n{DISPLAY_ASLEEP_HINT}") from timeout_error

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
                f"tool {sanitize_single_line(match.tool.name)!r} has no start/stop argument "
                f"(declared: {declared_arguments(match.tool)})"
            )
        arguments.setdefault(start_argument, start)
        result = call_discovered_tool(client, definitions, match.tool, arguments, studio, timeout)

    emit_result(result, match.tool.name, as_json)


def main():
    """Entry point: run the CLI with a catch-all so users never see a raw traceback.

    The second clause is the one that matters for anything the bridge sent. A
    frame this build cannot read should surface as one line, not as a traceback
    quoting the bytes that caused it; `SystemExit` and `typer.Exit` are not
    `Exception` subclasses, so an ordinary exit still passes straight through.
    """
    try:
        app()
    except StudioMcpError as client_error:
        report_error(client_error)
        raise SystemExit(exit_code_for(client_error))
    except Exception as unexpected_error:
        echo_server_text(
            f"error: {type(unexpected_error).__name__}: {unexpected_error}", err=True
        )
        raise SystemExit(EXIT_NOT_READY)


if __name__ == "__main__":
    main()
