"""Keep the Mac's display awake around a capture, because a dark display captures nothing.

Measured against Studio 0.739 on macOS: with the display asleep, Studio's screen
capture tool accepts the call and never answers. The CLI's own timeout then
fires, and it reads exactly like a broken bridge, which is the wrong thing to go
and debug at two in the morning. `screenshot --wake-display` removes the cause,
and a screenshot that times out without the flag gets told about it.

Two `caffeinate` processes do the work, and they do different jobs:

- `-u -t 15` asserts user activity, which is what actually turns the display
  back on. It exits on its own after its window.
- `-d` holds the display-sleep assertion open for as long as the process lives,
  which here is the duration of the call.

Both are launched with an argv list and no shell, and both are torn down when
the block ends. `caffeinate` missing or refusing to start is a warning, never a
failure: the capture may well work anyway, and failing the command over a
convenience would be worse than trying it.
"""

import logging
import platform
import subprocess
from contextlib import contextmanager

logger = logging.getLogger(__name__)

MACOS_PLATFORM_NAME = "Darwin"
CAFFEINATE_BINARY_PATH = "/usr/bin/caffeinate"
DISPLAY_WAKE_SECONDS = 15
CAFFEINATE_SHUTDOWN_SECONDS = 2.0

DISPLAY_ASLEEP_HINT = (
    "If the Mac's display is asleep the capture never returns; rerun with --wake-display."
)
NOT_MACOS_WARNING = (
    "warning: --wake-display drives macOS `caffeinate`, and this is not macOS; ignoring it"
)


def display_is_wakeable() -> bool:
    """True on the one platform where there is a display to wake and a tool to wake it."""
    return platform.system() == MACOS_PLATFORM_NAME


def start_caffeinate(arguments: list[str]) -> subprocess.Popen | None:
    """Launch one `caffeinate` with the given flags, or None when it cannot be launched."""
    try:
        return subprocess.Popen(
            [CAFFEINATE_BINARY_PATH, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as launch_error:
        logger.debug("could not launch caffeinate %s: %s", arguments, launch_error)
        return None


def stop_caffeinate(process: subprocess.Popen) -> None:
    """Terminate and reap one `caffeinate`, whether or not it already exited."""
    try:
        process.terminate()
        process.wait(timeout=CAFFEINATE_SHUTDOWN_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as stop_error:
        logger.debug("could not stop caffeinate: %s", stop_error)


@contextmanager
def display_kept_awake(requested: bool):
    """Wake the display and hold it awake for the block, yielding a warning to print.

    The yielded string is empty in the ordinary case, and carries the one line
    the caller should show otherwise (asked for on the wrong platform, or
    `caffeinate` would not start). Printing is the CLI's job, not this module's.
    """
    if not requested:
        yield ""
        return
    if not display_is_wakeable():
        yield NOT_MACOS_WARNING
        return

    started = [
        process
        for process in (
            start_caffeinate(["-u", "-t", str(DISPLAY_WAKE_SECONDS)]),
            start_caffeinate(["-d"]),
        )
        if process is not None
    ]
    try:
        yield "" if started else f"warning: could not run {CAFFEINATE_BINARY_PATH}; carrying on"
    finally:
        for process in started:
            stop_caffeinate(process)
