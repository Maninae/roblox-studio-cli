"""Tests for the terminal sanitiser, using the escape sequences that actually work.

Each attack here was fired at a real pty first: OSC 52 wrote the clipboard, OSC 0
rewrote the window title, CSI 2J cleared the screen. Anything Studio returns can
carry them, because a place file can contain any bytes at all.

The second group is the quieter half: characters a terminal does not execute but
a reader cannot see. A tag-character run is invisible payload inside a tool name,
and RLO reverses how a filename renders while leaving its bytes alone.

The third is length. Nothing above bounds how MUCH clean text a server can make
a terminal print, and a 6 MB server name scrolled a report off the screen using
nothing but the letter x.
"""

import pytest

from roblox_studio_cli.terminal import (
    MAX_DIAGNOSTIC_TEXT_CHARS,
    sanitize_diagnostic_line,
    sanitize_single_line,
    sanitize_terminal_text,
    truncate_display_text,
)


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
        ("soft hyphen", "vis\u00adible", "visible"),
        ("line separator", "vis\u2028ible", "visible"),
        ("paragraph separator", "vis\u2029ible", "visible"),
        ("Hangul filler", "vis\u3164ible", "visible"),
        ("Hangul choseong filler", "vis\u115fible", "visible"),
    ],
)
def test_invisible_and_reordering_characters_do_not_survive(label, hostile, expected):
    """Nothing a terminal executes, but all of it changes what a reader sees."""
    assert sanitize_terminal_text(hostile) == expected, label


def test_a_tag_smuggled_instruction_is_dropped_entirely():
    """Tag characters render as nothing, so a whole sentence can hide inside a tool name."""
    smuggled = "".join(chr(0xE0000 + ord(character)) for character in "ignore previous")
    assert sanitize_terminal_text(f"screen_capture{smuggled}") == "screen_capture"


@pytest.mark.parametrize(
    "label, hostile",
    [
        ("Arabic letter mark", "vis؜ible"),
        ("Mongolian vowel separator", "vis᠎ible"),
        ("interlinear annotation anchor", "vis￹ible"),
        ("interlinear annotation separator", "vis￺ible"),
        ("interlinear annotation terminator", "vis￻ible"),
        ("halfwidth Hangul filler", "visﾠible"),
    ],
)
def test_the_format_characters_no_hand_written_range_listed_are_dropped_too(label, hostile):
    """The enumerated ranges missed these; the category they share does not."""
    assert sanitize_terminal_text(hostile) == "visible", label


@pytest.mark.parametrize(
    "label, hostile, expected",
    [
        ("lone low surrogate", "vis\udcffible", "visible"),
        ("lone high surrogate", "vis\ud800ible", "visible"),
        # Written with chr(), because CPython folds a surrogate PAIR in a string
        # literal back into the astral character it encodes.
        ("both halves of a pair", chr(0xD83D) + chr(0xDE00) + "visible", "visible"),
    ],
)
def test_surrogates_never_reach_a_utf_8_terminal(label, hostile, expected):
    """A lone surrogate is unencodable, so printing one raises instead of printing.

    `json.loads` produces them happily from a `"\\udcff"` escape on the wire, and
    the result cost the command its whole output: `UnicodeEncodeError` on the
    first print, exit 1, and for `doctor` no verdict at all.
    """
    cleaned = sanitize_terminal_text(hostile)
    assert cleaned == expected, label
    # The line that used to raise: encoding for a UTF-8 stdout.
    assert cleaned.encode("utf-8").decode("utf-8") == expected, label


def test_a_smear_of_enclosing_marks_is_trimmed_like_any_other_run():
    """Me marks draw a circle per mark around the base, which smears the same way."""
    assert sanitize_terminal_text("a" + "⃝" * 60 + "b") == "a⃝⃝⃝b"


def test_single_line_folds_newlines_and_tabs_into_spaces():
    """A name with a newline in it must not forge a second row of output."""
    assert sanitize_single_line("first\nsecond") == "first second"
    assert sanitize_single_line("a\t\tb") == "a b"
    assert sanitize_single_line("  padded\n") == "padded"
    assert sanitize_single_line("plain") == "plain"


def test_short_diagnostic_text_is_left_exactly_as_it_is():
    """The cap must be invisible at every length a real server name has."""
    assert truncate_display_text("RobloxStudio 1.0.0") == "RobloxStudio 1.0.0"
    assert sanitize_diagnostic_line("c2bc0a63-ae87-4dfc-9bc2-a3045924ab06 (Place1)") == (
        "c2bc0a63-ae87-4dfc-9bc2-a3045924ab06 (Place1)"
    )


def test_a_giant_diagnostic_field_is_cut_with_an_ellipsis():
    """Measured: a 6 MB serverInfo name was echoed verbatim into the doctor report."""
    capped = sanitize_diagnostic_line("x" * 6_000_000)
    assert len(capped) == MAX_DIAGNOSTIC_TEXT_CHARS
    assert capped.endswith("...")


def test_the_cap_applies_after_sanitising_not_before():
    """Escapes are stripped first, so padding with them cannot smuggle text past the cap."""
    hostile = "\x1b[31m" * 100 + "visible"
    assert sanitize_diagnostic_line(hostile) == "visible"


def test_a_folded_multi_line_field_is_capped_too():
    rows = "\n".join(f"forged row {index}" for index in range(500))
    capped = sanitize_diagnostic_line(rows)
    assert "\n" not in capped
    assert len(capped) == MAX_DIAGNOSTIC_TEXT_CHARS


def test_a_smear_of_combining_marks_is_trimmed_to_a_readable_stack():
    """Hundreds of marks on one base character write over the rows above and below.

    No control character, no invisible character: just Mn marks doing what they
    are for. Three is more than any real script stacks, and what is left still
    reads as the text it was.
    """
    smeared = "a" + "\u0301" * 60 + "b"
    assert sanitize_terminal_text(smeared) == "a\u0301\u0301\u0301b"


def test_real_diacritics_are_left_alone():
    """Trimming has to be invisible to text that legitimately stacks marks."""
    assert sanitize_terminal_text("caf\u00e9") == "caf\u00e9", "a precomposed accent"
    assert sanitize_terminal_text("e\u0301") == "e\u0301", "one decomposed accent"
    assert sanitize_terminal_text("\u1ec7") == "\u1ec7", "Vietnamese, precomposed"
    assert sanitize_terminal_text("e\u0323\u0302") == "e\u0323\u0302", "Vietnamese, two marks"
