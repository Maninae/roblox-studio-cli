"""End-to-end tests for the `roblox-studio` command surface.

These drive the real Typer app through Click's `CliRunner`, with
`ROBLOX_STUDIO_MCP_BIN` pointed at the fake server, so they cover the path a
user actually takes: parse flags, discover tools, wait for the Studio instance,
call, print, exit. `FAKE_STUDIO_MODE` selects which situation to simulate.

Exit codes are the contract worth protecting, so they are asserted everywhere:
0 success, 1 the environment is not ready, 2 the request was malformed.
"""

import ast
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from fake_caffeinate import FakeCaffeinate
from typer.testing import CliRunner

from roblox_studio_cli import display_wake as display_wake_module
from roblox_studio_cli import main as main_module
from roblox_studio_cli.client import (
    DEFAULT_INITIALIZE_TIMEOUT_SECONDS,
    DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS,
    StudioMcpClient,
)
from roblox_studio_cli.luau_source import MAX_LUAU_SOURCE_BYTES
from roblox_studio_cli.main import (
    EXIT_NOT_READY,
    EXIT_OK,
    EXIT_REQUEST_ERROR,
    app,
)
from roblox_studio_cli.mcp_payloads import ToolDefinition
from roblox_studio_cli.terminal import MAX_DIAGNOSTIC_TEXT_CHARS

FAKE_SERVER_PATH = Path(__file__).resolve().parent / "fake_studio_mcp_server.py"
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"
# Short enough to keep the suite quick: the attach wait is bounded by --timeout.
SHORT_TIMEOUT = "1"
SILENT_SERVER_TIMEOUT = "2"
# What a `--timeout 1` command may spend end to end, generously. The real cost
# is the second it asked for plus the shutdown grace a wedged proxy makes the
# client pay for reaping it: measured, 1.3 s against a silent `tools/list` and
# 4.2 s against a silent handshake. The regressions below are 30.3 s and 18.1 s,
# so there is room for a loaded runner without the assertion going quiet.
BOUNDED_COMMAND_SECONDS = 10.0
# Stands in for a path only the test knows, since the table is built at import.
OUT_PLACEHOLDER = "<out>"
# Every command that opens a client, so every `studio_client()` and every
# `list_tools()` call site is covered: the bug was one site at a time.
TIMEOUT_BOUNDED_COMMANDS = {
    "doctor": ["doctor"],
    "instances": ["instances"],
    "tools": ["tools"],
    "call": ["call", "execute_luau", "--args", '{"code": "return 1", "datamodel_type": "Edit"}'],
    "luau": ["luau", "return 1"],
    "screenshot": ["screenshot", "--out", OUT_PLACEHOLDER],
    "state": ["state"],
    "play": ["play", "--start"],
}

runner = CliRunner()


def invoke(arguments: list[str], mode: str = "connected", **kwargs):
    """Run the CLI against the fake server in `mode`, as a user would from a shell."""
    return runner.invoke(
        app,
        arguments,
        env={"ROBLOX_STUDIO_MCP_BIN": str(FAKE_SERVER_PATH), "FAKE_STUDIO_MODE": mode},
        **kwargs,
    )


def temporary_captures() -> set[Path]:
    """Every default-path capture sitting in the temp directory right now."""
    return set(Path(tempfile.gettempdir()).glob("studio_screen_capture_*"))


def all_output(result) -> str:
    """stdout plus stderr, whichever way this Click version chose to split them."""
    text = result.output or ""
    try:
        if result.stderr:
            text += result.stderr
    except ValueError:
        # Older Click mixes stderr into output and refuses to serve it separately.
        pass
    return text


def test_the_fake_server_stays_runnable_by_a_stock_python_shebang():
    """These tests hand the CLI a path, so the script's own shebang picks the interpreter.

    `#!/usr/bin/env python3` is Apple's 3.9.6 on a Mac that never installed
    another Python, and an annotation like `dict | None` is evaluated when the
    `def` runs: the whole file raises TypeError on import and every test here
    fails with the fake server "exiting immediately". The package itself
    requires 3.10, so this guard is on the one file that has to run anywhere.
    """
    tree = ast.parse(FAKE_SERVER_PATH.read_text())
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.extend(argument.annotation for argument in node.args.args)
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)

    union_annotations = [
        ast.unparse(annotation)
        for annotation in annotations
        if annotation is not None
        and any(
            isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr)
            for inner in ast.walk(annotation)
        )
    ]
    assert union_annotations == [], f"3.10-only annotations in the fake server: {union_annotations}"


def test_doctor_reports_connected():
    result = invoke(["doctor"])
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert "FakeRobloxStudio 1.0.0" in output
    assert "studio-1 (Baseplate)" in output
    assert "CONNECTED (6 tools, 1 instance)" in output


def test_status_is_an_alias_for_doctor():
    assert all_output(invoke(["status"])) == all_output(invoke(["doctor"]))


def test_doctor_without_a_place_open_says_nothing_attached():
    result = invoke(["doctor", "--timeout", SHORT_TIMEOUT], mode="no-instances")
    assert result.exit_code == EXIT_NOT_READY
    output = all_output(result)
    assert "none registered" in output
    # The bridge is up, so the verdict must not blame the connection.
    assert "NOT READY" in output
    assert "No Roblox Studio instance attached" in output


def test_doctor_without_the_toggle_names_the_toggle():
    result = invoke(["doctor", "--timeout", SILENT_SERVER_TIMEOUT], mode="no-tools")
    assert result.exit_code == EXIT_NOT_READY
    output = all_output(result)
    assert "Tools:" in output and "not available" in output
    assert "MCP Servers" in output
    assert "NOT CONNECTED" in output


def test_doctor_json_carries_the_verdict_and_the_attach_time():
    result = invoke(["doctor", "--json"])
    report = json.loads(result.output)
    assert report["ok"] is True
    assert report["tool_count"] == 6
    assert report["instances"] == [{"id": "studio-1", "name": "Baseplate"}]
    assert report["attach_seconds"] is not None


def test_doctor_waits_out_a_late_attach():
    """A real fresh session sees "not yet" for a few seconds before Studio shows up."""
    result = invoke(["doctor", "--json"], mode="attach-late")
    report = json.loads(result.output)
    assert report["ok"] is True, all_output(result)
    assert report["attach_seconds"] >= 1.0, "the poll gave up before the instance appeared"


def test_a_never_attaching_studio_quotes_what_the_bridge_said():
    result = invoke(["doctor", "--timeout", SHORT_TIMEOUT], mode="attach-never")
    assert result.exit_code == EXIT_NOT_READY
    output = all_output(result)
    assert "No Roblox Studio instance attached" in output
    assert "Unable to reach Roblox Studio" in output, "the bridge's own words were dropped"
    # The wait is the smaller of the attach window and --timeout, and the
    # message has to name the one that elapsed: this run waited a second.
    assert f"within {SHORT_TIMEOUT} s" in output, "the advice named a window nobody waited"


def test_tools_lists_names_and_required_arguments():
    result = invoke(["tools"])
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert "execute_luau" in output
    assert "start_stop_play" in output, "the second tools/list page never printed"
    assert "studio_id*" in output, "required arguments are not marked"


def test_tools_without_the_toggle_exits_not_ready():
    result = invoke(["tools", "--timeout", SILENT_SERVER_TIMEOUT], mode="no-tools")
    assert result.exit_code == EXIT_NOT_READY
    assert "Enable Studio as MCP server" in all_output(result)


def test_a_silent_tools_list_cannot_outlive_the_timeout():
    """`--timeout` is a promise about the command, not only about its last exchange.

    With Studio's MCP toggle off the proxy answers the handshake and then never
    answers `tools/list`, which every convenience command runs first. That call
    kept its own 30 s default whatever the caller asked for: measured before the
    fix, `luau 'return 1' --timeout 1` took 30.3 s.
    """
    started = time.monotonic()
    result = invoke(["luau", "return 1", "--timeout", SHORT_TIMEOUT], mode="no-tools")
    elapsed = time.monotonic() - started

    assert result.exit_code == EXIT_NOT_READY, all_output(result)
    assert "Enable Studio as MCP server" in all_output(result)
    assert elapsed < BOUNDED_COMMAND_SECONDS, f"--timeout 1 ran for {elapsed:.1f}s"


def test_a_handshake_that_is_never_answered_cannot_outlive_the_timeout():
    """The same promise, one exchange earlier: a proxy that takes `initialize` and stops.

    `start()` had a 15 s default of its own and no caller ever narrowed it, so
    `luau 'return 1' --timeout 1` took 18.1 s here. What is left is the second
    the caller asked for plus the grace the client spends reaping a child that
    ignores stdin EOF.
    """
    started = time.monotonic()
    result = invoke(["luau", "return 1", "--timeout", SHORT_TIMEOUT], mode="handshake-silent")
    elapsed = time.monotonic() - started

    assert result.exit_code == EXIT_NOT_READY, all_output(result)
    assert "no response to 'initialize'" in all_output(result)
    assert elapsed < BOUNDED_COMMAND_SECONDS, f"--timeout 1 ran for {elapsed:.1f}s"


@pytest.mark.parametrize("command", sorted(TIMEOUT_BOUNDED_COMMANDS))
def test_every_command_hands_its_timeout_to_the_handshake_and_the_listing(
    monkeypatch, tmp_path, command
):
    """The enumeration behind the two wall-clock tests above.

    Those prove the bound on one command against a silent proxy; this one reads
    the number each command actually handed the two calls, because the fault was
    per call site and a new command is one `client.list_tools()` away from
    reintroducing it.
    """
    handed: list[tuple[str, float]] = []
    original_start = StudioMcpClient.start
    original_list_tools = StudioMcpClient.list_tools

    def record_start(self, timeout=DEFAULT_INITIALIZE_TIMEOUT_SECONDS):
        handed.append(("start", timeout))
        return original_start(self, timeout)

    def record_list_tools(self, timeout=DEFAULT_LIST_TOOLS_TIMEOUT_SECONDS):
        handed.append(("list_tools", timeout))
        return original_list_tools(self, timeout)

    monkeypatch.setattr(StudioMcpClient, "start", record_start)
    monkeypatch.setattr(StudioMcpClient, "list_tools", record_list_tools)
    arguments = [
        part.replace(OUT_PLACEHOLDER, str(tmp_path / "capture.png"))
        for part in TIMEOUT_BOUNDED_COMMANDS[command]
    ]

    result = invoke([*arguments, "--timeout", SHORT_TIMEOUT])

    assert result.exit_code == EXIT_OK, all_output(result)
    assert [name for name, _ in handed] == ["start", "list_tools"], handed
    assert all(seconds <= float(SHORT_TIMEOUT) for _, seconds in handed), handed


def test_instances_lists_the_registered_studio():
    result = invoke(["instances"])
    assert result.exit_code == EXIT_OK
    assert "studio-1 (Baseplate)" in all_output(result)


def test_instances_with_none_open_exits_not_ready():
    """Nothing attached is an environment fault (1), not a malformed request (2)."""
    result = invoke(["instances", "--timeout", SHORT_TIMEOUT], mode="no-instances")
    assert result.exit_code == EXIT_NOT_READY
    assert "No Roblox Studio instance attached" in all_output(result)


def test_a_hostile_lister_payload_is_polled_through_rather_than_crashing_the_command():
    """The lister's answer gets a SECOND parse, and it had none of the guards.

    Two payloads, both legal inside the frame that carried them and neither
    readable once unwrapped: 200,000 open brackets, which the C decoder answers
    with `RecursionError`, and a 5,000-digit id, which it answers with the
    `ValueError` past Python's integer-conversion limit. The frame's own depth
    scan reads brackets inside a string literal as text, correctly, so it saw
    nothing to refuse and every command that resolves an instance died on the
    parse afterwards.

    The docstring promises an unparseable payload reads as "no instances", and
    the bridge here sends the real one on its third poll, so what this asserts
    is that the poll survived both and kept going.
    """
    for command in (["instances"], ["luau", "return 1"], ["state"]):
        result = invoke(command, mode="hostile-lister")
        assert result.exit_code == EXIT_OK, (command, all_output(result))
        assert "Traceback" not in all_output(result), command
    assert '"studio_id": "studio-1"' in all_output(invoke(["luau", "return 1"], mode="hostile-lister"))


def test_doctor_survives_a_hostile_lister_payload_and_still_renders_its_verdict():
    """`doctor` is the one that has to print a report even when a check fails.

    It died mid-report on the same two payloads, so the caller got a traceback
    where the four rows and the verdict belong.
    """
    result = invoke(["doctor"], mode="hostile-lister")

    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    for row in ("Binary:", "Handshake:", "Tools:", "Instances:"):
        assert row in output, (row, output)
    assert "CONNECTED (6 tools, 1 instance)" in output


def test_luau_fills_in_studio_id_and_the_default_context():
    """The point of the convenience command: the user types code, nothing else."""
    result = invoke(["luau", "return 1 + 1"])
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert '"code": "return 1 + 1"' in output
    assert '"datamodel_type": "Edit"' in output
    assert '"studio_id": "studio-1"' in output


def test_luau_honours_an_explicit_context():
    assert '"datamodel_type": "Server"' in all_output(
        invoke(["luau", "print(1)", "--context", "Server"])
    )


def test_luau_reads_source_from_a_file(tmp_path):
    script = tmp_path / "probe.luau"
    script.write_text("return workspace.Name")
    assert "return workspace.Name" in all_output(invoke(["luau", "--file", str(script)]))


def test_luau_reads_source_from_stdin():
    result = invoke(["luau", "-"], input="return 42")
    assert result.exit_code == EXIT_OK, all_output(result)
    assert "return 42" in all_output(result)


def test_luau_without_any_source_explains_the_three_ways():
    result = invoke(["luau"])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "stdin" in all_output(result)


def test_luau_with_an_unreadable_file_fails_before_reaching_studio(tmp_path):
    """No traceback, and no proxy spawned: input is validated first."""
    result = invoke(["luau", "--file", str(tmp_path / "missing.luau")])
    assert result.exit_code == EXIT_REQUEST_ERROR
    output = all_output(result)
    assert "cannot read" in output
    assert "Traceback" not in output


def test_luau_with_a_binary_file_is_a_clean_error(tmp_path):
    binary = tmp_path / "not-text.luau"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    result = invoke(["luau", "--file", str(binary)])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "Traceback" not in all_output(result)


def test_two_open_studios_require_a_choice():
    result = invoke(["luau", "print(1)"], mode="two-instances")
    assert result.exit_code == EXIT_REQUEST_ERROR
    output = all_output(result)
    assert "--studio" in output
    assert "studio-2 (Obby)" in output


def test_studio_option_accepts_a_place_name():
    output = all_output(invoke(["luau", "print(1)", "--studio", "Obby"], mode="two-instances"))
    assert '"studio_id": "studio-2"' in output


def test_studio_option_accepts_an_id():
    output = all_output(invoke(["luau", "print(1)", "--studio", "studio-1"], mode="two-instances"))
    assert '"studio_id": "studio-1"' in output


def test_unknown_studio_is_rejected_with_the_known_list():
    result = invoke(["luau", "print(1)", "--studio", "nope"], mode="two-instances")
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "Registered:" in all_output(result)


def test_luau_without_a_place_open_exits_not_ready():
    result = invoke(["luau", "print(1)", "--timeout", SHORT_TIMEOUT], mode="no-instances")
    assert result.exit_code == EXIT_NOT_READY
    assert "No Roblox Studio instance attached" in all_output(result)


def test_screenshot_writes_a_real_png(tmp_path):
    """Also proves the CLI filled in capture_id and studio_id: the fake server
    rejects a call that is missing either one."""
    destination = tmp_path / "capture.png"
    result = invoke(["screenshot", "--out", str(destination)])
    assert result.exit_code == EXIT_OK, all_output(result)
    assert str(destination) in all_output(result)
    assert destination.read_bytes().startswith(PNG_MAGIC_BYTES)


def test_screenshot_writes_the_file_even_in_json_mode(tmp_path):
    destination = tmp_path / "capture.png"
    result = invoke(["screenshot", "--out", str(destination), "--json"])
    assert result.exit_code == EXIT_OK
    assert '"mimeType":"image/png"' in result.output
    assert destination.exists()


def test_json_without_out_writes_no_file_the_payload_does_not_name():
    """The JSON carries the image; a temp file nobody is told about is an orphan.

    `--out` is the request for a file. Without it, `--json` used to write a
    temp capture whose path appeared nowhere in the JSON, so every such call
    left a file behind that only `ls /tmp` could find.
    """
    before = temporary_captures()
    result = invoke(["screenshot", "--json"])
    assert result.exit_code == EXIT_OK, all_output(result)
    assert '"mimeType":"image/png"' in result.output, "the payload has to be in the JSON"
    assert temporary_captures() == before, "a temp capture the JSON never names"


def test_the_advice_on_an_unwritable_result_is_a_command_that_works(tmp_path):
    """"Use --json" was a dead end while --json wrote the files too: exit 1 either way."""
    result = invoke(["screenshot", "--out", str(tmp_path / "shot.zzz"), "--json"], mode="odd-mime")
    assert result.exit_code == EXIT_NOT_READY
    advice = all_output(result)
    assert "--json and no --out" in advice, advice

    followed = invoke(["screenshot", "--json"], mode="odd-mime")
    assert followed.exit_code == EXIT_OK, all_output(followed)
    assert '"mimeType":"image/x-roblox-capture"' in followed.output


def test_screenshot_refuses_to_overwrite_without_force(tmp_path):
    destination = tmp_path / "capture.png"
    destination.write_text("precious")
    result = invoke(["screenshot", "--out", str(destination)])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "--force" in all_output(result)
    assert destination.read_text() == "precious"

    assert invoke(["screenshot", "--out", str(destination), "--force"]).exit_code == EXIT_OK
    assert destination.read_bytes().startswith(PNG_MAGIC_BYTES)


def test_a_failed_capture_writes_no_file(tmp_path):
    """isError means the call failed, whatever content came attached to it."""
    destination = tmp_path / "capture.png"
    result = invoke(["screenshot", "--out", str(destination)], mode="capture-error")
    assert result.exit_code == EXIT_NOT_READY
    assert not destination.exists(), "a failed call wrote its image anyway"
    assert "not written" in all_output(result), "the caller was not told the file is missing"


def test_a_failed_capture_does_not_spend_the_force_the_caller_gave_it(tmp_path):
    """`--force` licenses overwriting for a capture that worked, not for one that failed."""
    destination = tmp_path / "capture.png"
    destination.write_text("precious")
    result = invoke(["screenshot", "--out", str(destination), "--force"], mode="capture-error")
    assert result.exit_code == EXIT_NOT_READY
    assert destination.read_text() == "precious", "a failed call overwrote the named path"


def test_screenshot_into_a_directory_is_a_clean_request_error(tmp_path):
    result = invoke(["screenshot", "--out", str(tmp_path)])
    assert result.exit_code == EXIT_REQUEST_ERROR
    output = all_output(result)
    assert "is a directory" in output
    assert "Traceback" not in output


def test_screenshot_without_out_writes_a_temp_file():
    result = invoke(["screenshot"])
    assert result.exit_code == EXIT_OK, all_output(result)
    written = Path(all_output(result).strip().splitlines()[-1])
    try:
        assert written.read_bytes().startswith(PNG_MAGIC_BYTES)
    finally:
        written.unlink(missing_ok=True)


def test_state_reports_the_open_place():
    assert "Baseplate" in all_output(invoke(["state"]))


def test_play_start_sends_the_boolean():
    assert "is_start=true" in all_output(invoke(["play", "--start"]))


def test_play_stop_sends_the_boolean():
    assert "is_start=false" in all_output(invoke(["play", "--stop"]))


@pytest.mark.parametrize("flags", [[], ["--start", "--stop"]])
def test_play_demands_exactly_one_direction(flags):
    result = invoke(["play", *flags])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "exactly one" in all_output(result)


def test_call_passes_raw_arguments_through():
    result = invoke(
        ["call", "execute_luau", "--args", '{"code": "print(1)", "datamodel_type": "Client"}']
    )
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert '"datamodel_type": "Client"' in output
    assert '"studio_id": "studio-1"' in output, "studio_id was not filled in for a raw call"


def test_a_tool_that_reports_failure_exits_not_ready():
    """`isError` is the tool failing its own job: the environment, not the request."""
    result = invoke(["call", "boom_tool"])
    assert result.exit_code == EXIT_NOT_READY
    assert "blew up" in all_output(result)


def test_a_server_side_rpc_error_exits_not_ready():
    result = invoke(["call", "execute_luau", "--args", '{"code": "x"}'], mode="malformed")
    assert result.exit_code == EXIT_NOT_READY
    assert "Traceback" not in all_output(result)


def test_call_rejects_an_unknown_tool_and_lists_the_real_ones():
    result = invoke(["call", "no_such_tool"])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "execute_luau" in all_output(result)


def test_call_rejects_malformed_args_json():
    result = invoke(["call", "execute_luau", "--args", "{oops}"])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "valid JSON" in all_output(result)


def test_missing_binary_is_reported_by_doctor(tmp_path):
    result = runner.invoke(
        app, ["doctor"], env={"ROBLOX_STUDIO_MCP_BIN": str(tmp_path / "gone" / "StudioMCP")}
    )
    assert result.exit_code == EXIT_NOT_READY
    output = all_output(result)
    assert "MISSING" in output
    assert "ROBLOX_STUDIO_MCP_BIN" in output


def test_a_hostile_tool_name_cannot_forge_a_row_or_a_verdict():
    """Every printable field of a tool is server-controlled, and rows are fixed-width."""
    result = invoke(["tools"], mode="hostile-names")
    assert result.exit_code == EXIT_OK, all_output(result)
    lines = all_output(result).splitlines()
    assert any(line.startswith("sneaky tool_name") for line in lines), lines
    assert not any(line.startswith("CONNECTED") for line in lines), "a forged verdict line printed"
    assert "\x1b" not in all_output(result), "an escape sequence reached the terminal"
    assert any("args: argument name" in line for line in lines), lines


def test_doctor_caps_the_fields_whose_length_the_server_chooses():
    """Length is an attack all by itself: 100 KB of clean x scrolls the report away."""
    result = invoke(["doctor"], mode="giant-names")
    assert result.exit_code == EXIT_OK, all_output(result)[:500]
    output = all_output(result)

    assert "..." in output, "nothing was truncated"
    longest = max(len(line) for line in output.splitlines())
    # Both capped fields, the label, the ` (name)` brackets, and the attach time
    # the Instances row carries when the fake took a measurable moment to attach.
    row_chrome = len("Instances: ") + len(" ()") + len(" after 12.3s")
    assert longest <= MAX_DIAGNOSTIC_TEXT_CHARS * 2 + row_chrome, longest
    assert len(output) < 1500, "the server's own text reached the terminal at its own length"


def test_the_tool_rows_cap_the_name_and_the_argument_list_like_every_other_row():
    """A tool row is chrome the server sizes: its name, and how many arguments it lists.

    The row was folded onto one line and never capped, so a 100 KB tool name
    printed in full, and a schema with forty arguments printed forty of them.
    """
    result = invoke(["tools"], mode="giant-names")
    assert result.exit_code == EXIT_OK, all_output(result)[:500]
    output = all_output(result)

    assert "t" * (MAX_DIAGNOSTIC_TEXT_CHARS + 1) not in output, "the tool name printed in full"
    assert "and 21 more" in output, "the argument list ran to the schema's length"
    assert "arg39" not in output
    assert len(output) < 2000, "the listing is longer than a screen"


def test_a_crowded_build_cannot_bury_the_advice_under_its_own_tool_list(tmp_path):
    """46 tools is already too many to print; a build with hundreds is the real case."""
    result = invoke(["call", "no_such_tool"], mode="crowded")
    assert result.exit_code == EXIT_REQUEST_ERROR
    output = all_output(result)
    assert "and 26 more" in output, output
    assert "insert_asset_039" not in output
    assert len(output) < 1000, output


def test_a_crowd_of_registered_studios_is_listed_only_as_far_as_it_helps():
    result = invoke(["luau", "print(1)"], mode="crowded")
    assert result.exit_code == EXIT_REQUEST_ERROR
    output = all_output(result)
    assert "40 Studio instances are registered" in output, "the real count was dropped"
    assert "and 20 more" in output
    assert "studio-39" not in output


def test_a_server_info_that_is_not_an_object_still_reaches_a_verdict():
    """`"serverInfo": "x"` passed `or {}` and then met `.get` two modules later.

    The AttributeError came out through the catch-all, so the report printed
    three of its four rows and no verdict at all, for a Studio that was fine.
    """
    result = invoke(["doctor"], mode="odd-serverinfo")
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert "malformed serverInfo" in output
    assert "CONNECTED (6 tools, 1 instance)" in output
    assert "AttributeError" not in output


def test_a_lone_surrogate_does_not_cost_the_report_its_verdict():
    """`"\\udcff"` is legal JSON and unencodable as UTF-8, so printing it raised.

    The doctor prints its rows before its verdict, so the exception landed
    between them: three rows, no verdict, exit 1, against a Studio that was fine.
    """
    result = invoke(["doctor"], mode="lone-surrogate")
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert "FakeStudio 1.0.0" in output, "the server name lost more than the surrogate"
    assert "studio-1 (Baseplate)" in output
    assert "CONNECTED (6 tools, 1 instance)" in output
    assert "UnicodeEncodeError" not in output


def test_a_lone_surrogate_in_a_tool_answer_still_prints_the_answer():
    result = invoke(["luau", "return 1"], mode="lone-surrogate")
    assert result.exit_code == EXIT_OK, all_output(result)
    assert "luau ok (surrogate)" in all_output(result)


def test_doctor_json_still_carries_the_full_field():
    """--json is for a consumer, not a terminal: it gets the bytes as sent."""
    report = json.loads(invoke(["doctor", "--json"], mode="giant-names").output)
    assert len(report["server_info"]["name"]) > MAX_DIAGNOSTIC_TEXT_CHARS


def test_the_app_never_renders_a_traceback_or_its_locals():
    """A rendered traceback would print the frames, and with locals the bytes inside them."""
    assert app.pretty_exceptions_enable is False
    assert app.pretty_exceptions_show_locals is False


def test_an_unexpected_exception_becomes_one_sanitised_line(monkeypatch, capsys):
    """Whatever slips past a command handler exits 1 with a line, not a stack."""
    def explode():
        raise RuntimeError("the proxy said \x1b]0;pwned\x07boom")

    monkeypatch.setattr(main_module, "app", explode)
    with pytest.raises(SystemExit) as exited:
        main_module.main()

    assert exited.value.code == EXIT_NOT_READY
    printed = capsys.readouterr().err
    assert printed.splitlines() == ["error: RuntimeError: the proxy said boom"]


def test_an_ordinary_exit_still_passes_through_the_catch_all(monkeypatch):
    """`typer.Exit` and `SystemExit` are not Exception subclasses, so exit codes survive."""
    monkeypatch.setattr(main_module, "app", lambda: (_ for _ in ()).throw(SystemExit(EXIT_OK)))
    with pytest.raises(SystemExit) as exited:
        main_module.main()
    assert exited.value.code == EXIT_OK


@pytest.fixture
def recorded_caffeinate(monkeypatch):
    """Record every `caffeinate` the wake helper asks for, and launch none of them.

    The seam is `start_caffeinate` rather than `subprocess.Popen`: the client
    spawns the proxy through the same module object, so patching Popen globally
    would take the bridge down with it.
    """
    launched: list[FakeCaffeinate] = []

    def record(arguments):
        launched.append(FakeCaffeinate(arguments))
        return launched[-1]

    monkeypatch.setattr(display_wake_module, "start_caffeinate", record)
    monkeypatch.setattr(display_wake_module.platform, "system", lambda: "Darwin")
    return launched


def test_a_non_finite_timeout_is_refused_before_it_poisons_a_deadline():
    """Click parses nan and inf; every comparison against NaN is False after that."""
    for value in ("nan", "inf", "-inf"):
        result = invoke(["luau", "print(1)", "--timeout", value])
        assert result.exit_code == EXIT_REQUEST_ERROR, value
        assert "finite" in all_output(result), value


def test_a_non_positive_timeout_is_refused_instead_of_misdiagnosing_the_bridge():
    """A deadline already in the past makes the first select() look like a fault.

    Measured against the fake server: `luau --timeout 0` reported "the Studio
    MCP proxy stopped reading its input", and `doctor --timeout -1` printed NOT
    CONNECTED, both against a bridge that was answering normally.
    """
    for value in ("0", "-1", "-0.5"):
        result = invoke(["luau", "print(1)", "--timeout", value])
        assert result.exit_code == EXIT_REQUEST_ERROR, value
        assert "positive" in all_output(result), value

    doctored = invoke(["doctor", "--timeout", "0"])
    assert doctored.exit_code == EXIT_REQUEST_ERROR
    assert "NOT CONNECTED" not in all_output(doctored), "a false verdict for a working bridge"


def test_a_luau_file_that_is_not_a_regular_file_is_refused(tmp_path):
    """Reading a FIFO blocks forever, and it passes every exists() check on the way."""
    fifo = tmp_path / "script.luau"
    os.mkfifo(fifo)
    result = invoke(["luau", "--file", str(fifo)])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "not a regular file" in all_output(result)


def test_an_oversized_luau_file_is_refused_with_its_size(tmp_path):
    script = tmp_path / "huge.luau"
    script.write_bytes(b"-" * (MAX_LUAU_SOURCE_BYTES + 1))
    result = invoke(["luau", "--file", str(script)])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "capped at" in all_output(result)


def test_oversized_luau_on_stdin_is_refused_the_same_way():
    """Same script, different door: `-` read the whole stream while --file said no."""
    result = invoke(["luau", "-"], input="-" * (MAX_LUAU_SOURCE_BYTES + 1))
    assert result.exit_code == EXIT_REQUEST_ERROR, all_output(result)[:300]
    assert "capped" in all_output(result)


def test_a_capture_that_never_answers_names_the_sleeping_display(tmp_path):
    """Studio accepts the call and goes quiet, which otherwise reads as a broken bridge."""
    result = invoke(
        ["screenshot", "--out", str(tmp_path / "shot.png"), "--timeout", SHORT_TIMEOUT],
        mode="capture-silent",
    )
    assert result.exit_code == EXIT_NOT_READY
    assert "--wake-display" in all_output(result)


def test_the_sleeping_display_hint_is_dropped_once_the_flag_was_used(tmp_path, recorded_caffeinate):
    """Having already ruled the display out, repeating the advice is noise."""
    result = invoke(
        ["screenshot", "--wake-display", "--out", str(tmp_path / "shot.png"),
         "--timeout", SHORT_TIMEOUT],
        mode="capture-silent",
    )
    assert result.exit_code == EXIT_NOT_READY
    assert "rerun with --wake-display" not in all_output(result)


def test_wake_display_asserts_user_activity_and_holds_the_display(tmp_path, recorded_caffeinate):
    destination = tmp_path / "capture.png"
    result = invoke(["screenshot", "--wake-display", "--out", str(destination)])
    assert result.exit_code == EXIT_OK, all_output(result)
    assert destination.read_bytes().startswith(PNG_MAGIC_BYTES)

    assert [item.arguments for item in recorded_caffeinate] == [
        ["-u", "-t", str(display_wake_module.DISPLAY_WAKE_SECONDS)],
        ["-d", "-w", str(os.getpid()), "-t", str(display_wake_module.DISPLAY_HOLD_FLOOR_SECONDS)],
    ]
    assert all(item.terminated for item in recorded_caffeinate), "a caffeinate was left running"


def test_wake_display_off_macos_warns_and_still_captures(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(display_wake_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(display_wake_module, "start_caffeinate", lambda a: launched.append(a))

    destination = tmp_path / "capture.png"
    result = invoke(["screenshot", "--wake-display", "--out", str(destination)])
    assert result.exit_code == EXIT_OK, all_output(result)
    assert destination.exists()
    assert "not macOS" in all_output(result)
    assert launched == [], "caffeinate was launched off macOS"


def test_json_output_is_one_compact_line():
    """`indent=2` is quadratic in nesting depth, and the depth is the server's choice.

    A 4 KB frame nested 2,000 deep printed 8 MB of `--json`, and the same shape
    at 20,000 deep printed 800 MB: two spaces per level, on every line. Compact
    separators make the output the size of the payload, and `jq` is there for
    anyone who wants it indented.
    """
    result = invoke(["doctor", "--json"])
    assert result.exit_code == EXIT_OK, all_output(result)
    payload = result.output.strip()

    assert "\n" not in payload, "the payload is indented, so its size grows with its depth"
    assert '"ok":true' in payload
    assert json.loads(payload)["tool_count"] == 6


@pytest.mark.parametrize("arguments", [["luau", "-- a comment"], ["luau", "--", "-- a comment"]])
def test_luau_source_may_start_with_a_dash(arguments):
    """A Luau comment is `-- text`, which an option parser reads as a flag.

    Before this, `roblox-studio luau '-- a comment'` answered "No such option:
    -- a comment" and the way through (`--`) was documented nowhere. Both forms
    work now, and the `--` one is what the help and the README point at, since
    it is the form that cannot be mistaken for anything.
    """
    result = invoke(arguments)
    assert result.exit_code == EXIT_OK, all_output(result)
    assert '"code": "-- a comment"' in all_output(result)


@pytest.mark.parametrize("typo", ["--fiel", "-x", "--jsonn"])
def test_a_mistyped_option_is_not_quietly_run_as_a_script(typo):
    """The cost of accepting unknown options as source, paid back at the door.

    A flag this CLI does not have would otherwise arrive as the script Studio
    runs. Source that really starts with a dash has a space or a newline in it;
    a mistyped flag does not, which is the whole of the test.
    """
    result = invoke(["luau", typo])
    assert result.exit_code == EXIT_REQUEST_ERROR, all_output(result)
    advice = all_output(result)
    assert "mistyped option" in advice
    assert "--file" in advice, "the message has to name a way through"


def test_an_unreadable_out_path_is_answered_by_the_code_that_writes_it(tmp_path):
    """`--out` is written, never read, so "is not readable" was never the fault.

    Typer builds a `click.Path` for a `Path` parameter, and its `readable`
    check runs on any path that exists, so `--out` at mode 0o000 failed in the
    parser with a reason that had nothing to do with the call. The refusals
    about this path belong to `image_output`, which knows which of them `--force`
    answers.
    """
    target = tmp_path / "locked.png"
    target.write_bytes(b"the previous capture")
    target.chmod(0o000)

    refused = invoke(["screenshot", "--out", str(target)])
    assert refused.exit_code == EXIT_REQUEST_ERROR, all_output(refused)
    assert "already exists" in all_output(refused)
    assert "is not readable" not in all_output(refused)

    forced = invoke(["screenshot", "--out", str(target), "--force"])
    assert forced.exit_code == EXIT_OK, all_output(forced)
    target.chmod(0o600)
    assert target.read_bytes().startswith(PNG_MAGIC_BYTES)


def test_a_tool_name_in_a_missing_argument_message_is_capped():
    """The name is chrome in front of the sentence naming what the tool lacks.

    Every other server-chosen identifier prints capped, and this one did not, so
    a build that names a tool in 100 KB pushed the words the caller needs off
    the screen. The declared list is capped in count by `declared_arguments`.
    """
    giant = ToolDefinition(name="t" * 500_000, description="", input_schema={})

    message = main_module.missing_argument_message(giant, "start/stop argument")

    assert len(message) < MAX_DIAGNOSTIC_TEXT_CHARS * 2, len(message)
    assert "start/stop argument" in message
