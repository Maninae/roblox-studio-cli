"""Tests for the two doors that read somebody else's bytes into a `luau` call.

`--file` and `-` end up in the same place, one JSON-RPC request down a pipe, so
they carry the same cap and the same refusals. Each test here is a way one door
behaved differently from the other:

- The cap is written in bytes and used to be applied to characters on stdin, so
  8,388,608 astral characters (33 MB, and a 96 MB request) passed a check that
  reads as 8 MB.
- Undecodable bytes on stdin came through as surrogate-escaped text and were
  forwarded to Studio; from a `--file` they were a clean message.
- `luau - <&-` is a process with no stdin at all, which was an AttributeError.
- An empty argument, an empty pipe and a file of whitespace all sent nothing to
  Studio, at the cost of a spawn, a `tools/list` and a call.

The `--file` tests also hold the property their refusals cannot show on their
own: the descriptor that was checked is the one that was read. See
`only_descriptor_reads`.
"""

import contextlib
import io
import os
from pathlib import Path

import pytest

from roblox_studio_cli import luau_source as luau_source_module
from roblox_studio_cli.errors import StudioRequestError
from roblox_studio_cli.luau_source import MAX_LUAU_SOURCE_BYTES, read_luau_source

ASTRAL_CHARACTER = "\U0001f600"
ASTRAL_BYTES = ASTRAL_CHARACTER.encode("utf-8")
BYTE_ORDER_MARK = "\ufeff"
ZERO_WIDTH_SPACE = "\u200b"


class FakeStdin:
    """A stdin with a binary buffer under it, which is what a real one has."""

    def __init__(self, data: bytes):
        """Hold `data` as the bytes `sys.stdin.buffer` would hand back."""
        self.buffer = io.BytesIO(data)


def piped(monkeypatch, data: bytes) -> None:
    """Point the module's stdin at `data`."""
    monkeypatch.setattr(luau_source_module.sys, "stdin", FakeStdin(data))


PATH_READERS = ("stat", "lstat", "exists", "is_file", "is_dir", "open", "read_text", "read_bytes")


@contextlib.contextmanager
def only_descriptor_reads():
    """Assert that nothing inside this block asked the filesystem about a PATH.

    `--file` opens once and asks `fstat` about the open file, because a path can
    be replaced between a check of it and a later open of it. A path-based
    implementation answers a FIFO and a directory with the same refusals, so
    those tests do not guard the property on their own; this is what does.

    It records rather than raising on the spot, and the patches come off before
    the block is left. pytest's own reporting calls `Path.exists`, so a version
    that raised took the test runner down with an INTERNALERROR instead of
    failing the test that caught the regression.
    """
    originals = {name: getattr(Path, name) for name in PATH_READERS}
    reached: list[str] = []

    def recorder(name):
        def record(self, *arguments, **keyword_arguments):
            reached.append(name)
            return originals[name](self, *arguments, **keyword_arguments)

        return record

    for name in PATH_READERS:
        setattr(Path, name, recorder(name))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(Path, name, original)
    assert not reached, f"went by path ({', '.join(sorted(set(reached)))}), not by the descriptor"


def test_the_stdin_cap_counts_bytes_not_characters(monkeypatch):
    """Four bytes per character means a quarter of the cap in characters passed it."""
    oversized = ASTRAL_BYTES * (MAX_LUAU_SOURCE_BYTES // len(ASTRAL_BYTES) + 1)
    assert len(oversized) > MAX_LUAU_SOURCE_BYTES
    assert len(oversized.decode("utf-8")) < MAX_LUAU_SOURCE_BYTES, "not the case being tested"

    piped(monkeypatch, oversized)
    with pytest.raises(StudioRequestError, match="capped"):
        read_luau_source("-", None)


def test_a_script_of_astral_characters_under_the_cap_still_reads(monkeypatch):
    """The cap must be invisible to any script a person would actually pipe in."""
    piped(monkeypatch, f'print("{ASTRAL_CHARACTER}")'.encode())
    assert read_luau_source("-", None) == f'print("{ASTRAL_CHARACTER}")'


def test_undecodable_bytes_on_stdin_are_a_message_not_a_surrogate_script(monkeypatch):
    """Forwarding surrogate-escaped text to Studio hides the fault one hop away."""
    piped(monkeypatch, b"\xff\xfe return 1")
    with pytest.raises(StudioRequestError, match="stdin"):
        read_luau_source("-", None)


def test_a_process_with_no_stdin_gets_a_message_rather_than_an_attribute_error(monkeypatch):
    """`luau - <&-` leaves sys.stdin as None, which used to reach `.read`."""
    monkeypatch.setattr(luau_source_module.sys, "stdin", None)
    with pytest.raises(StudioRequestError, match="stdin"):
        read_luau_source("-", None)


def test_a_leading_byte_order_mark_is_not_part_of_the_script(monkeypatch):
    """An editor's BOM is invisible in the file and a syntax error in Luau."""
    piped(monkeypatch, (BYTE_ORDER_MARK + "return 1").encode("utf-8"))
    assert read_luau_source("-", None) == "return 1"


def test_a_file_keeps_its_own_cap_and_loses_its_own_byte_order_mark(tmp_path):
    script = tmp_path / "probe.luau"
    script.write_text(BYTE_ORDER_MARK + "return workspace.Name", encoding="utf-8")
    assert read_luau_source(None, script) == "return workspace.Name"


def test_an_oversized_file_is_refused_without_reading_it(tmp_path):
    script = tmp_path / "huge.luau"
    script.write_bytes(b"-" * (MAX_LUAU_SOURCE_BYTES + 1))
    with pytest.raises(StudioRequestError, match="capped at"):
        read_luau_source(None, script)


def test_a_fifo_is_refused_on_the_descriptor_that_was_opened(tmp_path):
    """The check and the read have to be the same open file, and opening must not block."""
    fifo = tmp_path / "script.luau"
    os.mkfifo(fifo)
    with only_descriptor_reads():
        with pytest.raises(StudioRequestError, match="not a regular file"):
            read_luau_source(None, fifo)


def test_a_directory_is_refused_the_same_way(tmp_path):
    with only_descriptor_reads():
        with pytest.raises(StudioRequestError, match="cannot read|not a regular file"):
            read_luau_source(None, tmp_path)


def test_an_ordinary_file_is_read_through_the_descriptor_that_was_checked(tmp_path):
    """The refusals above hold against a path-based check too, so this is the guard.

    A `stat` of the path followed by a `read_text` of the path answers a FIFO
    and a directory exactly the way the descriptor does, and is wrong for the
    reason the FIFO test cannot show: the path can be replaced in between.
    """
    script = tmp_path / "probe.luau"
    script.write_text("return workspace.Name", encoding="utf-8")
    with only_descriptor_reads():
        assert read_luau_source(None, script) == "return workspace.Name"


def test_source_as_an_argument_is_left_exactly_as_typed():
    assert read_luau_source("return 1 + 1", None) == "return 1 + 1"


def test_an_empty_argument_is_a_message_rather_than_an_empty_call():
    """`roblox-studio luau ""` spent a spawn, a tools/list and a call to send nothing."""
    with pytest.raises(StudioRequestError, match="provide Luau source"):
        read_luau_source("", None)


def test_an_empty_pipe_is_refused_at_the_same_place(monkeypatch):
    piped(monkeypatch, b"")
    with pytest.raises(StudioRequestError, match="provide Luau source"):
        read_luau_source("-", None)


def test_a_file_of_whitespace_is_not_a_script(tmp_path):
    """A newline is what an editor leaves behind, not something to run in Studio."""
    script = tmp_path / "empty.luau"
    script.write_text("\n  \t\n", encoding="utf-8")
    with pytest.raises(StudioRequestError, match="provide Luau source"):
        read_luau_source(None, script)


def test_a_script_that_is_only_a_comment_still_counts_as_source():
    """Refusing empty must not start refusing scripts that merely do nothing."""
    assert read_luau_source("-- nothing to see", None) == "-- nothing to see"


@pytest.mark.parametrize(
    "source, reason",
    [
        (BYTE_ORDER_MARK, "a BOM is what an editor wrote, not what the caller typed"),
        (BYTE_ORDER_MARK * 2, "and a file through two editors has two of them"),
        (ZERO_WIDTH_SPACE, "a zero-width space is not a statement"),
        ("\x00", "nor is a NUL, which `strip()` does not know about"),
        (f" {BYTE_ORDER_MARK}\t{ZERO_WIDTH_SPACE}\n", "nor all of them together"),
    ],
)
def test_a_source_of_invisible_characters_is_an_empty_source(source, reason):
    """`str.strip()` only knows whitespace, so each of these passed as a script.

    Every one of them cost a proxy spawn, a `tools/list` and a `tools/call` to
    hand Studio something it cannot compile, through the one door that never
    stripped anything: the argument.
    """
    with pytest.raises(StudioRequestError, match="provide Luau source"):
        read_luau_source(source, None)


def test_the_argument_door_loses_a_byte_order_mark_too(tmp_path):
    """A BOM pasted into an argument is the same BOM a file carries."""
    assert read_luau_source(BYTE_ORDER_MARK + "return 1", None) == "return 1"

    script = tmp_path / "twice.luau"
    script.write_text(BYTE_ORDER_MARK * 2 + "return 2", encoding="utf-8")
    assert read_luau_source(None, script) == "return 2", "the second BOM survived"


def test_an_invisible_character_inside_a_script_is_left_alone():
    """Refusing invisible-only source must not start editing source that has one in it."""
    source = f'return "a{ZERO_WIDTH_SPACE}b"'
    assert read_luau_source(source, None) == source
