"""Tests for the doctor verdict, which is the one line people act on.

The three failures need three different fixes, so they must never collapse into
one message: no tools at all is the MCP toggle, tools but no instance is Studio
not having attached (or no place open), and tools that arrive empty is a
Studio-side fault that neither of those explains.
"""

from roblox_studio_cli.doctor_report import DoctorReport, doctor_verdict
from roblox_studio_cli.instance_discovery import StudioInstance

BINARY = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"


def build_report(**overrides) -> DoctorReport:
    """A report that is healthy except for whatever the test breaks."""
    fields = {
        "binary_path": BINARY,
        "binary_exists": True,
        "server_info": {"name": "RobloxStudio", "version": "1.0.0"},
        "tools_available": True,
        "tool_count": 28,
        "instances": [StudioInstance("studio-1", "Place1")],
    }
    fields.update(overrides)
    return DoctorReport(**fields)


def test_a_reachable_studio_reads_as_connected():
    report = build_report()
    assert report.ok is True
    assert doctor_verdict(report) == "CONNECTED (28 tools, 1 instance)"


def test_two_instances_are_counted_in_the_plural():
    report = build_report(
        instances=[StudioInstance("studio-1", "Place1"), StudioInstance("studio-2", "Obby")]
    )
    assert doctor_verdict(report) == "CONNECTED (28 tools, 2 instances)"


def test_no_tools_at_all_blames_the_connection():
    report = build_report(tools_available=False, tool_count=0, instances=[])
    assert report.ok is False
    assert doctor_verdict(report) == "NOT CONNECTED"


def test_tools_but_no_instance_blames_the_attach():
    report = build_report(instances=[])
    assert "no Studio instance attached" in doctor_verdict(report)
    assert doctor_verdict(report).startswith("NOT READY")


def test_an_empty_tool_list_says_so_rather_than_blaming_the_attach():
    """Studio answered, and answered with nothing. Neither of the other two faults."""
    report = build_report(tool_count=0, instances=[])
    assert doctor_verdict(report) == "NOT READY (bridge is up, Studio exposes no tools)"


def test_the_json_shape_keeps_the_keys_a_script_gates_on():
    payload = build_report(attach_seconds=3.2).as_dict()
    assert payload["ok"] is True
    assert payload["instances"] == [{"id": "studio-1", "name": "Place1"}]
    assert payload["attach_seconds"] == 3.2
    assert payload["problem"] is None
