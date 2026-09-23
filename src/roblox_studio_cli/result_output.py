"""What a tool result becomes on the way out: printed, written, or both.

One module stands between a `ToolCallResult` and the caller's terminal, so the
rules about a call that failed and about `--json` next to `--out` are decided
once rather than in each of the six commands that produce a result. Images go
on to `image_output`, which owns where a file may land and the refusals on the
way there.

The exit code a self-reported tool failure earns is raised from here too, as a
`typer.Exit`: whether the tool failed is a question about the result, and this
is the only module that reads one.
"""

from pathlib import Path
from typing import Optional

import typer

from roblox_studio_cli.errors import EXIT_NOT_READY
from roblox_studio_cli.image_output import save_images
from roblox_studio_cli.json_output import compact_json
from roblox_studio_cli.mcp_payloads import ToolCallResult
from roblox_studio_cli.terminal import echo_server_text


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
