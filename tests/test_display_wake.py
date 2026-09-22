"""Tests for the display-wake helper, which never launches a real `caffeinate`.

The behaviour worth pinning is narrow: two processes with the right flags, an
argv list and no shell, both torn down afterwards, and a warning instead of an
exception whenever any of that is impossible. A screenshot must not fail because
a convenience did.

The hold also has to survive this process NOT getting to tear it down. A
`caffeinate -d` with no exit condition reparents to launchd when the CLI is
killed, and the Mac then stays awake until somebody notices, so the flags that
bound it are asserted here rather than left to the `finally`.
"""

import os
import subprocess

from fake_caffeinate import FakeCaffeinate

from roblox_studio_cli import display_wake
from roblox_studio_cli.display_wake import (
    CAFFEINATE_BINARY_PATH,
    DISPLAY_HOLD_FLOOR_SECONDS,
    DISPLAY_HOLD_MARGIN_SECONDS,
    DISPLAY_WAKE_SECONDS,
    display_kept_awake,
    start_caffeinate,
)

SHORT_CALL_SECONDS = 120.0
LONG_CALL_SECONDS = 600.0


def test_start_caffeinate_uses_an_argv_list_and_never_a_shell(monkeypatch):
    seen = {}

    def fake_popen(command, **keyword_arguments):
        seen["command"] = command
        seen["keyword_arguments"] = keyword_arguments
        return FakeCaffeinate(command)

    monkeypatch.setattr(display_wake.subprocess, "Popen", fake_popen)
    start_caffeinate(["-u", "-t", "15"])

    assert seen["command"] == [CAFFEINATE_BINARY_PATH, "-u", "-t", "15"]
    assert seen["keyword_arguments"].get("shell") is None
    assert seen["keyword_arguments"]["stdin"] == subprocess.DEVNULL


def test_a_missing_caffeinate_is_a_warning_and_not_a_failure(monkeypatch):
    def refuse(*args, **keyword_arguments):
        raise FileNotFoundError(CAFFEINATE_BINARY_PATH)

    monkeypatch.setattr(display_wake.subprocess, "Popen", refuse)
    monkeypatch.setattr(display_wake.platform, "system", lambda: "Darwin")
    with display_kept_awake(True, SHORT_CALL_SECONDS) as warning:
        assert "carrying on" in warning


def test_both_assertions_are_held_for_the_block_and_dropped_after(monkeypatch):
    """`-u` wakes the display, `-d` keeps it awake while the capture runs."""
    launched: list[FakeCaffeinate] = []

    def record(arguments):
        launched.append(FakeCaffeinate(arguments))
        return launched[-1]

    monkeypatch.setattr(display_wake.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(display_wake, "start_caffeinate", record)

    with display_kept_awake(True, SHORT_CALL_SECONDS) as warning:
        assert warning == ""
        assert [item.arguments for item in launched] == [
            ["-u", "-t", str(DISPLAY_WAKE_SECONDS)],
            ["-d", "-w", str(os.getpid()), "-t", str(DISPLAY_HOLD_FLOOR_SECONDS)],
        ]
        assert not any(item.terminated for item in launched), "released before the capture ran"
    assert all(item.terminated for item in launched), "an assertion outlived the command"


def test_the_hold_is_released_by_the_kernel_when_this_process_dies(monkeypatch):
    """`finally` never runs on SIGKILL, so the hold must not depend on it.

    Without `-w`, a killed CLI leaves `caffeinate -d` reparented to launchd and
    the display awake indefinitely. `-w <our pid>` makes the kernel drop the
    assertion however this process ends, and `-t` is the backstop for the case
    where even that fails.
    """
    launched: list[FakeCaffeinate] = []

    def record(arguments):
        launched.append(FakeCaffeinate(arguments))
        return launched[-1]

    monkeypatch.setattr(display_wake.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(display_wake, "start_caffeinate", record)

    with display_kept_awake(True, SHORT_CALL_SECONDS):
        hold = launched[-1].arguments

    assert "-d" in hold, "the display-sleep assertion is the point of the hold"
    assert hold[hold.index("-w") + 1] == str(os.getpid()), "the hold is not tied to this process"
    assert int(hold[hold.index("-t") + 1]) > 0, "the hold has no ceiling"


def test_nothing_is_launched_when_the_flag_was_not_passed(monkeypatch):
    def refuse(arguments):
        raise AssertionError("caffeinate was launched without --wake-display")

    monkeypatch.setattr(display_wake, "start_caffeinate", refuse)
    with display_kept_awake(False, SHORT_CALL_SECONDS) as warning:
        assert warning == ""


def test_the_hold_outlasts_the_call_it_is_holding_for(monkeypatch):
    """A fixed 300 s hold expired mid-capture under a --timeout larger than it.

    The display then slept while Studio was still working on the capture, which
    is the exact failure --wake-display exists to prevent.
    """
    launched: list[FakeCaffeinate] = []
    monkeypatch.setattr(display_wake.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(display_wake, "start_caffeinate",
                        lambda arguments: launched.append(FakeCaffeinate(arguments)) or launched[-1])

    with display_kept_awake(True, LONG_CALL_SECONDS):
        hold = launched[-1].arguments

    held_seconds = int(hold[hold.index("-t") + 1])
    assert held_seconds == int(LONG_CALL_SECONDS) + DISPLAY_HOLD_MARGIN_SECONDS
    assert held_seconds > LONG_CALL_SECONDS, "the hold expires before the call it covers"


def test_a_caffeinate_that_died_on_launch_is_reported(monkeypatch):
    """Popen succeeding says the fork worked, not that caffeinate is running.

    A bad flag or a sandbox denial exits within milliseconds, and the capture
    then goes ahead against a display nothing is holding awake.
    """
    monkeypatch.setattr(display_wake.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(display_wake, "start_caffeinate",
                        lambda arguments: FakeCaffeinate(arguments, returncode=1))

    with display_kept_awake(True, SHORT_CALL_SECONDS) as warning:
        assert "exited" in warning, warning
        assert CAFFEINATE_BINARY_PATH in warning
