"""End-to-end tests for the `roblox-studio` command surface.

These drive the real Typer app through Click's `CliRunner`, with
`ROBLOX_STUDIO_MCP_BIN` pointed at the fake server, so they cover the path a
user actually takes: parse flags, discover tools, resolve the Studio instance,
call, print, exit. `FAKE_STUDIO_MODE` selects which Studio situation to
simulate (one instance, none, two, or an unenabled toggle).
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from roblox_studio_cli.discovery import NO_STUDIO_INSTANCE_MESSAGE
from roblox_studio_cli.main import EXIT_NOT_READY, EXIT_OK, EXIT_USAGE_ERROR, app

FAKE_SERVER_PATH = Path(__file__).resolve().parent / "fake_studio_mcp_server.py"
SILENT_SERVER_TIMEOUT = "2"
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"

runner = CliRunner()


def invoke(arguments: list[str], mode: str = "connected"):
    """Run the CLI against the fake server in `mode`, as a user would from a shell."""
    return runner.invoke(
        app,
        arguments,
        env={"ROBLOX_STUDIO_MCP_BIN": str(FAKE_SERVER_PATH), "FAKE_STUDIO_MODE": mode},
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


def test_doctor_without_a_place_open_fails_with_the_open_a_place_advice():
    result = invoke(["doctor"], mode="no-instances")
    assert result.exit_code == EXIT_NOT_READY
    output = all_output(result)
    assert "none registered" in output
    # The bridge is up, so the verdict must not blame the connection.
    assert "NOT READY" in output
    assert "Open a place in Studio" in output


def test_doctor_without_the_toggle_names_the_toggle():
    result = invoke(["doctor", "--timeout", SILENT_SERVER_TIMEOUT], mode="no-tools")
    assert result.exit_code == EXIT_NOT_READY
    output = all_output(result)
    assert "Tools:" in output and "not available" in output
    assert "Manage MCP Servers" in output


def test_doctor_json_carries_the_verdict():
    import json

    result = invoke(["doctor", "--json"])
    report = json.loads(result.output)
    assert report["ok"] is True
    assert report["tool_count"] == 6
    assert report["instances"] == [{"id": "studio-1", "name": "Baseplate"}]


def test_tools_lists_names_and_required_arguments():
    result = invoke(["tools"])
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert "execute_luau" in output
    assert "start_stop_play" in output, "the second tools/list page never printed"
    assert "studio_id*" in output, "required arguments are not marked"


def test_tools_without_the_toggle_exits_one():
    result = invoke(["tools", "--timeout", SILENT_SERVER_TIMEOUT], mode="no-tools")
    assert result.exit_code == EXIT_NOT_READY
    assert "Enable Studio as MCP server" in all_output(result)


def test_instances_lists_the_registered_studio():
    result = invoke(["instances"])
    assert result.exit_code == EXIT_OK
    assert "studio-1 (Baseplate)" in all_output(result)


def test_instances_with_none_open_exits_two():
    result = invoke(["instances"], mode="no-instances")
    assert result.exit_code == EXIT_USAGE_ERROR
    assert NO_STUDIO_INSTANCE_MESSAGE in all_output(result)


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
    output = all_output(invoke(["luau", "--file", str(script)]))
    assert "return workspace.Name" in output


def test_luau_reads_source_from_stdin():
    result = runner.invoke(
        app,
        ["luau", "-"],
        input="return 42",
        env={"ROBLOX_STUDIO_MCP_BIN": str(FAKE_SERVER_PATH), "FAKE_STUDIO_MODE": "connected"},
    )
    assert result.exit_code == EXIT_OK, all_output(result)
    assert "return 42" in all_output(result)


def test_luau_without_any_source_explains_the_three_ways():
    result = invoke(["luau"])
    assert result.exit_code == EXIT_USAGE_ERROR
    assert "stdin" in all_output(result)


def test_two_open_studios_require_a_choice():
    result = invoke(["luau", "print(1)"], mode="two-instances")
    assert result.exit_code == EXIT_USAGE_ERROR
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
    assert result.exit_code == EXIT_USAGE_ERROR
    assert "Registered:" in all_output(result)


def test_luau_without_a_place_open_says_to_open_one():
    result = invoke(["luau", "print(1)"], mode="no-instances")
    assert result.exit_code == EXIT_USAGE_ERROR
    assert "Open a place in Studio" in all_output(result)


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


def test_state_reports_the_open_place():
    assert "Baseplate" in all_output(invoke(["state"]))


def test_play_start_sends_the_boolean():
    assert "is_start=true" in all_output(invoke(["play", "--start"]))


def test_play_stop_sends_the_boolean():
    assert "is_start=false" in all_output(invoke(["play", "--stop"]))


@pytest.mark.parametrize("flags", [[], ["--start", "--stop"]])
def test_play_demands_exactly_one_direction(flags):
    result = invoke(["play", *flags])
    assert result.exit_code == EXIT_USAGE_ERROR
    assert "exactly one" in all_output(result)


def test_call_passes_raw_arguments_through():
    result = invoke(
        ["call", "execute_luau", "--args", '{"code": "print(1)", "datamodel_type": "Client"}']
    )
    assert result.exit_code == EXIT_OK, all_output(result)
    output = all_output(result)
    assert '"datamodel_type": "Client"' in output
    assert '"studio_id": "studio-1"' in output, "studio_id was not filled in for a raw call"


def test_call_reports_a_failing_tool_with_a_non_zero_exit():
    result = invoke(["call", "boom_tool"])
    assert result.exit_code == EXIT_NOT_READY
    assert "blew up" in all_output(result)


def test_call_rejects_an_unknown_tool_and_lists_the_real_ones():
    result = invoke(["call", "no_such_tool"])
    assert result.exit_code == EXIT_USAGE_ERROR
    assert "execute_luau" in all_output(result)


def test_call_rejects_malformed_args_json():
    result = invoke(["call", "execute_luau", "--args", "{oops}"])
    assert result.exit_code == EXIT_USAGE_ERROR
    assert "valid JSON" in all_output(result)


def test_missing_binary_is_reported_by_doctor(tmp_path):
    result = runner.invoke(
        app, ["doctor"], env={"ROBLOX_STUDIO_MCP_BIN": str(tmp_path / "gone" / "StudioMCP")}
    )
    assert result.exit_code == EXIT_NOT_READY
    output = all_output(result)
    assert "MISSING" in output
    assert "ROBLOX_STUDIO_MCP_BIN" in output
