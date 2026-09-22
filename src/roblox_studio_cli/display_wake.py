"""Keep the Mac's display awake around a capture, because a dark display captures nothing.

Measured against Studio 0.739 on macOS: with the display asleep, Studio's screen
capture tool accepts the call and never answers. The CLI's own timeout then
fires, and it reads exactly like a broken bridge, which is the wrong thing to go
and debug at two in the morning. `screenshot --wake-display` removes the cause,
and a screenshot that times out without the flag gets told about it.

Two `caffeinate` processes do the work, and they do different jobs:

- `-u -t 15` asserts user activity, which is what actually turns the display
  back on. It exits on its own after its window.
- `-d -w <our pid> -t <the call's deadline plus a margin>` holds the
  display-sleep assertion open for the duration of the call. The ceiling is
  sized to the call rather than fixed, because a fixed 300 s expired under a
  longer `--timeout` and let the display sleep mid-capture.

Both are launched with an argv list and no shell, and both are torn down when
the block ends. The hold carries two exit conditions anyway, because the
teardown is the one thing that cannot be relied on: `finally` does not run when
the CLI is SIGKILLed or its terminal is closed, and a bare `caffeinate -d` then
reparents to launchd and keeps the Mac's display awake until somebody notices.
`-w` hands that job to the kernel, which drops the assertion when this process
exits however it dies, and `-t` is the backstop ceiling if even that fails. Both
flags are documented in caffeinate(8) and were measured together: a hold started
with both exited the instant the watched pid did.

`caffeinate` missing or refusing to start is a warning, never a failure: the
capture may well work anyway, and failing the command over a convenience would
be worse than trying it. So is a `caffeinate` that starts and then exits: a
successful `Popen` says the fork worked, not that the process is still there,
and a bad flag or a sandbox denial is gone within milliseconds.
"""

import logging
import os
import platform
import subprocess
from contextlib import contextmanager

logger = logging.getLogger(__name__)

MACOS_PLATFORM_NAME = "Darwin"
CAFFEINATE_BINARY_PATH = "/usr/bin/caffeinate"
DISPLAY_WAKE_SECONDS = 15
# The hold's ceiling is the larger of this floor and the call's own deadline
# plus a margin. The floor covers a default capture (`--timeout` defaults to
# 120 s) and keeps a hold nothing released short-lived; the margin covers the
# seconds around the call itself, because a ceiling that expires while Studio
# is still capturing puts the display back to sleep mid-capture.
DISPLAY_HOLD_FLOOR_SECONDS = 300
DISPLAY_HOLD_MARGIN_SECONDS = 30
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


def display_hold_arguments(timeout: float) -> list[str]:
    """Flags for the hold: prevent display sleep, and end when this process does.

    The `finally` below terminates it on the ordinary path. These two flags cover
    the paths where nothing of ours runs at all, which is any signal that is not
    caught: `-w` releases the assertion when the kernel reaps this pid, and `-t`
    expires it regardless.

    Args:
        timeout: the call's own deadline in seconds. The ceiling is sized past
            it, so the hold cannot expire while the capture is still running.
    """
    held = max(DISPLAY_HOLD_FLOOR_SECONDS, int(timeout) + DISPLAY_HOLD_MARGIN_SECONDS)
    return ["-d", "-w", str(os.getpid()), "-t", str(held)]


def stop_caffeinate(process: subprocess.Popen) -> None:
    """Terminate and reap one `caffeinate`, whether or not it already exited."""
    try:
        process.terminate()
        process.wait(timeout=CAFFEINATE_SHUTDOWN_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as stop_error:
        logger.debug("could not stop caffeinate: %s", stop_error)


def wake_warning(started: list) -> str:
    """The one line worth printing about the wake attempt, empty when it looks healthy.

    Two ways it can be worth printing: nothing launched at all, or something
    launched and was already gone when we looked. Neither fails the command.
    """
    if not started:
        return f"warning: could not run {CAFFEINATE_BINARY_PATH}; carrying on"
    exited = [process for process in started if process.poll() is not None]
    if exited:
        return (
            f"warning: {CAFFEINATE_BINARY_PATH} exited immediately "
            f"(code {exited[0].returncode}); the display may not stay awake"
        )
    return ""


@contextmanager
def display_kept_awake(requested: bool, timeout: float):
    """Wake the display and hold it awake for the block, yielding a warning to print.

    The yielded string is empty in the ordinary case, and carries the one line
    the caller should show otherwise (asked for on the wrong platform, nothing
    would start, or something started and immediately died). Printing is the
    CLI's job, not this module's.

    Args:
        requested: whether `--wake-display` was passed.
        timeout: the call's deadline in seconds, which the hold must outlast.
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
            start_caffeinate(display_hold_arguments(timeout)),
        )
        if process is not None
    ]
    try:
        yield wake_warning(started)
    finally:
        for process in started:
            stop_caffeinate(process)
