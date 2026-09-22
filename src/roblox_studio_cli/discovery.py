"""Runtime discovery: which live tool to call, with which arguments, on which Studio.

Nothing here hardcodes a tool name or an argument key. Every lookup starts from
the `tools/list` payload Studio returned this run, so a Roblox rename
(`execute_luau` to something else, `code` to `source`) degrades to a clear
"no matching tool" message instead of a silent wrong call.

Two problems live in this module:

- Tool and argument matching. `find_tool_by_keywords` maps an intent ("run some
  Luau") onto a live tool name; `find_argument_name` maps a value ("the source
  text") onto a live schema key.
- Studio instance resolution. Every Studio tool except the instance lister takes
  a required `studio_id`, naming which running Studio to act on. The CLI asks
  the bridge which instances exist and fills that argument in, so a human never
  has to paste an id by hand when only one Studio is open.
"""

import json
import logging
import os
from dataclasses import dataclass

from roblox_studio_cli.client import StudioMcpClient, StudioMcpError, ToolDefinition

logger = logging.getLogger(__name__)

STUDIO_ID_ARGUMENT_NAME = "studio_id"
STUDIO_ID_ENV_VAR = "ROBLOX_STUDIO_ID"

LIST_INSTANCES_TOOL_NAME_KEYWORDS = ("list_roblox_studios", "list_studios", "studios")
LUAU_TOOL_NAME_KEYWORDS = ("execute_luau", "luau", "lua")
SCREENSHOT_TOOL_NAME_KEYWORDS = ("screen_capture", "screenshot", "screen", "capture")
STUDIO_STATE_TOOL_NAME_KEYWORDS = ("get_studio_state", "studio_state", "state")
PLAY_TOOL_NAME_KEYWORDS = ("start_stop_play", "play", "run")

LUAU_CODE_ARGUMENT_NAMES = ("code", "luau", "source", "script", "command", "expression")
LUAU_CONTEXT_ARGUMENT_NAMES = ("datamodel_type", "datamodelType", "data_model_type", "context")
PLAY_START_ARGUMENT_NAMES = ("is_start", "isStart", "start", "playing")
CAPTURE_ID_ARGUMENT_NAMES = ("capture_id", "captureId")

STUDIO_INSTANCE_ID_KEYS = ("studio_id", "id", "studioId", "instance_id")
STUDIO_INSTANCE_NAME_KEYS = ("name", "place_name", "placeName", "title", "game_name")
STUDIO_INSTANCE_LIST_KEYS = ("studios", "instances", "roblox_studios")

NO_STUDIO_INSTANCE_MESSAGE = (
    "No Roblox Studio instance is registered with the MCP bridge. "
    "Open a place in Studio (File > New, or a template); if one is already open, "
    'quit and relaunch Studio so the "Enable Studio as MCP server" toggle takes effect.'
)


class ToolDiscoveryError(StudioMcpError):
    """No live tool matches the intent, or the call cannot be completed as asked.

    Subclasses `StudioMcpError` so the CLI's one error handler covers it.
    """


@dataclass(frozen=True)
class StudioInstance:
    """One Roblox Studio process registered with the MCP bridge."""

    identifier: str
    name: str

    def describe(self) -> str:
        """`id (name)` for display, or the bare id when the bridge reported no name."""
        return f"{self.identifier} ({self.name})" if self.name else self.identifier


def find_tool_by_keywords(
    tools: list[ToolDefinition], keywords: tuple[str, ...], purpose: str
) -> ToolDefinition:
    """Pick the live tool whose name best matches `keywords`.

    Keywords are tried in priority order, so a specific match (`screen_capture`)
    wins over a loose one (`capture`). Among equal matches the shortest name
    wins, which favours the plain tool over a more qualified variant.

    Raises:
        ToolDiscoveryError: nothing matched; the message lists what Studio does
            expose, so the caller can fall back to `roblox-studio call`.
    """
    for keyword in keywords:
        matches = [tool for tool in tools if keyword in tool.name.lower()]
        if matches:
            return min(matches, key=lambda tool: len(tool.name))
    available = ", ".join(tool.name for tool in tools) or "(none)"
    raise ToolDiscoveryError(
        f"no {purpose} tool found in this Studio build. Available tools: {available}. "
        "Use `roblox-studio call <tool> --args '<json>'` to drive it directly."
    )


def find_argument_name(
    tool: ToolDefinition, preferred_names: tuple[str, ...], fall_back_to_required: bool
) -> str | None:
    """Resolve which schema argument carries a value, by preference then by shape.

    Args:
        tool: the discovered tool whose JSON Schema is being read.
        preferred_names: candidate keys in priority order.
        fall_back_to_required: when no candidate matches, accept the tool's first
            required string argument. That is the right guess for a renamed code
            field and the wrong guess for an optional extra, so it is opt-in.

    Returns:
        The argument name, or None when nothing plausible exists.
    """
    for name in preferred_names:
        if name in tool.argument_names:
            return name
    if fall_back_to_required:
        for name in tool.required_argument_names:
            if tool.property_schema(name).get("type") == "string":
                return name
    return None


def check_required_arguments(tool: ToolDefinition, arguments: dict) -> None:
    """Fail before the call when the schema demands arguments the caller did not supply.

    Catching this locally produces an actionable message naming the missing keys,
    rather than a server side rejection that has to be decoded.
    """
    missing = [name for name in tool.required_argument_names if name not in arguments]
    if not missing:
        return
    example = json.dumps({name: "..." for name in missing})
    raise ToolDiscoveryError(
        f"tool {tool.name!r} requires {', '.join(missing)}. Add them with --args '{example}'"
    )


def parse_arguments_option(raw_arguments: str | None) -> dict:
    """Parse the `--args` JSON blob into a mapping, rejecting anything else."""
    if not raw_arguments:
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as decode_error:
        raise ToolDiscoveryError(f"--args is not valid JSON: {decode_error}") from decode_error
    if not isinstance(parsed, dict):
        raise ToolDiscoveryError(
            "--args must be a JSON object, for example '{\"code\": \"return 1\"}'"
        )
    return parsed


def parse_studio_instances(payload_text: str) -> list[StudioInstance]:
    """Read the instance lister's text content into `StudioInstance` records.

    The bridge returns its answer as JSON inside a text content item, currently
    `{"studios": [...]}`. Entries are accepted as objects (any of the usual id
    and name spellings) or as bare id strings, and an unparseable payload reads
    as "no instances" rather than crashing the command.
    """
    stripped = payload_text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("instance list was not JSON: %r", stripped[:200])
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
        identifier = next((str(entry[key]) for key in STUDIO_INSTANCE_ID_KEYS if entry.get(key)), "")
        name = next((str(entry[key]) for key in STUDIO_INSTANCE_NAME_KEYS if entry.get(key)), "")
        if identifier:
            instances.append(StudioInstance(identifier=identifier, name=name))
    return instances


def list_studio_instances(
    client: StudioMcpClient, tools: list[ToolDefinition]
) -> list[StudioInstance]:
    """Ask the bridge which Studio processes are registered right now.

    Nothing is cached: a Studio can open or close between two commands, and a
    stale id fails in a way that is much harder to read than one extra call.
    """
    lister = find_tool_by_keywords(tools, LIST_INSTANCES_TOOL_NAME_KEYWORDS, "Studio instance list")
    result = client.call_tool(lister.name, {})
    if result.is_error:
        raise ToolDiscoveryError(f"could not list Studio instances: {result.text}")
    return parse_studio_instances(result.text)


def resolve_studio_id(
    client: StudioMcpClient, tools: list[ToolDefinition], requested: str | None
) -> str:
    """Decide which Studio instance a tool call should target.

    Resolution order: the explicit `--studio` value, then `ROBLOX_STUDIO_ID`,
    then auto-selection when exactly one instance is registered.

    Args:
        requested: an instance id or a (case-insensitive) place name, or None.

    Raises:
        ToolDiscoveryError: no instance is registered, or several are and the
            caller did not say which.
    """
    wanted = (requested or os.environ.get(STUDIO_ID_ENV_VAR, "")).strip()
    instances = list_studio_instances(client, tools)

    if not instances:
        # A user-supplied id is still worth trying: the lister can lag behind a
        # Studio that is up, and Studio itself gives the authoritative answer.
        if wanted:
            return wanted
        raise ToolDiscoveryError(NO_STUDIO_INSTANCE_MESSAGE)

    if wanted:
        for instance in instances:
            if instance.identifier == wanted:
                return instance.identifier
        for instance in instances:
            if instance.name.lower() == wanted.lower():
                return instance.identifier
        for instance in instances:
            if wanted.lower() in instance.name.lower():
                return instance.identifier
        known = ", ".join(instance.describe() for instance in instances)
        raise ToolDiscoveryError(f"no Studio instance matches {wanted!r}. Registered: {known}")

    if len(instances) == 1:
        return instances[0].identifier

    listed = "\n".join(f"  {instance.describe()}" for instance in instances)
    raise ToolDiscoveryError(
        f"{len(instances)} Studio instances are registered; pass --studio <id-or-name>:\n{listed}"
    )


def apply_studio_id(
    client: StudioMcpClient,
    tools: list[ToolDefinition],
    tool: ToolDefinition,
    arguments: dict,
    requested: str | None,
) -> None:
    """Fill in the `studio_id` argument when the tool declares one and it is unset.

    Skipped for tools that take no `studio_id` (the instance lister), and for an
    explicit value the caller already passed through `--args`.
    """
    if STUDIO_ID_ARGUMENT_NAME not in tool.argument_names:
        return
    if arguments.get(STUDIO_ID_ARGUMENT_NAME):
        return
    arguments[STUDIO_ID_ARGUMENT_NAME] = resolve_studio_id(client, tools, requested)
