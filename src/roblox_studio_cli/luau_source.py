"""Where the Luau source for one `luau` call comes from, and what it must not be.

Three ways in, all resolved before the proxy is spawned, so a typo costs a
message rather than a handshake: the source as an argument, `-` to read stdin,
or `--file PATH`.

Both of the ways that read something else's bytes are bounded, and bounded the
SAME way, because they end up in the same place: one JSON-RPC request down a
pipe that holds about 64 KB, to a Studio text box that is not where a
multi-megabyte script belongs. A `--file` was capped at `MAX_LUAU_SOURCE_BYTES`
while `-` was not, which is the same script arriving by a different door.

Both doors read BYTES and decode strictly, which is what makes the cap mean
what it says. Reading stdin as text counted characters against a byte budget,
so 8,388,608 astral characters (33 MB of UTF-8, and a 96 MB JSON request)
passed a check written as 8 MB, and undecodable bytes arrived as
surrogate-escaped text that was forwarded to Studio rather than refused.

`--file` also has to be an ordinary file, and the check and the read are the
same open descriptor: a FIFO or a device passes every `exists()` check, blocks
the read forever, and a path can be swapped between a check and a later open.
`O_NONBLOCK` is what makes the open itself safe to attempt, since a FIFO with
no writer blocks inside `open()` before any check could run.

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
# Invisible in an editor, a syntax error to Luau: an editor's BOM is not source.
BYTE_ORDER_MARK = "﻿"


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
        return read_bounded_file(file_path)
    if code is None:
        raise StudioRequestError(
            "provide Luau source as an argument, `-` to read stdin, or --file PATH"
        )
    if code == STDIN_SOURCE_MARKER:
        return read_bounded_stdin()
    return code


def read_bounded_stdin() -> str:
    """Read piped Luau source as bytes, under the same cap a `--file` gets.

    Bytes, because the cap is a byte cap. `sys.stdin.read` counts CHARACTERS,
    so 8,388,608 astral characters walked through an 8 MB check as 33 MB of
    UTF-8 and a 96 MB JSON request. Reading one byte past the cap rather than
    the whole stream is what makes a `cat` of something enormous a refusal
    instead of a memory read.

    Decoding is strict, so undecodable input is a message here rather than a
    surrogate-escaped script handed to Studio. `sys.stdin` itself can be absent:
    `luau - <&-` is a process with no stdin at all, which used to be an
    `AttributeError` from the catch-all.

    Raises:
        StudioRequestError: no readable stdin, over the cap, or not UTF-8.
    """
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise StudioRequestError(
            "`-` reads Luau source from stdin, and this process has no readable stdin. "
            "Pass the source as an argument or with --file PATH."
        )
    try:
        raw = stream.read(MAX_LUAU_SOURCE_BYTES + 1)
        refuse_oversized_source(
            len(raw) > MAX_LUAU_SOURCE_BYTES,
            f"the Luau source on stdin is over {MAX_LUAU_SOURCE_BYTES} bytes, which is where "
            "`-` is capped, the same as --file.",
        )
        return strip_byte_order_mark(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError) as stdin_error:
        raise StudioRequestError(
            f"cannot read Luau source from stdin: {stdin_error}"
        ) from stdin_error


def read_bounded_file(file_path: Path) -> str:
    """Read a `--file` through one descriptor, checked on that same descriptor.

    Opening first and asking `fstat` about the open file is what keeps the
    regular-file check and the read talking about the same thing: a path can be
    replaced between a `stat` of it and a later `open` of it. `O_NONBLOCK` makes
    the open safe to attempt at all, because a FIFO with no writer blocks inside
    `open()` long before any check could run.

    Raises:
        StudioRequestError: unreadable, not an ordinary file, over the cap, or
            not UTF-8. All caller-fixable, so they exit 2.
    """
    try:
        descriptor = os.open(file_path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as open_error:
        raise StudioRequestError(f"cannot read {file_path}: {open_error}") from open_error

    try:
        with os.fdopen(descriptor, "rb") as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise StudioRequestError(
                    f"{file_path} is not a regular file (reading a FIFO or device would "
                    "block); --file takes a Luau source file"
                )
            refuse_oversized_source(
                status.st_size > MAX_LUAU_SOURCE_BYTES,
                f"{file_path} is {status.st_size} bytes; --file is capped at "
                f"{MAX_LUAU_SOURCE_BYTES} bytes.",
            )
            raw = handle.read(MAX_LUAU_SOURCE_BYTES + 1)
        # A file can grow between the fstat and the read, and some regular files
        # report a size they do not have, so the bytes in hand get the last word.
        refuse_oversized_source(
            len(raw) > MAX_LUAU_SOURCE_BYTES,
            f"{file_path} is over {MAX_LUAU_SOURCE_BYTES} bytes; --file is capped at "
            f"{MAX_LUAU_SOURCE_BYTES} bytes.",
        )
        return strip_byte_order_mark(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError) as read_error:
        raise StudioRequestError(f"cannot read {file_path}: {read_error}") from read_error


def refuse_oversized_source(over_the_cap: bool, detail: str) -> None:
    """Refuse an over-cap script, with the one piece of advice both doors give."""
    if over_the_cap:
        raise StudioRequestError(f"{detail} Read it in Studio instead of sending it.")


def strip_byte_order_mark(source: str) -> str:
    """Drop a leading BOM, which an editor writes and Luau will not compile."""
    return source[len(BYTE_ORDER_MARK):] if source.startswith(BYTE_ORDER_MARK) else source
