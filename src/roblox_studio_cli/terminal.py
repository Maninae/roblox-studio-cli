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
3. Invisible and reordering Unicode: zero-width characters, the bidi overrides
   and isolates, the line and paragraph separators, the soft hyphen, the Hangul
   fillers, the BOM, and the tag block. None of these are executed by a
   terminal, but all of them change what a reader sees without changing the
   characters they can select: RLO reverses a rendered filename, a zero-width
   joiner hides a word boundary, a soft hyphen splits a word for anyone
   searching the output, and a tag-character run is invisible payload.
4. Runs of combining marks, trimmed to three per base character. A base with
   hundreds of marks on it renders as a vertical smear over the rows above and
   below, hiding output the reader came for, using nothing a terminal executes.

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
# Invisible or reordering, none of it executed by a terminal: the soft hyphen
# (splits a word for anyone searching the output), the Hangul fillers (width
# with no ink), zero-width and bidi marks (200b-200f), the line and paragraph
# separators, bidi embeddings and overrides (202a-202e), the invisible-operator
# block (2060-2064), the bidi isolates (2066-2069), the byte order mark, and the
# tag block used to smuggle text that renders as nothing at all.
INVISIBLE_CHARACTER_PATTERN = re.compile(
    "[\u00ad"
    "\u115f\u1160\u3164"
    "\u200b-\u200f"
    "\u2028\u2029"
    "\u202a-\u202e"
    "\u2060-\u2064"
    "\u2066-\u2069"
    "\ufeff"
    "\U000e0000-\U000e007f]"
)
# Marks that attach to the character before them. More than a few on one base is
# not a script, it is a smear across the neighbouring rows.
COMBINING_MARK_CATEGORIES = frozenset({"Mn", "Mc"})
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
    return limit_combining_mark_runs(without_invisibles)


def limit_combining_mark_runs(text: str) -> str:
    """Keep at most three combining marks on any one base character.

    Tool output is never truncated, so a server can print as much as it likes;
    what it may not do is print it ON something else. A base character carrying
    hundreds of Mn marks ("Zalgo" text) draws over the rows above and below it,
    and every character in it is ordinary printable Unicode. Three marks is more
    than Vietnamese or Thai stack, so real text passes through untouched.

    ASCII short-circuits the character walk, which is every large tool result in
    practice: no ASCII character is a combining mark. Measured on this laptop,
    4.6 MB of ASCII output sanitises in 29 ms and 4.3 MB of non-ASCII in 0.4 s,
    against a per-request parse budget of 16 MB.
    """
    if text.isascii():
        return text
    kept: list[str] = []
    marks_on_this_base = 0
    for character in text:
        if unicodedata.category(character) in COMBINING_MARK_CATEGORIES:
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
