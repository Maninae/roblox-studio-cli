r"""Strip terminal control sequences out of server-controlled text before echoing it.

Everything the bridge hands back is attacker-influenceable text: tool output,
console lines, script sources, tool names and descriptions, place and instance
names, the proxy's own stderr. Echoed raw, an escape sequence inside that text
is not displayed by a terminal, it is executed. Measured against a pty while
writing this module: OSC 52 wrote the system clipboard, OSC 0 rewrote the window
title, and CSI 2J cleared the screen and scrollback.

`sanitize_terminal_text` keeps printable text plus newline and tab, which is
everything a CLI needs to show, and drops the rest. Four passes, in order:

1. Every ESC-introduced sequence: CSI (`ESC [ ... final`), OSC (`ESC ] ...`
   terminated by BEL or ST, or unterminated to the end of the text), and the
   single-character escapes.
2. Every remaining C0 and C1 control character, DEL included. Carriage return
   goes too, since redrawing a line is how output hides itself.
3. The invisible characters no Unicode category names: the line and paragraph
   separators (Zl and Zp), the Hangul fillers (letters, with width and no ink),
   and the whole tag block, half of which is unassigned rather than format.
4. One walk over the characters, applying the three rules that are category
   questions rather than range questions:
   - Format characters (Cf) go, all of them. That is the zero-width set, the
     bidi overrides and isolates, the soft hyphen, the BOM, the assigned tag
     characters, and the ones no hand-written range had: U+061C, U+180E and
     the interlinear annotation marks. None are executed by a terminal and all
     change what a reader sees without changing what they can select: RLO
     reverses a rendered filename, a soft hyphen splits a word for anyone
     searching the output. Losing the ZWJ inside an emoji sequence is the
     accepted cost.
   - Surrogates (Cs) go. `json.loads` produces a lone surrogate from a
     `"\udcff"` escape, and it is not encodable as UTF-8 at all: printing one
     raised `UnicodeEncodeError` and cost the command its entire output, which
     for `doctor` meant three rows and no verdict.
   - Runs of combining marks (Mn, Mc, Me), trimmed to three per base character.
     A base with hundreds of marks renders as a vertical smear over the rows
     above and below, hiding output the reader came for, using nothing a
     terminal executes.

`--json` output does not go through this: `json.dumps` already escapes control
characters as `\uXXXX`, a consumer parsing JSON is not a terminal, and a caller
piping JSON somewhere else needs the bytes the server actually sent.
"""

import re
import unicodedata
from collections.abc import Iterable

import typer

# Order matters: ESC sequences are removed as whole sequences first, so that the
# second pattern only ever sees leftover bare control bytes.
ESCAPE_SEQUENCE_PATTERN = re.compile(
    "\x1b\\[[0-?]*[ -/]*[@-~]"
    "|\x1b\\][\\s\\S]*?(?:\x07|\x1b\\\\|\\Z)"
    "|\x1b[@-_]"
    "|\x1b."
)
CONTROL_CHARACTER_PATTERN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# The invisible characters that are NOT format characters, so the category walk
# below cannot find them: the Hangul fillers (letters, width with no ink, the
# halfwidth one included), the line and paragraph separators (Zl and Zp), and
# the tag block, whose unassigned half is Cn rather than Cf and which is the
# classic way to smuggle a sentence that renders as nothing at all.
INVISIBLE_CHARACTER_PATTERN = re.compile(
    "[\u115f\u1160\u3164\uffa0"
    "\u2028\u2029"
    "\U000e0000-\U000e007f]"
)
# Dropped wholesale by category, because enumerating ranges by hand is what let
# U+061C, U+180E and U+FFF9-FFFB through: Cf is every format character
# (zero-width, bidi, soft hyphen, BOM, tags) and Cs is the surrogates, which a
# UTF-8 stdout cannot encode at all.
DISCARDED_UNICODE_CATEGORIES = frozenset({"Cf", "Cs"})
# Marks that attach to the character before them, the enclosing ones (Me)
# included. More than a few on one base is not a script, it is a smear across
# the neighbouring rows.
COMBINING_MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})
MAX_COMBINING_MARKS_PER_BASE = 3
# What a row-shaped or one-line-shaped print turns a newline or tab into.
LINE_BREAK_REPLACEMENT = " "
LINE_BREAK_PATTERN = re.compile("[\n\t]+")
# How much DIAGNOSTIC text is worth printing: the server's name and version, an
# instance id and place name, the error the bridge last answered with. None of
# those carry meaning past a line or two, and the bridge picks their length: a
# 6 MB serverInfo name of nothing but "x" scrolled a doctor report off the
# screen without a single control character in it. Tool OUTPUT is never capped,
# because that is the thing the caller asked for.
MAX_DIAGNOSTIC_TEXT_CHARS = 200
TRUNCATION_MARKER = "..."
# How many server-chosen names one message may enumerate. Every such list (the
# tools a build exposes, the arguments a schema demands, the instances that
# registered) is chrome in front of the advice that follows it, and its length
# is the bridge's choice: 200 tools at 200 characters each is 40 KB of scroll
# between the caller and the sentence telling them what to do instead.
MAX_ENUMERATED_NAMES = 20


def sanitize_terminal_text(text: str) -> str:
    """Return `text` with every control sequence, control character and invisible mark removed.

    Newline and tab survive; everything else that a terminal would interpret
    rather than print, and everything that renders as nothing or reorders what
    is around it, does not. Safe to call on text that is already clean, and on a
    non-string it stringifies first so an error path cannot trip on it.
    """
    if not isinstance(text, str):
        text = str(text)
    without_sequences = ESCAPE_SEQUENCE_PATTERN.sub("", text)
    without_controls = CONTROL_CHARACTER_PATTERN.sub("", without_sequences)
    without_invisibles = INVISIBLE_CHARACTER_PATTERN.sub("", without_controls)
    return apply_unicode_category_rules(without_invisibles)


def apply_unicode_category_rules(text: str) -> str:
    """Drop format characters and surrogates, and cap combining marks per base.

    Three rules that are questions about a character's category rather than
    about a range, which is why they are a walk rather than a fourth regex:

    - Cf, the format characters, are dropped. Hand-written ranges kept missing
      members of this set (U+061C, U+180E, the interlinear annotation marks),
      and the category cannot.
    - Cs, the surrogates, are dropped. A lone surrogate is unencodable as UTF-8,
      so one in a tool name took the whole command's output with it.
    - Combining marks are capped at three per base character. Tool output is
      never truncated, so a server can print as much as it likes; what it may
      not do is print it ON something else. A base carrying hundreds of marks
      ("Zalgo" text) draws over the rows above and below, using nothing a
      terminal executes. Three is more than Vietnamese or Thai stack, so real
      text passes through untouched.

    ASCII short-circuits the walk, which is every large tool result in practice:
    no ASCII character is in any of these categories. Measured on this laptop,
    4.6 MB of ASCII output sanitises in 29 ms and 4.3 MB of non-ASCII in 0.4 s,
    against a per-request parse budget of 16 MB.
    """
    if text.isascii():
        return text
    kept: list[str] = []
    marks_on_this_base = 0
    for character in text:
        category = unicodedata.category(character)
        if category in DISCARDED_UNICODE_CATEGORIES:
            continue
        if category in COMBINING_MARK_CATEGORIES:
            marks_on_this_base += 1
            if marks_on_this_base > MAX_COMBINING_MARKS_PER_BASE:
                continue
        else:
            marks_on_this_base = 0
        kept.append(character)
    return "".join(kept)


def sanitize_single_line(text: str) -> str:
    """Sanitised text with newlines and tabs folded into spaces.

    For anything printed as part of a row or an identifier, where a server that
    puts a newline in a tool name or a place name would otherwise break the
    column alignment and forge what looks like a second entry.
    """
    return LINE_BREAK_PATTERN.sub(LINE_BREAK_REPLACEMENT, sanitize_terminal_text(text)).strip()


def truncate_display_text(text: str, limit: int = MAX_DIAGNOSTIC_TEXT_CHARS) -> str:
    """Cut display text down to `limit` characters, marking that it was cut.

    Apply it AFTER sanitising, never before: a field padded with escape
    sequences would otherwise spend the budget on characters that are about to
    be stripped, and arrive truncated for no reason.
    """
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def sanitize_diagnostic_line(text: str) -> str:
    """One-line, sanitised and length-capped, for a server-chosen label or identifier.

    The chrome a report is built from: a server name and version, an instance id
    and place name, whatever the bridge last said. Use `echo_server_text` for the
    answer itself, which is never capped.
    """
    return truncate_display_text(sanitize_single_line(text))


def capped_display_names(names: Iterable[str], limit: int = MAX_ENUMERATED_NAMES) -> list[str]:
    """Sanitised one-line names for an enumeration, ending in "and N more" when cut.

    The list half of the chrome rule: `sanitize_diagnostic_line` bounds how long
    one name may print, and this bounds how many of them print at all. Callers
    join the result however their message reads, with a comma or a row each.
    """
    entries = list(names)
    shown = [sanitize_diagnostic_line(name) for name in entries[:limit]]
    if len(entries) > limit:
        shown.append(f"and {len(entries) - limit} more")
    return shown


def echo_server_text(text: str, err: bool = False) -> None:
    """Echo text that came from the bridge, sanitised.

    Every print of server-derived text goes through here. Text the CLI wrote
    itself can use `typer.echo` directly.
    """
    typer.echo(sanitize_terminal_text(text), err=err)
