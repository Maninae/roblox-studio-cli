"""Runtime discovery, half two: which Studio instance a tool call is aimed at.

Every Studio tool except the instance lister takes a required `studio_id`, so a
convenience command has to answer two questions and this module owns the second
one. It asks `tool_discovery` for exactly one thing, the live instance lister,
and does the rest itself: poll that lister until Studio registers, read its
payload into `StudioInstance` records, and resolve `--studio` (or
`ROBLOX_STUDIO_ID`, or the single open place) against them.

Two facts drive the shape of it:

- Studio does not attach to a client the instant that client connects. For the
  first few seconds the lister answers with an empty list or an "unable to reach
  Roblox Studio" error, both of which mean "not yet", so
  `wait_for_studio_instances` polls rather than reading once and giving up.
- Nothing may be cached between runs. Instances open and close between commands
  and ids do not survive a Studio restart, so every command resolves afresh.

Its answer arrives as JSON inside a JSON frame, which makes reading it a SECOND
parse of server bytes, with none of the guards the first one ran: brackets in
the lister's text are a string literal to `framing`'s depth scan and structure
to this one. So `parse_studio_instances` runs those guards itself.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field

from roblox_studio_cli.client import DEFAULT_CALL_TOOL_TIMEOUT_SECONDS, StudioMcpClient
from roblox_studio_cli.errors import StudioMcpTimeoutError, StudioNotAttachedError
from roblox_studio_cli.framing import (
    MAX_FRAME_CONTAINER_DEPTH,
    exceeds_container_depth,
    refuse_json_constant,
    refuse_non_finite_number,
)
from roblox_studio_cli.mcp_payloads import ToolDefinition
from roblox_studio_cli.terminal import (
    capped_display_names,
    sanitize_diagnostic_line,
    sanitize_terminal_text,
    truncate_display_text,
)
from roblox_studio_cli.tool_discovery import (
    LIST_INSTANCES_INTENT,
    ToolDiscoveryError,
    find_tool,
)

logger = logging.getLogger(__name__)

STUDIO_ID_ARGUMENT_NAME = "studio_id"
STUDIO_ID_ENV_VAR = "ROBLOX_STUDIO_ID"

STUDIO_INSTANCE_ID_KEYS = ("studio_id", "id", "studioId", "instance_id")
STUDIO_INSTANCE_NAME_KEYS = ("name", "place_name", "placeName", "title", "game_name")
STUDIO_INSTANCE_LIST_KEYS = ("studios", "instances", "roblox_studios")

# Measured against Studio 0.739 on 2026-09-22: three fresh client sessions saw
# their instance appear 2.75 s, 2.87 s and 3.38 s after connecting, with the
# bridge answering "no studios" (or an "unable to reach Studio" error) until
# then. Earlier runs attached in 1.1 s and once took about 10 s.
STUDIO_ATTACH_TIMEOUT_SECONDS = 12.0
STUDIO_ATTACH_POLL_INTERVAL_SECONDS = 0.5
# What is left of the window has to be worth a round trip, or the window is
# spent rather than nearly spent. A poll handed a sliver of one reaches the
# bridge with a deadline already gone, and `write_all` reports that as a proxy
# that stopped reading its input: a transport fault quoted back for a bridge
# that was answering fine. A real round trip over a local pipe is microseconds,
# so 50 ms is generous, and it is invisible against a 12 s window.
MINIMUM_ATTACH_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class StudioInstance:
    """One Roblox Studio process registered with the MCP bridge."""

    identifier: str
    name: str

    def describe(self) -> str:
        """`id (name)` for display, or the bare id when the bridge reported no name.

        Both halves come from the bridge, so both are sanitised onto one line and
        capped: an id with a newline in it would otherwise forge a second entry
        in a list the caller is about to choose from, and a half-megabyte place
        name would bury the entry next to it. Display only; `identifier` is what
        gets sent.
        """
        identifier = sanitize_diagnostic_line(self.identifier)
        rendered = sanitize_diagnostic_line(self.name)
        return f"{identifier} ({rendered})" if rendered else identifier


@dataclass(frozen=True)
class StudioAttachOutcome:
    """The result of waiting for Studio to attach: what registered, and how long it took."""

    instances: list[StudioInstance] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    last_error_text: str = ""
    # What the failure message quotes: `--timeout` when it is the shorter one.
    window_seconds: float = STUDIO_ATTACH_TIMEOUT_SECONDS

    @property
    def attached(self) -> bool:
        """True when at least one Studio instance registered inside the window."""
        return bool(self.instances)


def parse_studio_instances(payload_text: str) -> list[StudioInstance]:
    """Read the instance lister's text content into `StudioInstance` records.

    The bridge returns its answer as JSON inside a text content item, currently
    `{"studios": [{"id": ..., "name": ...}]}`. Entries are accepted as objects
    (any of the usual id and name spellings, and a missing name has been seen in
    the wild) or as bare id strings, and an unparseable payload reads as "no
    instances" rather than crashing the command.

    This is a SECOND parse of server bytes, and the hostile JSON it has to
    survive is the same set `framing.parse_frame` survives, so it runs the same
    guards: the depth scan before the parse and the two number hooks inside it.
    They do not come free from the frame the text arrived in. The frame's own
    scan reads a bracket inside a string literal as text, correctly, and this
    payload IS that string: 200,000 `[` in it rode through framing untouched and
    took `luau`, `instances` and `doctor` down here with a `RecursionError`, and
    a 5,000-digit instance id did the same with the `ValueError` Python raises
    past its integer-conversion limit. Neither is a JSONDecodeError, so neither
    was caught.
    """
    stripped = payload_text.strip()
    if not stripped:
        return []
    # A lone surrogate is legal JSON and unencodable, and the scan only reads
    # ASCII structure, so replacing what will not encode costs it nothing.
    if exceeds_container_depth(stripped.encode("utf-8", errors="replace")):
        logger.debug("instance list nested past %d containers", MAX_FRAME_CONTAINER_DEPTH)
        return []
    try:
        payload = json.loads(
            stripped,
            parse_constant=refuse_json_constant,
            parse_float=refuse_non_finite_number,
        )
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError, the integer-conversion limit, and
        # the two hooks above; RecursionError covers nesting the scan let past.
        logger.debug("instance list was not JSON this build can read: %r", stripped[:200])
        return []

    entries = payload
    if isinstance(payload, dict):
        for key in STUDIO_INSTANCE_LIST_KEYS:
            if isinstance(payload.get(key), list):
                entries = payload[key]
                break
        else:
            return []
    if not isinstance(entries, list):
        return []

    instances: list[StudioInstance] = []
    for entry in entries:
        if isinstance(entry, str):
            instances.append(StudioInstance(identifier=entry, name=""))
            continue
        if not isinstance(entry, dict):
            continue
        identifier = next(
            (str(entry[key]) for key in STUDIO_INSTANCE_ID_KEYS if entry.get(key)), ""
        )
        name = next((str(entry[key]) for key in STUDIO_INSTANCE_NAME_KEYS if entry.get(key)), "")
        if identifier:
            instances.append(StudioInstance(identifier=identifier, name=name))
    return instances


def wait_for_studio_instances(
    client: StudioMcpClient,
    tools: list[ToolDefinition],
    timeout: float = DEFAULT_CALL_TOOL_TIMEOUT_SECONDS,
    attach_timeout: float = STUDIO_ATTACH_TIMEOUT_SECONDS,
) -> StudioAttachOutcome:
    """Poll the bridge until a Studio instance registers, or the window closes.

    Studio does not attach to a client the instant that client connects: for the
    first few seconds the lister answers with an empty list, or with an "unable
    to reach Roblox Studio" error. Both mean "not yet", so both are polled
    through rather than reported. The wait is bounded by the smaller of
    `attach_timeout` and the caller's `--timeout`, so a short timeout still
    means a short command, and by a floor at the other end: what is left of the
    window has to be worth a round trip, or the wait ends here rather than
    spending a request id on a deadline that is already gone.

    Nothing is cached between runs: a Studio can open or close between two
    commands, and instance ids do not survive a Studio restart.
    """
    lister = find_tool(tools, LIST_INSTANCES_INTENT).tool
    # The smaller of the two bounds is the one every poll and every message obeys.
    window = max(min(attach_timeout, timeout), 0.0)
    started = time.monotonic()
    deadline = started + window
    last_error_text = ""

    while True:
        # What is left of the window, never the whole `--timeout`: given that, a
        # poll starting just inside a 12 s window could run another 120. Read
        # at the TOP of each pass, after the sleep below rather than before it,
        # because with a 1 s window and a 0.5 s interval the arithmetic lands
        # exactly on the deadline and the poll that followed was handed nothing.
        remaining = deadline - time.monotonic()
        if remaining < MINIMUM_ATTACH_POLL_SECONDS:
            return StudioAttachOutcome(
                elapsed_seconds=time.monotonic() - started,
                last_error_text=last_error_text,
                window_seconds=window,
            )
        try:
            result = client.call_tool(lister.name, {}, timeout=remaining)
        except StudioMcpTimeoutError as poll_error:
            # The poll held the rest of the window, so the window is gone, and
            # "nothing attached in time" is what this function already reports.
            # What it does not say is that nothing answered at all, which is a
            # different fault from a bridge that answered "no studios": quote it
            # the way an error answer is quoted, or the advice sends the reader
            # to Studio for a bridge that never spoke. "nothing" leads, because
            # the line it lands under reads "The bridge last answered:".
            last_error_text = f"nothing; {poll_error}"
            logger.debug("the instance lister did not answer inside the attach window")
        else:
            if result.is_error:
                last_error_text = result.text
            else:
                last_error_text = ""
                instances = parse_studio_instances(result.text)
                if instances:
                    return StudioAttachOutcome(
                        instances, time.monotonic() - started, window_seconds=window
                    )

        sleep_seconds = min(STUDIO_ATTACH_POLL_INTERVAL_SECONDS, deadline - time.monotonic())
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def no_studio_instance_message(window_seconds: float) -> str:
    """The "no Studio attached" advice, naming the window that actually elapsed.

    That number is a fact about this run, not the default: `--timeout 1` waits
    one second, and saying twelve sends the reader hunting for eleven that
    never happened.
    """
    return (
        f"No Roblox Studio instance attached within {window_seconds:g} s. "
        "Studio attaches to a client a few seconds after it connects; check that a place is "
        'open and "Enable Studio as MCP server" is on (Assistant settings > MCP Servers). '
        'Studio\'s dialog shows "No clients connected" until a command is running; '
        "that is normal."
    )


def attach_failure_message(outcome: StudioAttachOutcome) -> str:
    """The "no Studio attached" advice, quoting what the bridge last said.

    The quote keeps its line breaks, because a bridge error can be a short stack,
    but it is capped: it is a diagnostic aside under the advice that matters, and
    a server that answers with a megabyte would otherwise push the advice away.
    """
    advice = no_studio_instance_message(outcome.window_seconds)
    if not outcome.last_error_text:
        return advice
    quoted = truncate_display_text(sanitize_terminal_text(outcome.last_error_text.strip()))
    return f"{advice}\nThe bridge last answered: {quoted}"


def resolve_studio_id(
    client: StudioMcpClient,
    tools: list[ToolDefinition],
    requested: str | None,
    timeout: float = DEFAULT_CALL_TOOL_TIMEOUT_SECONDS,
) -> str:
    """Decide which Studio instance a tool call should target.

    Resolution order: the explicit `--studio` value, then `ROBLOX_STUDIO_ID`,
    then auto-selection when exactly one instance is registered. A `--studio`
    value is matched against ids first, then whole names, then name substrings,
    and a value that fits several instances is an error rather than a first-hit
    guess.

    Raises:
        StudioNotAttachedError: nothing registered inside the attach window.
        ToolDiscoveryError: the caller named an instance that does not exist or
            is ambiguous, or several are open and the caller did not choose.
    """
    wanted = (requested or os.environ.get(STUDIO_ID_ENV_VAR, "")).strip()
    outcome = wait_for_studio_instances(client, tools, timeout=timeout)
    instances = outcome.instances

    if not instances:
        # A caller-supplied id is still worth trying: the lister can lag behind a
        # Studio that is up, and Studio itself gives the authoritative answer.
        if wanted:
            return wanted
        raise StudioNotAttachedError(attach_failure_message(outcome))

    if wanted:
        return match_requested_instance(instances, wanted)

    if len(instances) == 1:
        return instances[0].identifier

    # The count is the bridge's answer and stays exact; the rows under it stop
    # at MAX_ENUMERATED_NAMES, because the caller only needs enough to choose.
    listed = "\n".join(
        f"  {row}" for row in capped_display_names(item.describe() for item in instances)
    )
    raise ToolDiscoveryError(
        f"{len(instances)} Studio instances are registered; pass --studio <id-or-name>:\n{listed}"
    )


def match_requested_instance(instances: list[StudioInstance], wanted: str) -> str:
    """Resolve a `--studio` value against the registered instances, or explain why not."""
    for instance in instances:
        if instance.identifier == wanted:
            return instance.identifier

    folded = wanted.lower()
    for candidates in (
        [item for item in instances if item.name.lower() == folded],
        [item for item in instances if folded in item.name.lower()],
    ):
        if len(candidates) == 1:
            return candidates[0].identifier
        if len(candidates) > 1:
            listed = ", ".join(capped_display_names(item.describe() for item in candidates))
            raise ToolDiscoveryError(
                f"--studio {wanted!r} matches {len(candidates)} instances: {listed}. "
                "Pass the id instead."
            )

    known = ", ".join(capped_display_names(instance.describe() for instance in instances))
    raise ToolDiscoveryError(f"no Studio instance matches {wanted!r}. Registered: {known}")


def apply_studio_id(
    client: StudioMcpClient,
    tools: list[ToolDefinition],
    tool: ToolDefinition,
    arguments: dict,
    requested: str | None,
    timeout: float = DEFAULT_CALL_TOOL_TIMEOUT_SECONDS,
) -> None:
    """Fill in the `studio_id` argument when the tool declares one and it is unset.

    Skipped for tools that take no `studio_id` (the instance lister), and for an
    explicit value the caller already passed through `--args`.
    """
    if STUDIO_ID_ARGUMENT_NAME not in tool.argument_names:
        return
    if arguments.get(STUDIO_ID_ARGUMENT_NAME):
        return
    arguments[STUDIO_ID_ARGUMENT_NAME] = resolve_studio_id(client, tools, requested, timeout)
