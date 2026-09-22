"""End-to-end tests for the `roblox-studio` command surface.

These drive the real Typer app through Click's `CliRunner`, with
`ROBLOX_STUDIO_MCP_BIN` pointed at the fake server, so they cover the path a
user actually takes: parse flags, discover tools, wait for the Studio instance,
call, print, exit. `FAKE_STUDIO_MODE` selects which situation to simulate.

Exit codes are the contract worth protecting, so they are asserted everywhere:
0 success, 1 the environment is not ready, 2 the request was malformed.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from roblox_studio_cli.main import EXIT_NOT_READY, EXIT_OK, EXIT_REQUEST_ERROR, app

FAKE_SERVER_PATH = Path(__file__).resolve().parent / "fake_studio_mcp_server.py"
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"
# Short enough to keep the suite quick: the attach wait is bounded by --timeout.
SHORT_TIMEOUT = "1"
SILENT_SERVER_TIMEOUT = "2"

runner = CliRunner()


def invoke(arguments: list[str], mode: str = "connected", **kwargs):
    """Run the CLI against the fake server in `mode`, as a user would from a shell."""
    return runner.invoke(
        app,
        arguments,
        env={"ROBLOX_STUDIO_MCP_BIN": str(FAKE_SERVER_PATH), "FAKE_STUDIO_MODE": mode},
        **kwargs,
    )


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


def test_instances_lists_the_registered_studio():
    result = invoke(["instances"])
    assert result.exit_code == EXIT_OK
    assert "studio-1 (Baseplate)" in all_output(result)


def test_instances_with_none_open_exits_not_ready():
    """Nothing attached is an environment fault (1), not a malformed request (2)."""
    result = invoke(["instances", "--timeout", SHORT_TIMEOUT], mode="no-instances")
    assert result.exit_code == EXIT_NOT_READY
    assert "No Roblox Studio instance attached" in all_output(result)


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
    assert '"mimeType": "image/png"' in result.output
    assert destination.exists()


def test_screenshot_refuses_to_overwrite_without_force(tmp_path):
    destination = tmp_path / "capture.png"
    destination.write_text("precious")
    result = invoke(["screenshot", "--out", str(destination)])
    assert result.exit_code == EXIT_REQUEST_ERROR
    assert "--force" in all_output(result)
    assert destination.read_text() == "precious"

    assert invoke(["screenshot", "--out", str(destination), "--force"]).exit_code == EXIT_OK
    assert destination.read_bytes().startswith(PNG_MAGIC_BYTES)


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
