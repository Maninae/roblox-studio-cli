"""Unit tests for runtime discovery: tool matching, argument matching, instance parsing.

These are pure functions over a `tools/list` payload, so they need neither a
subprocess nor Roblox. They pin the two behaviours that matter: intent maps onto
whatever names the live server reports (surviving a Roblox rename), and it
refuses to map onto a tool that merely looks similar.

The decoy names below are the ones substring matching actually picked wrong:
`evaluate_expression` contains "lua" inside "eva-lua-te", `reset_state` contains
"state", `close_studios` contains "studios", `screen_share` contains "screen".
Calling any of them instead of the tool the user asked for would act on a live
place, so each gets a test.
"""

import pytest

from roblox_studio_cli.discovery import (
    LIST_INSTANCES_INTENT,
    LUAU_CODE_ARGUMENT_NAMES,
    LUAU_INTENT,
    PLAY_INTENT,
    SCREENSHOT_INTENT,
    STUDIO_STATE_INTENT,
    StudioInstance,
    ToolDiscoveryError,
    check_required_arguments,
    find_argument_name,
    find_tool,
    match_requested_instance,
    parse_arguments_option,
    parse_studio_instances,
    tool_name_tokens,
)
from roblox_studio_cli.mcp_payloads import ToolDefinition

STRING = {"type": "string"}


def build_tool(name: str, properties: dict, required: list[str] | None = None) -> ToolDefinition:
    """A ToolDefinition with the JSON Schema shape the real server sends."""
    return ToolDefinition(
        name=name,
        description=f"{name} description",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required if required is not None else [],
        },
    )


LUAU_TOOL = build_tool(
    "execute_luau",
    {"code": STRING, "datamodel_type": STRING, "studio_id": STRING},
    ["code", "datamodel_type", "studio_id"],
)
CAPTURE_TOOL = build_tool(
    "screen_capture", {"capture_id": STRING, "studio_id": STRING}, ["capture_id", "studio_id"]
)
STATE_TOOL = build_tool("get_studio_state", {"studio_id": STRING}, ["studio_id"])
LISTER_TOOL = build_tool("list_roblox_studios", {})
PLAY_TOOL = build_tool(
    "start_stop_play", {"is_start": {"type": "boolean"}, "studio_id": STRING}, ["is_start"]
)

DECOY_EVALUATE = build_tool("evaluate_expression", {"expression": STRING}, ["expression"])
DECOY_RESET_STATE = build_tool("reset_state", {"studio_id": STRING}, ["studio_id"])
DECOY_CLOSE_STUDIOS = build_tool("close_studios", {"studio_id": STRING}, ["studio_id"])
DECOY_SCREEN_SHARE = build_tool("screen_share", {"studio_id": STRING}, ["studio_id"])
DECOY_CAPTURE_METRICS = build_tool("capture_metrics", {"studio_id": STRING})
ALL_DECOYS = [
    DECOY_EVALUATE,
    DECOY_RESET_STATE,
    DECOY_CLOSE_STUDIOS,
    DECOY_SCREEN_SHARE,
    DECOY_CAPTURE_METRICS,
]


def test_tool_name_tokens_splits_separators_and_camel_case():
    assert tool_name_tokens("list_roblox_studios") == {"list", "roblox", "studios"}
    assert tool_name_tokens("listRobloxStudios") == {"list", "roblox", "studios"}
    assert tool_name_tokens("HTTPGet") == {"http", "get"}


@pytest.mark.parametrize(
    "intent, expected",
    [
        (LIST_INSTANCES_INTENT, "list_roblox_studios"),
        (LUAU_INTENT, "execute_luau"),
        (SCREENSHOT_INTENT, "screen_capture"),
        (STUDIO_STATE_INTENT, "get_studio_state"),
        (PLAY_INTENT, "start_stop_play"),
    ],
)
def test_every_intent_finds_its_tool_among_the_decoys(intent, expected):
    """Studio 0.739's real names, surrounded by the names that used to win wrongly."""
    tools = [LISTER_TOOL, LUAU_TOOL, CAPTURE_TOOL, STATE_TOOL, PLAY_TOOL, *ALL_DECOYS]
    match = find_tool(tools, intent)
    assert match.tool.name == expected
    assert match.matched_by_exact_name is True


@pytest.mark.parametrize(
    "intent, decoy",
    [
        (LUAU_INTENT, DECOY_EVALUATE),
        (STUDIO_STATE_INTENT, DECOY_RESET_STATE),
        (LIST_INSTANCES_INTENT, DECOY_CLOSE_STUDIOS),
        (SCREENSHOT_INTENT, DECOY_SCREEN_SHARE),
        (SCREENSHOT_INTENT, DECOY_CAPTURE_METRICS),
    ],
)
def test_a_decoy_alone_is_refused_rather_than_called(intent, decoy):
    """With only a look-alike present, the answer is an error, never that tool."""
    with pytest.raises(ToolDiscoveryError) as raised:
        find_tool([decoy], intent)
    assert decoy.name in str(raised.value), "the message should name what Studio does expose"


def test_find_tool_survives_a_rename():
    """A renamed Luau tool is still found, through the loose keyword plus its arguments."""
    renamed = build_tool("run_lua_script", {"source": STRING}, ["source"])
    match = find_tool([renamed, DECOY_EVALUATE], LUAU_INTENT)
    assert match.tool.name == "run_lua_script"
    assert match.matched_by_exact_name is False, "a loose match must not license argument guessing"


def test_find_tool_accepts_camel_case_renames():
    renamed = build_tool("executeLuau", {"code": STRING}, ["code"])
    assert find_tool([renamed], LUAU_INTENT).tool.name == "executeLuau"


def test_find_tool_refuses_to_choose_between_two_plausible_tools():
    """Two candidates is an error naming both, not a coin toss on name length."""
    tools = [
        build_tool("run_lua_fast", {"code": STRING}, ["code"]),
        build_tool("run_lua_safe", {"code": STRING}, ["code"]),
    ]
    with pytest.raises(ToolDiscoveryError, match="Several tools could be it"):
        find_tool(tools, LUAU_INTENT)


def test_find_tool_says_when_the_name_matched_but_the_shape_did_not():
    with pytest.raises(ToolDiscoveryError, match="matched by name but declares none of"):
        find_tool([DECOY_CAPTURE_METRICS], SCREENSHOT_INTENT)


def test_find_tool_lists_alternatives_when_nothing_matches():
    with pytest.raises(ToolDiscoveryError, match="insert_asset"):
        find_tool([build_tool("insert_asset", {})], LUAU_INTENT)


def test_find_argument_prefers_the_known_name():
    assert find_argument_name(LUAU_TOOL, LUAU_CODE_ARGUMENT_NAMES, True) == "code"


def test_find_argument_falls_back_to_the_required_string():
    """When the code field is renamed to something unknown, shape picks it up."""
    renamed = build_tool("run_lua_script", {"body": STRING}, ["body"])
    assert find_argument_name(renamed, LUAU_CODE_ARGUMENT_NAMES, True) == "body"


def test_find_argument_returns_none_when_the_fallback_is_off():
    assert find_argument_name(CAPTURE_TOOL, LUAU_CODE_ARGUMENT_NAMES, False) is None


def test_check_required_arguments_names_what_is_missing():
    with pytest.raises(ToolDiscoveryError) as raised:
        check_required_arguments(LUAU_TOOL, {"code": "return 1"})
    message = str(raised.value)
    assert "datamodel_type" in message and "studio_id" in message
    assert "--args" in message


def test_check_required_arguments_passes_when_complete():
    check_required_arguments(LUAU_TOOL, {"code": "x", "datamodel_type": "Edit", "studio_id": "s"})


def test_parse_arguments_option_rejects_non_objects():
    assert parse_arguments_option(None) == {}
    assert parse_arguments_option('{"a": 1}') == {"a": 1}
    with pytest.raises(ToolDiscoveryError, match="valid JSON"):
        parse_arguments_option("{not json}")
    with pytest.raises(ToolDiscoveryError, match="JSON object"):
        parse_arguments_option("[1, 2]")


def test_parse_studio_instances_reads_the_bridge_payload():
    instances = parse_studio_instances('{"studios": [{"id": "studio-1", "name": "Baseplate"}]}')
    assert [(item.identifier, item.name) for item in instances] == [("studio-1", "Baseplate")]
    assert instances[0].describe() == "studio-1 (Baseplate)"


def test_parse_studio_instances_handles_the_empty_and_broken_cases():
    """An empty place list and unparseable output both mean "nothing registered"."""
    assert parse_studio_instances('{"studios": []}') == []
    assert parse_studio_instances("") == []
    assert parse_studio_instances("not json at all") == []
    assert parse_studio_instances('{"unexpected": 1}') == []


def test_parse_studio_instances_accepts_alternative_spellings_and_a_missing_name():
    """A live bridge has been seen returning an entry with no `name` at all."""
    instances = parse_studio_instances(
        '[{"studio_id": "a", "place_name": "P"}, "bare-id", {"id": "c"}]'
    )
    assert [item.identifier for item in instances] == ["a", "bare-id", "c"]
    assert instances[1].describe() == "bare-id"
    assert instances[2].describe() == "c"


def test_instance_names_cannot_carry_escape_sequences_into_the_terminal():
    hostile = StudioInstance(identifier="studio-1", name="Place\x1b]0;pwned\x07")
    assert hostile.describe() == "studio-1 (Place)"


def test_requested_instance_matches_id_then_name_then_substring():
    instances = [
        StudioInstance("studio-1", "Baseplate"),
        StudioInstance("studio-2", "Obby Tower"),
    ]
    assert match_requested_instance(instances, "studio-2") == "studio-2"
    assert match_requested_instance(instances, "baseplate") == "studio-1"
    assert match_requested_instance(instances, "Obby") == "studio-2"


def test_an_ambiguous_studio_name_is_an_error_not_a_first_hit():
    instances = [
        StudioInstance("studio-1", "Tower Defense"),
        StudioInstance("studio-2", "Tower Defense copy"),
    ]
    with pytest.raises(ToolDiscoveryError, match="matches 2 instances"):
        match_requested_instance(instances, "tower")


def test_an_unknown_studio_is_rejected_with_the_known_list():
    instances = [StudioInstance("studio-1", "Baseplate")]
    with pytest.raises(ToolDiscoveryError, match="Registered: studio-1"):
        match_requested_instance(instances, "nope")


def test_instance_ids_are_sanitised_onto_one_line_as_well_as_names():
    """A newline in an id would forge an extra entry in the list a caller chooses from."""
    hostile = StudioInstance(identifier="studio-1\nstudio-2 (Fake)", name="Place‮eht")
    assert hostile.describe() == "studio-1 studio-2 (Fake) (Placeeht)"
    assert "\n" not in hostile.describe()
