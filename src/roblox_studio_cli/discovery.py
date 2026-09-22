"""Runtime discovery: which live tool to call, with which arguments, on which Studio.

Nothing here hardcodes a tool name or an argument key. Every lookup starts from
the `tools/list` payload Studio returned this run, so a Roblox rename
(`execute_luau` to something else, `code` to `source`) degrades to a clear "no
matching tool" message instead of a silent wrong call.

Matching is deliberately strict, because the failure it prevents is calling the
wrong tool on someone's open place. Four rules:

- Names match on whole tokens, never on substrings. `execute_luau` splits into
  {execute, luau} and `screenCapture` into {screen, capture}. Substring matching
  used to pick `evaluate_expression` for "lua" (it is in "eva-lua-te") and
  `close_studios` for "studios".
- A keyword names the ACTION as well as the subject: "list_studios", not
  "studios"; "get_state", not "state". There are no bare subject keywords, which
  is what a pen-test round turned up: with "luau" on the list, a server offering
  `execute_luau_and_delete_place` or `delete_lua_scripts` won the lookup.
- Outside an exact name hit, the candidate's token set must EQUAL the keyword's,
  not merely contain it. A superset is a different tool: `delete_capture` is not
  a capture. The candidate must also declare one of the arguments the intent
  needs, and must be the only candidate. Anything else is an error naming the
  alternatives, so the caller can fall back to `roblox-studio call`.
- A destructive verb in a name disqualifies it from every convenience command,
  at both tiers, however well it matches. An intent that legitimately destroys
  something says so once, in `allowed_destructive_tokens`: `start_stop_play`
  carries "stop" and the play intent is the only one that accepts it.

Studio instance resolution lives here too. Every Studio tool except the instance
lister takes a required `studio_id`, and Studio attaches to a freshly connected
client a few seconds late, so `wait_for_studio_instances` polls instead of
reading once and giving up.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from roblox_studio_cli.client import DEFAULT_CALL_TOOL_TIMEOUT_SECONDS, StudioMcpClient
from roblox_studio_cli.errors import StudioNotAttachedError, StudioRequestError
from roblox_studio_cli.mcp_payloads import ToolDefinition
from roblox_studio_cli.terminal import sanitize_single_line, sanitize_terminal_text

logger = logging.getLogger(__name__)

STUDIO_ID_ARGUMENT_NAME = "studio_id"
STUDIO_ID_ENV_VAR = "ROBLOX_STUDIO_ID"

LUAU_CODE_ARGUMENT_NAMES = ("code", "luau", "source", "script", "command", "expression")
LUAU_CONTEXT_ARGUMENT_NAMES = ("datamodel_type", "datamodelType", "data_model_type", "context")
PLAY_START_ARGUMENT_NAMES = ("is_start", "isStart", "start", "playing")
CAPTURE_ID_ARGUMENT_NAMES = ("capture_id", "captureId")

STUDIO_INSTANCE_ID_KEYS = ("studio_id", "id", "studioId", "instance_id")
STUDIO_INSTANCE_NAME_KEYS = ("name", "place_name", "placeName", "title", "game_name")
STUDIO_INSTANCE_LIST_KEYS = ("studios", "instances", "roblox_studios")

# Measured against Studio 0.739 on 2026-09-22: three fresh client sessions saw
# their instance appear 2.75 s, 2.87 s and 3.38 s after connecting, with the
# bridge answering "no studios" (or an "unable to reach Studio" error) until
# then. Earlier runs attached in 1.1 s and once took about 10 s.
STUDIO_ATTACH_TIMEOUT_SECONDS = 12.0
STUDIO_ATTACH_POLL_INTERVAL_SECONDS = 0.5

NO_STUDIO_INSTANCE_MESSAGE = (
    f"No Roblox Studio instance attached within {STUDIO_ATTACH_TIMEOUT_SECONDS:.0f} s. "
    "Studio attaches to a client a few seconds after it connects; check that a place is "
    'open and "Enable Studio as MCP server" is on (Assistant settings > MCP Servers). '
    'Studio\'s dialog shows "No clients connected" until a command is running; '
    "that is normal."
)

TOOL_NAME_TOKEN_PATTERN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

# Verbs that make a tool the wrong answer to "show me" or "run this", whatever
# else its name says. No convenience command may bind to a name carrying one,
# unless its intent declares that verb (only play control does, for "stop").
DESTRUCTIVE_NAME_TOKENS = frozenset(
    {"delete", "remove", "close", "reset", "clear", "stop", "destroy", "wipe"}
)


class ToolDiscoveryError(StudioRequestError):
    """No live tool matches the intent, or the call cannot be made as asked.

    A `StudioRequestError`, so the CLI exits 2: the request needs changing, not
    the environment.
    """


@dataclass(frozen=True)
class ToolIntent:
    """What a convenience command is looking for in whatever tools Studio reports.

    Attributes:
        purpose: human words for the error message ("Luau execution").
        name_keywords: candidate names, most specific first. Each must carry the
            action token, so a keyword is "list_studios" rather than "studios",
            and never a bare subject like "luau" or "capture".
        expected_arguments: argument names that prove a candidate does this job.
            Enforced for every match except an exact name hit, where the name is
            evidence enough and a future build may have renamed its arguments.
        allowed_destructive_tokens: the destructive verbs this intent is allowed
            to bind to. Empty for everything but play control, whose real tool is
            `start_stop_play`.
    """

    purpose: str
    name_keywords: tuple[str, ...]
    expected_arguments: tuple[str, ...] = ()
    allowed_destructive_tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolMatch:
    """The tool discovery settled on, and how sure the match is.

    `matched_by_exact_name` gates the riskier argument guesses: falling back to
    "the first required string argument" is right for a tool we recognised by
    name and wrong for one we recognised by a loose keyword.
    """

    tool: ToolDefinition
    matched_by_exact_name: bool


@dataclass(frozen=True)
class StudioInstance:
    """One Roblox Studio process registered with the MCP bridge."""

    identifier: str
    name: str

    def describe(self) -> str:
        """`id (name)` for display, or the bare id when the bridge reported no name.

        Both halves come from the bridge, so both are sanitised onto one line: an
        id with a newline in it would otherwise forge a second entry in a list
        the caller is about to choose from.
        """
        identifier = sanitize_single_line(self.identifier)
        rendered = sanitize_single_line(self.name)
        return f"{identifier} ({rendered})" if rendered else identifier


@dataclass(frozen=True)
class StudioAttachOutcome:
    """The result of waiting for Studio to attach: what registered, and how long it took."""

    instances: list[StudioInstance] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    last_error_text: str = ""

    @property
    def attached(self) -> bool:
        """True when at least one Studio instance registered inside the window."""
        return bool(self.instances)


LIST_INSTANCES_INTENT = ToolIntent(
    purpose="Studio instance list",
    name_keywords=("list_roblox_studios", "list_studios", "list_studio", "studio_list"),
)
LUAU_INTENT = ToolIntent(
    purpose="Luau execution",
    name_keywords=("execute_luau", "run_luau"),
    expected_arguments=LUAU_CODE_ARGUMENT_NAMES,
)
SCREENSHOT_INTENT = ToolIntent(
    purpose="screen capture",
    name_keywords=("screen_capture", "screenshot"),
    expected_arguments=CAPTURE_ID_ARGUMENT_NAMES,
)
STUDIO_STATE_INTENT = ToolIntent(
    purpose="Studio state",
    name_keywords=("get_studio_state", "studio_state", "get_state"),
)
PLAY_INTENT = ToolIntent(
    purpose="play control",
    name_keywords=("start_stop_play", "start_stop"),
    expected_arguments=PLAY_START_ARGUMENT_NAMES,
    allowed_destructive_tokens=("stop",),
)


def tool_name_tokens(name: str) -> set[str]:
    """Lowercase word tokens of a tool name, splitting separators and camelCase.

    `list_roblox_studios` and `listRobloxStudios` both give
    {list, roblox, studios}, so matching survives a change of naming style.
    """
    return {token.lower() for token in TOOL_NAME_TOKEN_PATTERN.findall(name)}


def declares_any_argument(tool: ToolDefinition, argument_names: tuple[str, ...]) -> bool:
    """True when the tool's schema has at least one of these arguments (or none is asked for)."""
    if not argument_names:
        return True
    declared = set(tool.argument_names)
    return any(name in declared for name in argument_names)


def destructive_tokens_in(tool_name: str, intent: ToolIntent) -> set[str]:
    """Destructive verbs in a tool name that this intent has not declared.

    Empty means the name is safe to bind to. `start_stop_play` returns empty for
    the play intent (which declares "stop") and {"stop"} for every other intent.
    """
    return (tool_name_tokens(tool_name) & DESTRUCTIVE_NAME_TOKENS) - set(
        intent.allowed_destructive_tokens
    )


def plausible_destructive_names(tools: list[ToolDefinition], intent: ToolIntent) -> list[str]:
    """Destructive tools that carry every token of some keyword, for the error message.

    These are exactly the names that used to win the lookup under the old
    "candidate contains the keyword's tokens" rule, so naming them tells the
    caller why a tool that looks right was passed over.
    """
    names = []
    for tool in tools:
        if not destructive_tokens_in(tool.name, intent):
            continue
        tokens = tool_name_tokens(tool.name)
        if any(tool_name_tokens(keyword) <= tokens for keyword in intent.name_keywords):
            names.append(tool.name)
    return names


def find_tool(tools: list[ToolDefinition], intent: ToolIntent) -> ToolMatch:
    """Pick the one live tool that serves `intent`.

    Destructive names are dropped first, so no later tier can resurrect one.
    Then exact name, in keyword order. Then whole-token matching on an EQUAL
    token set, where a candidate must also declare one of the intent's arguments
    and must be the only such candidate: two plausible tools is an error, not a
    coin toss.

    Raises:
        ToolDiscoveryError: nothing matched, or several things did. The message
            says which, lists what Studio does expose, and points at
            `roblox-studio call`.
    """
    safe = [tool for tool in tools if not destructive_tokens_in(tool.name, intent)]

    for keyword in intent.name_keywords:
        for tool in safe:
            if tool.name.lower() == keyword.lower():
                return ToolMatch(tool=tool, matched_by_exact_name=True)

    ambiguous: list[str] = []
    rejected: list[str] = []
    for keyword in intent.name_keywords:
        keyword_tokens = tool_name_tokens(keyword)
        named = [tool for tool in safe if tool_name_tokens(tool.name) == keyword_tokens]
        usable = [tool for tool in named if declares_any_argument(tool, intent.expected_arguments)]
        rejected.extend(tool.name for tool in named if tool not in usable)
        if len(usable) == 1:
            logger.debug("matched %r for %s via keyword %r", usable[0].name, intent.purpose, keyword)
            return ToolMatch(tool=usable[0], matched_by_exact_name=False)
        if len(usable) > 1:
            ambiguous = [tool.name for tool in usable]

    destructive = plausible_destructive_names(tools, intent)
    raise ToolDiscoveryError(no_tool_message(tools, intent, ambiguous, rejected, destructive))


def no_tool_message(
    tools: list[ToolDefinition],
    intent: ToolIntent,
    ambiguous: list[str],
    rejected: list[str],
    destructive: list[str],
) -> str:
    """Explain a failed lookup: too many candidates, destructive or wrong-shaped ones, or none."""
    if ambiguous:
        detail = f"Several tools could be it: {', '.join(sorted(set(ambiguous)))}."
    elif destructive:
        detail = (
            f"{', '.join(sorted(set(destructive)))} matched by name but carries a "
            "destructive verb, so no convenience command will call it."
        )
    elif rejected:
        wanted = ", ".join(intent.expected_arguments)
        detail = (
            f"{', '.join(sorted(set(rejected)))} matched by name but declares none of: {wanted}."
        )
    else:
        detail = "Nothing matched by name."
    available = ", ".join(sorted(tool.name for tool in tools)) or "(none)"
    return sanitize_terminal_text(
        f"no {intent.purpose} tool found in this Studio build. {detail} "
        f"Available tools: {available}. "
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
            required string argument. Right for a renamed field on a tool we
            matched by exact name, wrong for a tool matched by a loose keyword,
            so callers pass `ToolMatch.matched_by_exact_name` here.

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
        sanitize_terminal_text(
            f"tool {tool.name!r} requires {', '.join(missing)}. Add them with --args '{example}'"
        )
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
    `{"studios": [{"id": ..., "name": ...}]}`. Entries are accepted as objects
    (any of the usual id and name spellings, and a missing name has been seen in
    the wild) or as bare id strings, and an unparseable payload reads as "no
    instances" rather than crashing the command.
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
    means a short command.

    Nothing is cached between runs: a Studio can open or close between two
    commands, and instance ids do not survive a Studio restart.
    """
    lister = find_tool(tools, LIST_INSTANCES_INTENT).tool
    started = time.monotonic()
    deadline = started + max(min(attach_timeout, timeout), 0.0)
    last_error_text = ""

    while True:
        result = client.call_tool(lister.name, {}, timeout=timeout)
        if result.is_error:
            last_error_text = result.text
        else:
            last_error_text = ""
            instances = parse_studio_instances(result.text)
            if instances:
                return StudioAttachOutcome(instances, time.monotonic() - started)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return StudioAttachOutcome(
                elapsed_seconds=time.monotonic() - started, last_error_text=last_error_text
            )
        time.sleep(min(STUDIO_ATTACH_POLL_INTERVAL_SECONDS, remaining))


def attach_failure_message(outcome: StudioAttachOutcome) -> str:
    """The "no Studio attached" advice, quoting what the bridge last said."""
    if not outcome.last_error_text:
        return NO_STUDIO_INSTANCE_MESSAGE
    quoted = sanitize_terminal_text(outcome.last_error_text.strip())
    return f"{NO_STUDIO_INSTANCE_MESSAGE}\nThe bridge last answered: {quoted}"


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

    listed = "\n".join(f"  {instance.describe()}" for instance in instances)
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
            listed = ", ".join(item.describe() for item in candidates)
            raise ToolDiscoveryError(
                f"--studio {wanted!r} matches {len(candidates)} instances: {listed}. "
                "Pass the id instead."
            )

    known = ", ".join(instance.describe() for instance in instances)
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
