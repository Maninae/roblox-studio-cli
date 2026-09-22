"""Tests for the terminal sanitiser, using the escape sequences that actually work.

Each attack here was fired at a real pty first: OSC 52 wrote the clipboard, OSC 0
rewrote the window title, CSI 2J cleared the screen. Anything Studio returns can
carry them, because a place file can contain any bytes at all.

The second group is the quieter half: characters a terminal does not execute but
a reader cannot see. A tag-character run is invisible payload inside a tool name,
and RLO reverses how a filename renders while leaving its bytes alone.
"""

import pytest

from roblox_studio_cli.terminal import sanitize_single_line, sanitize_terminal_text


@pytest.mark.parametrize(
    "label, hostile, expected",
    [
        ("OSC 52 clipboard write", "\x1b]52;c;aGVsbG8=\x07visible", "visible"),
        ("OSC 0 title, ST ended", "\x1b]0;NEW TITLE\x1b\\visible", "visible"),
        ("OSC unterminated", "visible\x1b]0;runs to the end", "visible"),
        ("CSI clear screen", "vis\x1b[2J\x1b[3J\x1b[Hible", "visible"),
        ("CSI colour", "\x1b[31mvisible\x1b[0m", "visible"),
        ("ESC c full reset", "visible\x1bc", "visible"),
        ("carriage return redraw", "visible\rFAKE", "visibleFAKE"),
        # Dropping the C1 introducer leaves its parameter bytes as inert text.
        ("C1 CSI introducer", "vis\x9bmible", "vismible"),
        ("NUL and DEL", "vis\x00\x7fible", "visible"),
    ],
)
def test_control_sequences_do_not_survive(label, hostile, expected):
    assert sanitize_terminal_text(hostile) == expected, label


def test_ordinary_text_is_untouched():
    text = "studio-1 (Place1)\tcolumn\nsecond line \u00e9 \u4e2d\u6587"
    assert sanitize_terminal_text(text) == text


def test_non_strings_are_stringified_rather_than_raising():
    assert sanitize_terminal_text(None) == "None"
    assert sanitize_terminal_text(42) == "42"


@pytest.mark.parametrize(
    "label, hostile, expected",
    [
        ("zero-width space", "vis​ible", "visible"),
        ("zero-width joiner", "vis‍ible", "visible"),
        ("right-to-left mark", "vis‏ible", "visible"),
        ("RLO filename reversal", "‮visible‬", "visible"),
        ("word joiner", "vis⁠ible", "visible"),
        ("invisible separator", "vis⁣ible", "visible"),
        ("bidi isolate pair", "⁦vis⁩ible", "visible"),
        ("byte order mark", "﻿visible", "visible"),
        ("tag characters", "vis\U000e0041\U000e007fible", "visible"),
    ],
)
def test_invisible_and_reordering_characters_do_not_survive(label, hostile, expected):
    """Nothing a terminal executes, but all of it changes what a reader sees."""
    assert sanitize_terminal_text(hostile) == expected, label


def test_a_tag_smuggled_instruction_is_dropped_entirely():
    """Tag characters render as nothing, so a whole sentence can hide inside a tool name."""
    smuggled = "".join(chr(0xE0000 + ord(character)) for character in "ignore previous")
    assert sanitize_terminal_text(f"screen_capture{smuggled}") == "screen_capture"


def test_single_line_folds_newlines_and_tabs_into_spaces():
    """A name with a newline in it must not forge a second row of output."""
    assert sanitize_single_line("first\nsecond") == "first second"
    assert sanitize_single_line("a\t\tb") == "a b"
    assert sanitize_single_line("  padded\n") == "padded"
    assert sanitize_single_line("plain") == "plain"
