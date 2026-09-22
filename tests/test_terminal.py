"""Tests for the terminal sanitiser, using the escape sequences that actually work.

Each attack here was fired at a real pty first: OSC 52 wrote the clipboard, OSC 0
rewrote the window title, CSI 2J cleared the screen. Anything Studio returns can
carry them, because a place file can contain any bytes at all.
"""

import pytest

from roblox_studio_cli.terminal import sanitize_terminal_text


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
