"""The `doctor` health check: gather the four facts, then render them.

The bridge has two halves that fail separately, and the whole point of this
report is to say which half is missing:

- The proxy binary plus the MCP handshake. Broken here means a Studio update
  moved the binary, or `ROBLOX_STUDIO_MCP_BIN` points at the wrong thing.
- Studio itself. Tools never arriving means the "Enable Studio as MCP server"
  toggle is off (NOT CONNECTED). Tools arriving but no instance registering
  means Studio has not attached yet or has no place open (NOT READY).

Checks run in that order and stop at the first failure, so the report names one
cause rather than a cascade. The verdict goes to stdout because it is the
answer; the remediation goes to stderr so a script can read the verdict without
parsing advice.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import typer

from roblox_studio_cli.client import (
    STUDIO_MCP_BINARY_ENV_VAR,
    STUDIO_NOT_ENABLED_MESSAGE,
    StudioMcpClient,
    resolve_studio_binary_path,
)
from roblox_studio_cli.discovery import (
    StudioInstance,
    attach_failure_message,
    wait_for_studio_instances,
)
from roblox_studio_cli.errors import StudioMcpError, StudioNotConnectedError
from roblox_studio_cli.terminal import (
    echo_server_text,
    sanitize_diagnostic_line,
    sanitize_terminal_text,
)

DOCTOR_LABEL_COLUMN_WIDTH = 11
# The handshake worked and the server described itself in a shape nobody can
# read. Worth saying out loud, and not worth failing the report over.
MALFORMED_SERVER_INFO_ROW = "(malformed serverInfo; --json has it as sent)"
NO_TOOLS_MESSAGE = (
    "Studio answered the handshake but exposes no tools. That is a Studio-side "
    "fault rather than a toggle: reopen the place, or restart Studio."
)


@dataclass
class DoctorReport:
    """Everything `doctor` learned, in check order. `problem` holds the first fault."""

    binary_path: str
    binary_exists: bool
    # Whatever the handshake called serverInfo, kept exactly as sent: an object
    # in the ordinary case, and `--json` carries whatever it really was. The
    # renderer is the one place that has to survive the other shapes.
    server_info: object = None
    tools_available: bool = False
    tool_count: int = 0
    instances: list[StudioInstance] = field(default_factory=list)
    attach_seconds: float | None = None
    problem: str = ""

    @property
    def ok(self) -> bool:
        """True only when a Studio instance is actually reachable."""
        return bool(self.instances)

    def as_dict(self) -> dict:
        """The `--json` shape. Stable enough for a script to gate on `ok`."""
        return {
            "binary": self.binary_path,
            "binary_exists": self.binary_exists,
            "server_info": self.server_info,
            "tools_available": self.tools_available,
            "tool_count": self.tool_count,
            "instances": [
                {"id": instance.identifier, "name": instance.name}
                for instance in self.instances
            ],
            "attach_seconds": self.attach_seconds,
            "problem": self.problem or None,
            "ok": self.ok,
        }


def gather_doctor_report(timeout: float) -> DoctorReport:
    """Run every check, stopping at the first thing that is wrong."""
    binary_path = resolve_studio_binary_path()
    report = DoctorReport(binary_path=binary_path, binary_exists=Path(binary_path).exists())
    if not report.binary_exists:
        report.problem = (
            f"Studio MCP binary not found at {binary_path}. It ships inside "
            f"RobloxStudio.app, so a Studio update can move it; set {STUDIO_MCP_BINARY_ENV_VAR} "
            "to its current path."
        )
        return report

    client = StudioMcpClient()
    try:
        handshake = client.start()
        # Kept exactly as sent, including the shapes that are not an object:
        # `handshake_row` is where reading it safely belongs, and `--json` wants
        # what the server really answered.
        report.server_info = handshake.get("serverInfo")
        tool_definitions = client.list_tools(timeout=timeout)
        report.tools_available = True
        report.tool_count = len(tool_definitions)
        if not tool_definitions:
            report.problem = NO_TOOLS_MESSAGE
            return report

        outcome = wait_for_studio_instances(client, tool_definitions, timeout=timeout)
        report.instances = outcome.instances
        report.attach_seconds = round(outcome.elapsed_seconds, 2)
        if not outcome.attached:
            report.problem = attach_failure_message(outcome)
    except StudioNotConnectedError:
        report.problem = STUDIO_NOT_ENABLED_MESSAGE
    except StudioMcpError as client_error:
        report.problem = sanitize_terminal_text(str(client_error))
    finally:
        client.close()
    return report


def render_doctor_report(report: DoctorReport, as_json: bool) -> None:
    """Print the report as aligned text, or as its `--json` dict."""
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
        return

    width = DOCTOR_LABEL_COLUMN_WIDTH
    typer.echo(f"{'Binary:':<{width}}{report.binary_path} "
               f"({'found' if report.binary_exists else 'MISSING'})")

    typer.echo(f"{'Handshake:':<{width}}{handshake_row(report.server_info)}")

    if report.tools_available:
        typer.echo(f"{'Tools:':<{width}}{report.tool_count} available")
    else:
        typer.echo(f"{'Tools:':<{width}}not available")

    if report.instances:
        attached = f" after {report.attach_seconds:.1f}s" if report.attach_seconds else ""
        echo_server_text(f"{'Instances:':<{width}}{report.instances[0].describe()}{attached}")
        for instance in report.instances[1:]:
            echo_server_text(f"{'':<{width}}{instance.describe()}")
    else:
        typer.echo(f"{'Instances:':<{width}}none registered")

    typer.echo(f"\n{doctor_verdict(report)}")
    if report.problem:
        echo_server_text(report.problem, err=True)


def handshake_row(server_info: object) -> str:
    """What the server called itself, or why that cannot be shown.

    The spec says an object with a name and a version, and nothing makes a
    server send one: a bare string here used to reach `.get` and take the whole
    report down with an AttributeError, verdict included. Both fields print as
    one row, so both are folded and capped; `--json` still carries them in full.
    """
    if not server_info:
        return "no response"
    if not isinstance(server_info, dict):
        return MALFORMED_SERVER_INFO_ROW
    name = sanitize_diagnostic_line(str(server_info.get("name", "unknown")))
    version = sanitize_diagnostic_line(str(server_info.get("version", "?")))
    return f"{name} {version}"


def doctor_verdict(report: DoctorReport) -> str:
    """One line naming which half of the bridge is missing."""
    if report.ok:
        plural = "" if len(report.instances) == 1 else "s"
        return f"CONNECTED ({report.tool_count} tools, {len(report.instances)} instance{plural})"
    if report.tools_available and report.tool_count == 0:
        return "NOT READY (bridge is up, Studio exposes no tools)"
    if report.tools_available:
        return "NOT READY (bridge is up, no Studio instance attached)"
    return "NOT CONNECTED"
