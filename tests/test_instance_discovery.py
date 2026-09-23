"""Unit tests for instance discovery: reading the lister, and resolving `--studio`.

Two halves, and neither needs Roblox. Parsing is a pure function over the text
the bridge answers with, which has been seen in several shapes and is allowed to
be nonsense. Resolution is the rest: matching a `--studio` value against what
registered, and the attach poll, which is driven here by a stub lister that
records the timeout each poll was handed.
"""

import pytest
from fake_studio_mcp_server import (
    HOSTILE_LISTER_ID_DIGITS,
    HOSTILE_LISTER_NESTING_DEPTH,
)

from roblox_studio_cli import instance_discovery as instance_discovery_module
from roblox_studio_cli.errors import StudioMcpTimeoutError
from roblox_studio_cli.instance_discovery import (
    MINIMUM_ATTACH_POLL_SECONDS,
    STUDIO_ATTACH_POLL_INTERVAL_SECONDS,
    STUDIO_ATTACH_TIMEOUT_SECONDS,
    StudioAttachOutcome,
    StudioInstance,
    attach_failure_message,
    match_requested_instance,
    no_studio_instance_message,
    parse_studio_instances,
    wait_for_studio_instances,
)
from roblox_studio_cli.mcp_payloads import ToolCallResult, ToolDefinition
from roblox_studio_cli.terminal import MAX_DIAGNOSTIC_TEXT_CHARS
from roblox_studio_cli.tool_discovery import ToolDiscoveryError

# A caller's timeout far longer than the attach window, so the two cannot be
# confused for each other, and a window short enough to spend in a test.
GENEROUS_CALL_TIMEOUT_SECONDS = 120.0
SHORT_ATTACH_WINDOW_SECONDS = 1.0


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


def test_a_lister_payload_nested_past_any_reader_is_no_instances_rather_than_a_crash():
    """The frame's depth scan cannot help here, and correctly does not try.

    The lister's answer arrives as a JSON string inside the frame, so the
    brackets in it are text to the scan that guards `parse_frame` and structure
    to the parse that happens here. 200,000 of them reached the C decoder and
    came back as `RecursionError`, which is not a `ValueError` and was not
    caught, so it escaped `luau`, `instances` and `doctor` alike.
    """
    assert parse_studio_instances("[" * HOSTILE_LISTER_NESTING_DEPTH) == []


def test_a_lister_payload_of_megabytes_of_brackets_is_no_instances_too():
    """The depth scan refuses a frame like this, and a refusal is not this parse's answer.

    Every other unreadable payload here reads as "nothing registered", because
    the alternative is a `doctor` that dies on the way to its verdict over a
    bridge that is merely answering nonsense. The scan borrowed from `framing`
    raises on megabytes of structure rather than returning, so this one has to
    be caught as well as the parser's own refusals.
    """
    assert parse_studio_instances("[]" * (2 * 2**20)) == []


def test_a_lister_id_longer_than_python_will_convert_is_no_instances_too():
    """Past 4300 digits `int()` refuses the conversion, with a plain ValueError.

    Legal JSON, unreadable by this interpreter, and not a `JSONDecodeError`, so
    it escaped the same three commands the nesting above did.
    """
    giant_id = '{"studios": [{"id": ' + "9" * HOSTILE_LISTER_ID_DIGITS + '}]}'
    assert parse_studio_instances(giant_id) == []


def test_a_lister_payload_carrying_infinity_is_refused_the_way_a_frame_is():
    """`NaN` and `Infinity` are Python extensions, not JSON, on both parses.

    The frame parse refuses them so `--json` cannot re-emit something a strict
    consumer chokes on. This payload is server bytes too and takes the same
    hooks, so a lister answering with one reads as no instances.
    """
    assert parse_studio_instances('{"studios": [{"id": "a", "name": Infinity}]}') == []


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


def test_the_registered_list_is_capped_in_count_as_well_as_per_row():
    """Each row was already capped; the number of rows is the bridge's choice too."""
    crowd = [StudioInstance(f"studio-{index}", f"Place{index}") for index in range(60)]
    with pytest.raises(ToolDiscoveryError) as raised:
        match_requested_instance(crowd, "nope")
    message = str(raised.value)
    assert "and 40 more" in message
    assert "studio-59" not in message


def test_instance_ids_are_sanitised_onto_one_line_as_well_as_names():
    """A newline in an id would forge an extra entry in the list a caller chooses from."""
    hostile = StudioInstance(identifier="studio-1\nstudio-2 (Fake)", name="Place‮eht")
    assert hostile.describe() == "studio-1 studio-2 (Fake) (Placeeht)"
    assert "\n" not in hostile.describe()


def test_a_giant_instance_name_is_capped_before_it_is_printed():
    """An instance name is chrome around an answer, and the bridge chooses its length."""
    described = StudioInstance(identifier="studio-1", name="n" * 500_000).describe()
    assert len(described) < MAX_DIAGNOSTIC_TEXT_CHARS * 2
    assert described.startswith("studio-1 (nnn")
    assert described.endswith("...)")


def test_a_giant_instance_id_is_capped_too():
    described = StudioInstance(identifier="i" * 500_000, name="").describe()
    assert len(described) == MAX_DIAGNOSTIC_TEXT_CHARS
    assert described.endswith("...")


def test_the_quoted_bridge_error_is_capped():
    """The attach advice quotes whatever the lister last said, which can be anything."""
    outcome = StudioAttachOutcome(last_error_text="e" * 500_000)
    message = attach_failure_message(outcome)
    advice = no_studio_instance_message(outcome.window_seconds)
    assert "The bridge last answered:" in message
    assert len(message) < len(advice) + MAX_DIAGNOSTIC_TEXT_CHARS + 40

class HandAdvancedClock:
    """The `time` the attach poll sees, advanced only by its own sleeps.

    Stands in for the module's `time` (it needs `monotonic` and `sleep`, and
    nothing else), so the arithmetic in the loop is exactly what the test
    wrote down. On the real clock the interesting cases live in a few
    milliseconds of window, and `time.sleep` overshoots by 15 to 40 ms here, so
    a test aimed at that band lands somewhere else one run in five.
    """

    def __init__(self, start: float = 1000.0):
        """Start at an arbitrary epoch; only the differences matter."""
        self.now = start

    def monotonic(self) -> float:
        """The current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance by exactly what was asked for, and return at once."""
        self.now += seconds


class NeverAttachingClient:
    """A lister that answers promptly and always says no Studio is registered.

    Records the timeout each poll was given, which is the thing under test: a
    poll is entitled to what is left of the attach window, not to the caller's
    whole `--timeout`.
    """

    def __init__(self, failure: Exception | None = None):
        self.poll_timeouts: list[float] = []
        self.failure = failure

    def call_tool(self, name: str, arguments: dict, timeout: float) -> ToolCallResult:
        self.poll_timeouts.append(timeout)
        if self.failure is not None:
            raise self.failure
        return ToolCallResult(is_error=True, text="Unable to reach Roblox Studio")

# The instance lister exactly as `tools/list` reports it: no arguments at all,
# which is what makes it the one tool callable before an instance is resolved.
LISTER_TOOLS = [
    ToolDefinition(
        name="list_roblox_studios",
        description="list_roblox_studios description",
        input_schema={"type": "object", "properties": {}, "required": []},
    )
]


def test_the_attach_message_names_the_window_that_was_actually_waited():
    """`--timeout 1` waits one second, and used to report twelve.

    The window is the smaller of the attach window and the caller's timeout, so
    the number in the message is a fact about this run. Naming the default sent
    the reader looking for eleven seconds that never happened.
    """
    outcome = StudioAttachOutcome(window_seconds=SHORT_ATTACH_WINDOW_SECONDS)

    message = attach_failure_message(outcome)

    assert "within 1 s" in message
    assert f"{STUDIO_ATTACH_TIMEOUT_SECONDS:g} s" not in message


def test_no_poll_may_outlive_the_window_it_shares():
    """A poll given the whole `--timeout` can run past the window it was bounded by.

    The wait promises the smaller of the two, but each poll used to carry the
    caller's timeout, so a poll starting just inside a 12 s window could run
    another 120, and a command that promised 12 s took over two minutes.
    """
    client = NeverAttachingClient()

    outcome = wait_for_studio_instances(
        client,
        LISTER_TOOLS,
        timeout=GENEROUS_CALL_TIMEOUT_SECONDS,
        attach_timeout=SHORT_ATTACH_WINDOW_SECONDS,
    )

    assert not outcome.attached
    assert outcome.window_seconds == SHORT_ATTACH_WINDOW_SECONDS
    assert client.poll_timeouts, "the lister was never polled"
    assert max(client.poll_timeouts) <= SHORT_ATTACH_WINDOW_SECONDS, client.poll_timeouts
    assert client.poll_timeouts == sorted(client.poll_timeouts, reverse=True), (
        "each poll should get what is left of the window, so the budgets shrink"
    )


def test_a_lister_that_never_answers_is_nothing_attached_rather_than_a_transport_error():
    """A poll bounded by the window can only time out once the window is gone.

    Which is the outcome this function exists to report, so it reports it: the
    caller gets the "no Studio attached" advice, not a timeout naming a fraction
    of a second nobody asked for.
    """
    client = NeverAttachingClient(failure=StudioMcpTimeoutError("no response to 'tools/call'"))

    outcome = wait_for_studio_instances(
        client,
        LISTER_TOOLS,
        timeout=GENEROUS_CALL_TIMEOUT_SECONDS,
        attach_timeout=SHORT_ATTACH_WINDOW_SECONDS,
    )

    assert not outcome.attached
    spent = SHORT_ATTACH_WINDOW_SECONDS - STUDIO_ATTACH_POLL_INTERVAL_SECONDS
    assert outcome.elapsed_seconds >= spent, "it gave up before the window was gone"


def test_no_poll_is_handed_a_deadline_that_has_already_passed():
    """The sleep between polls can land on the deadline, and one used to poll anyway.

    A poll with no time left cannot succeed: it spends a request id, fails
    inside the read loop before `select` returns anything, and logs that the
    lister did not answer inside the window. The lister was never asked. With a
    1 s window and a 0.5 s interval the arithmetic lands there exactly, which is
    what this reproduces.
    """
    client = NeverAttachingClient()

    wait_for_studio_instances(
        client,
        LISTER_TOOLS,
        timeout=GENEROUS_CALL_TIMEOUT_SECONDS,
        attach_timeout=SHORT_ATTACH_WINDOW_SECONDS,
    )

    assert min(client.poll_timeouts) > 0, client.poll_timeouts


def test_a_window_too_thin_to_poll_is_spent_rather_than_polled():
    """A sliver of a window is not a poll, and spending one blamed the bridge for it.

    Handed a microsecond, `call_tool` gets as far as `write_all` with its
    deadline already gone, writes nothing, and reports "the Studio MCP proxy
    stopped reading its input (0 of 112 bytes written)". That is a transport
    fault named for a bridge that was answering fine, and it then rode into the
    attach advice as the thing the bridge "last answered". Below the floor the
    window is simply over.
    """
    client = NeverAttachingClient()

    outcome = wait_for_studio_instances(
        client,
        LISTER_TOOLS,
        timeout=GENEROUS_CALL_TIMEOUT_SECONDS,
        attach_timeout=MINIMUM_ATTACH_POLL_SECONDS / 2,
    )

    assert not outcome.attached
    assert client.poll_timeouts == [], "a window under the floor still bought a poll"
    assert "stopped reading its input" not in attach_failure_message(outcome)


def test_the_floor_applies_between_polls_and_not_only_to_the_first(monkeypatch):
    """A window one interval plus a sliver long used to spend the sliver on a poll.

    The sleep between polls is capped at what is left of the window, so it
    usually lands on the deadline and the wait ends there. It does not when
    more than one interval remains: the sleep takes a full 0.5 s and the next
    pass inherits whatever the window had over that. A 0.53 s window handed
    that pass 0.03 s, which buys nothing and reads as a transport fault.
    """
    clock = HandAdvancedClock()
    monkeypatch.setattr(instance_discovery_module, "time", clock)
    client = NeverAttachingClient()
    sliver_window = STUDIO_ATTACH_POLL_INTERVAL_SECONDS + MINIMUM_ATTACH_POLL_SECONDS * 0.6

    wait_for_studio_instances(
        client,
        LISTER_TOOLS,
        timeout=GENEROUS_CALL_TIMEOUT_SECONDS,
        attach_timeout=sliver_window,
    )

    assert client.poll_timeouts, "the lister was never polled"
    assert min(client.poll_timeouts) >= MINIMUM_ATTACH_POLL_SECONDS, client.poll_timeouts


def test_a_lister_that_never_answers_says_so_instead_of_blaming_the_place():
    """Silence and "no studios" are the same outcome and different faults.

    Both end as "no Studio instance attached", and the advice under that line is
    about opening a place and checking the toggle. A bridge that answered
    nothing at all deserves to be quoted the way a bridge that answered an error
    already is, otherwise the one reading it goes looking at Studio.
    """
    client = NeverAttachingClient(failure=StudioMcpTimeoutError("no response to 'tools/call'"))

    outcome = wait_for_studio_instances(
        client,
        LISTER_TOOLS,
        timeout=GENEROUS_CALL_TIMEOUT_SECONDS,
        attach_timeout=SHORT_ATTACH_WINDOW_SECONDS,
    )

    assert "no response to 'tools/call'" in attach_failure_message(outcome)
