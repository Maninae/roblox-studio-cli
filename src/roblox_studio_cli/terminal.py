r"""Strip terminal control sequences out of server-controlled text before echoing it.

Everything the bridge hands back is attacker-influenceable text: tool output,
console lines, script sources, tool names and descriptions, place and instance
names, the proxy's own stderr. Echoed raw, an escape sequence inside that text
is not displayed by a terminal, it is executed. Measured against a pty while
writing this module: OSC 52 wrote the system clipboard, OSC 0 rewrote the window
title, and CSI 2J cleared the screen and scrollback.

`sanitize_terminal_text` keeps printable text plus newline and tab, which is
everything a CLI needs to show, and drops the rest. Two passes, in order:

1. Every ESC-introduced sequence: CSI (`ESC [ ... final`), OSC (`ESC ] ...`
   terminated by BEL or ST, or unterminated to the end of the text), and the
   single-character escapes.
2. Every remaining C0 and C1 control character, DEL included. Carriage return
   goes too, since redrawing a line is how output hides itself.

`--json` output does not go through this: `json.dumps` already escapes control
characters as `\uXXXX`, and a consumer parsing JSON is not a terminal.
"""

import re

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


def sanitize_terminal_text(text: str) -> str:
    """Return `text` with every terminal control sequence and control character removed.

    Newline and tab survive; everything else that a terminal would interpret
    rather than print does not. Safe to call on text that is already clean, and
    on a non-string it stringifies first so an error path cannot trip on it.
    """
    if not isinstance(text, str):
        text = str(text)
    without_sequences = ESCAPE_SEQUENCE_PATTERN.sub("", text)
    return CONTROL_CHARACTER_PATTERN.sub("", without_sequences)


def echo_server_text(text: str, err: bool = False) -> None:
    """Echo text that came from the bridge, sanitised.

    Every print of server-derived text goes through here. Text the CLI wrote
    itself can use `typer.echo` directly.
    """
    typer.echo(sanitize_terminal_text(text), err=err)
