"""Tests for the display-wake helper, which never launches a real `caffeinate`.

The behaviour worth pinning is narrow: two processes with the right flags, an
argv list and no shell, both torn down afterwards, and a warning instead of an
exception whenever any of that is impossible. A screenshot must not fail because
a convenience did.
"""

import subprocess

from fake_caffeinate import FakeCaffeinate

from roblox_studio_cli import display_wake
from roblox_studio_cli.display_wake import (
    CAFFEINATE_BINARY_PATH,
    DISPLAY_WAKE_SECONDS,
    display_kept_awake,
    start_caffeinate,
)


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
    with display_kept_awake(True) as warning:
        assert "carrying on" in warning


def test_both_assertions_are_held_for_the_block_and_dropped_after(monkeypatch):
    """`-u` wakes the display, `-d` keeps it awake while the capture runs."""
    launched: list[FakeCaffeinate] = []

    def record(arguments):
        launched.append(FakeCaffeinate(arguments))
        return launched[-1]

    monkeypatch.setattr(display_wake.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(display_wake, "start_caffeinate", record)

    with display_kept_awake(True) as warning:
        assert warning == ""
        assert [item.arguments for item in launched] == [
            ["-u", "-t", str(DISPLAY_WAKE_SECONDS)],
            ["-d"],
        ]
        assert not any(item.terminated for item in launched), "released before the capture ran"
    assert all(item.terminated for item in launched), "an assertion outlived the command"


def test_nothing_is_launched_when_the_flag_was_not_passed(monkeypatch):
    def refuse(arguments):
        raise AssertionError("caffeinate was launched without --wake-display")

    monkeypatch.setattr(display_wake, "start_caffeinate", refuse)
    with display_kept_awake(False) as warning:
        assert warning == ""
