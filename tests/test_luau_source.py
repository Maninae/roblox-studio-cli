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
"""

import io
import os

import pytest

from roblox_studio_cli import luau_source as luau_source_module
from roblox_studio_cli.errors import StudioRequestError
from roblox_studio_cli.luau_source import MAX_LUAU_SOURCE_BYTES, read_luau_source

ASTRAL_CHARACTER = "\U0001f600"
ASTRAL_BYTES = ASTRAL_CHARACTER.encode("utf-8")
BYTE_ORDER_MARK = "﻿"


class FakeStdin:
    """A stdin with a binary buffer under it, which is what a real one has."""

    def __init__(self, data: bytes):
        """Hold `data` as the bytes `sys.stdin.buffer` would hand back."""
        self.buffer = io.BytesIO(data)


def piped(monkeypatch, data: bytes) -> None:
    """Point the module's stdin at `data`."""
    monkeypatch.setattr(luau_source_module.sys, "stdin", FakeStdin(data))


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
    with pytest.raises(StudioRequestError, match="not a regular file"):
        read_luau_source(None, fifo)


def test_a_directory_is_refused_the_same_way(tmp_path):
    with pytest.raises(StudioRequestError, match="cannot read|not a regular file"):
        read_luau_source(None, tmp_path)


def test_source_as_an_argument_is_left_exactly_as_typed():
    assert read_luau_source("return 1 + 1", None) == "return 1 + 1"
