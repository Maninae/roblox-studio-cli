"""Runtime discovery, half one: which live tool to call, with which arguments.

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

Half two is `instance_discovery`: WHICH Studio a matched tool gets called on.
Everything here is a pure function over a `tools/list` payload, so nothing in
this module needs a live client.
"""

import json
import logging
import re
from dataclasses import dataclass

from roblox_studio_cli.errors import StudioRequestError
from roblox_studio_cli.mcp_payloads import ToolDefinition
from roblox_studio_cli.terminal import (
    MAX_ENUMERATED_NAMES,
    capped_display_names,
    sanitize_diagnostic_line,
    sanitize_terminal_text,
)

logger = logging.getLogger(__name__)

LUAU_CODE_ARGUMENT_NAMES = ("code", "luau", "source", "script", "command", "expression")
LUAU_CONTEXT_ARGUMENT_NAMES = ("datamodel_type", "datamodelType", "data_model_type", "context")
PLAY_START_ARGUMENT_NAMES = ("is_start", "isStart", "start", "playing")
CAPTURE_ID_ARGUMENT_NAMES = ("capture_id", "captureId")

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


def named_list(names: list[str]) -> str:
    """Sorted, deduplicated tool names for one clause of a message, capped."""
    return ", ".join(capped_display_names(sorted(set(names))))


def no_tool_message(
    tools: list[ToolDefinition],
    intent: ToolIntent,
    ambiguous: list[str],
    rejected: list[str],
    destructive: list[str],
) -> str:
    """Explain a failed lookup: too many candidates, destructive or wrong-shaped ones, or none.

    Every name in here was chosen by the server, so each list is capped: the
    sentence that matters is the last one, and it has to survive a build that
    exposes hundreds of tools.
    """
    if ambiguous:
        detail = f"Several tools could be it: {named_list(ambiguous)}."
    elif destructive:
        detail = (
            f"{named_list(destructive)} matched by name but carries a "
            "destructive verb, so no convenience command will call it."
        )
    elif rejected:
        wanted = ", ".join(intent.expected_arguments)
        detail = f"{named_list(rejected)} matched by name but declares none of: {wanted}."
    else:
        detail = "Nothing matched by name."
    available = ", ".join(capped_display_names(sorted(tool.name for tool in tools))) or "(none)"
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
    rather than a server side rejection that has to be decoded. The schema
    decides how many keys that is AND how long each one is, and the server wrote
    the schema, so the example is built from the same capped display names the
    naming clause uses. Built from the raw names, it put a 1,000,000-character
    argument key on stderr in full, on one line, after the clause above it had
    been capped.
    """
    missing = [name for name in tool.required_argument_names if name not in arguments]
    if not missing:
        return
    shown = capped_display_names(missing)
    # Slicing past the cap drops the "and N more" tail, which is a sentence
    # rather than an argument name and has no business inside the JSON example.
    example = json.dumps({name: "..." for name in shown[:MAX_ENUMERATED_NAMES]})
    named = ", ".join(shown)
    raise ToolDiscoveryError(
        sanitize_terminal_text(
            f"tool {sanitize_diagnostic_line(tool.name)!r} requires {named}. "
            f"Add them with --args '{example}'"
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
