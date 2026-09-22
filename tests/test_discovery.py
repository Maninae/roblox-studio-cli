"""Unit tests for runtime discovery: tool matching, argument matching, instance parsing.

These are pure functions over a `tools/list` payload, so they need neither a
subprocess nor Roblox. They pin the behaviour that protects the CLI from a
Roblox rename: intent maps onto whatever names the live server reports.
"""

import pytest

from roblox_studio_cli.client import ToolDefinition
from roblox_studio_cli.discovery import (
    LUAU_CODE_ARGUMENT_NAMES,
    LUAU_TOOL_NAME_KEYWORDS,
    SCREENSHOT_TOOL_NAME_KEYWORDS,
    ToolDiscoveryError,
    check_required_arguments,
    find_argument_name,
    find_tool_by_keywords,
    parse_arguments_option,
    parse_studio_instances,
)


def build_tool(name: str, properties: dict, required: list[str]) -> ToolDefinition:
    """A ToolDefinition with the JSON Schema shape the real server sends."""
    return ToolDefinition(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": properties, "required": required},
    )


LUAU_TOOL = build_tool(
    "execute_luau",
    {"code": {"type": "string"}, "datamodel_type": {"type": "string"}, "studio_id": {"type": "string"}},
    ["code", "datamodel_type", "studio_id"],
)
CAPTURE_TOOL = build_tool("screen_capture", {"studio_id": {"type": "string"}}, ["studio_id"])


def test_find_tool_matches_the_specific_keyword_first():
    """`screen_capture` must win over a tool that only matches the loose `capture`."""
    tools = [build_tool("capture_metrics", {}, []), CAPTURE_TOOL]
    found = find_tool_by_keywords(tools, SCREENSHOT_TOOL_NAME_KEYWORDS, "screen capture")
    assert found.name == "screen_capture"


def test_find_tool_survives_a_rename():
    """A renamed Luau tool is still found through the looser keyword."""
    renamed = build_tool("run_lua_script", {"source": {"type": "string"}}, ["source"])
    found = find_tool_by_keywords([renamed], LUAU_TOOL_NAME_KEYWORDS, "Luau execution")
    assert found.name == "run_lua_script"


def test_find_tool_lists_alternatives_when_nothing_matches():
    with pytest.raises(ToolDiscoveryError, match="insert_asset"):
        find_tool_by_keywords(
            [build_tool("insert_asset", {}, [])], LUAU_TOOL_NAME_KEYWORDS, "Luau execution"
        )


def test_find_argument_prefers_the_known_name():
    assert find_argument_name(LUAU_TOOL, LUAU_CODE_ARGUMENT_NAMES, fall_back_to_required=True) == "code"


def test_find_argument_falls_back_to_the_required_string():
    """When the code field is renamed to something unknown, shape picks it up."""
    renamed = build_tool("run_lua_script", {"body": {"type": "string"}}, ["body"])
    assert find_argument_name(renamed, LUAU_CODE_ARGUMENT_NAMES, fall_back_to_required=True) == "body"


def test_find_argument_returns_none_when_the_fallback_is_off():
    assert find_argument_name(CAPTURE_TOOL, LUAU_CODE_ARGUMENT_NAMES, fall_back_to_required=False) is None


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


def test_parse_studio_instances_accepts_alternative_spellings():
    instances = parse_studio_instances('[{"studio_id": "a", "place_name": "P"}, "bare-id"]')
    assert [item.identifier for item in instances] == ["a", "bare-id"]
    assert instances[1].describe() == "bare-id"
