"""Where the Luau source for one `luau` call comes from, and what it must not be.

Three ways in, all resolved before the proxy is spawned, so a typo costs a
message rather than a handshake: the source as an argument, `-` to read stdin,
or `--file PATH`.

Both of the ways that read something else's bytes are bounded, and bounded the
SAME way, because they end up in the same place: one JSON-RPC request down a
pipe that holds about 64 KB, to a Studio text box that is not where a
multi-megabyte script belongs. A `--file` was capped at `MAX_LUAU_SOURCE_BYTES`
while `-` was not, which is the same script arriving by a different door.

`--file` also has to be an ordinary file. A FIFO or a device passes every
`exists()` check and then blocks the read forever.

Split out of `main` so the command surface stays about flags, output and exit
codes: this module answers one question (what source did the caller mean?) and
knows nothing about Typer or about the bridge.
"""

import os
import stat
import sys
from pathlib import Path

from roblox_studio_cli.errors import StudioRequestError

STDIN_SOURCE_MARKER = "-"
# Studio's Luau box is not where a multi-megabyte script belongs, and the write
# to the proxy is bounded too, so say no here where the message can be useful.
MAX_LUAU_SOURCE_BYTES = 8 * 2**20


def read_luau_source(code: str | None, file_path: Path | None) -> str:
    """Resolve Luau source from the argument, stdin, or a file, whichever was given.

    Called before the proxy is spawned, so a typo costs nothing but a message.

    Raises:
        StudioRequestError: no source at all, both an argument and a `--file`,
            or a file (or a stdin stream) this command will not read.
    """
    if file_path is not None:
        if code:
            raise StudioRequestError("pass Luau source as an argument or with --file, not both")
        check_luau_file_is_readable_and_bounded(file_path)
        try:
            return file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as read_error:
            raise StudioRequestError(f"cannot read {file_path}: {read_error}") from read_error
    if code is None:
        raise StudioRequestError(
            "provide Luau source as an argument, `-` to read stdin, or --file PATH"
        )
    if code == STDIN_SOURCE_MARKER:
        return read_bounded_stdin()
    return code


def read_bounded_stdin() -> str:
    """Read piped Luau source, under the same cap a `--file` gets.

    Reads one character past the cap rather than the whole stream, so a `cat`
    of something enormous is refused instead of held in memory first.
    """
    source = sys.stdin.read(MAX_LUAU_SOURCE_BYTES + 1)
    if len(source) > MAX_LUAU_SOURCE_BYTES:
        raise StudioRequestError(
            f"the Luau source on stdin is over {MAX_LUAU_SOURCE_BYTES} bytes, which is where "
            "`-` is capped, the same as --file. Read it in Studio instead of sending it."
        )
    return source


def check_luau_file_is_readable_and_bounded(file_path: Path) -> None:
    """Refuse a `--file` that is not an ordinary, reasonably sized source file.

    A FIFO or a device passes an `exists()` check and then blocks the read
    forever, and a huge file is a slow way to discover that the request will not
    fit down the pipe anyway.
    """
    try:
        status = os.stat(file_path)
    except OSError as stat_error:
        raise StudioRequestError(f"cannot read {file_path}: {stat_error}") from stat_error
    if not stat.S_ISREG(status.st_mode):
        raise StudioRequestError(
            f"{file_path} is not a regular file (reading a FIFO or device would block); "
            "--file takes a Luau source file"
        )
    if status.st_size > MAX_LUAU_SOURCE_BYTES:
        raise StudioRequestError(
            f"{file_path} is {status.st_size} bytes; --file is capped at "
            f"{MAX_LUAU_SOURCE_BYTES} bytes. Read it in Studio instead of sending it."
        )
