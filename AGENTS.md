# AGENTS.md

Orientation for anyone (human or agent) changing this repo. The README explains what the tool does; this file explains how it is built and what must stay true.

## Module map

`src/roblox_studio_cli/`, in dependency order. Nothing imports upward, so the list is also a reading order.

| Module | Responsibility |
| --- | --- |
| `errors.py` | The exception taxonomy, and the exit code each class maps onto. A leaf, so every layer can raise the same classes. |
| `terminal.py` | Strip terminal control sequences out of server-controlled text before echoing it. |
| `mcp_payloads.py` | The payload shapes a server answers with: tool definitions, tool results, images, the JSON-RPC error envelope. |
| `framing.py` | Stdout bytes to JSON-RPC messages: line buffering, defensive parsing, the byte budgets, the large-frame id peek. Knows nothing of processes. |
| `client.py` | Process and protocol. Spawn the proxy, run the handshake, write requests, match responses, drain stderr, reap the child. |
| `discovery.py` | Which live tool to call, with which arguments, on which Studio instance. Includes the attach poll. |
| `image_output.py` | Where a returned image lands on disk, and the refusals on the way (traversal, symlink, clobber). |
| `luau_source.py` | Where one `luau` call's source comes from: an argument, stdin, or a file, with the same byte cap on the two that read somebody else's bytes. |
| `display_wake.py` | macOS only: wake the display for a capture and hold it awake, because a dark display captures nothing. |
| `doctor_report.py` | The `doctor` health check: gather the four facts, render them, name the verdict. |
| `main.py` | Typer commands, output, exit codes. No protocol knowledge. |

Sizes are a budget, not a suggestion: `wc -l src/roblox_studio_cli/*.py` should show nothing over ~600 lines. When one grows, extract a responsibility you can name in a short phrase, not an arbitrary half. `framing.py` came out of `client.py` that way, and the seam it left is worth keeping: framing takes bytes and gives back messages, so a framing bug reproduces by calling `feed()` with a literal, and the client stays the only module that knows there is a child process.

## Invariants

**Runtime discovery. Never hardcode a tool name or an argument key.** Roblox iterates on this surface; a name in a constant is a bug waiting for the next Studio release. Every lookup starts from the `tools/list` payload that this run received. Add a new convenience command by declaring a `ToolIntent` in `discovery.py` (purpose, name keywords, expected arguments), never by writing `"execute_luau"` in `main.py`.

Matching is strict on purpose, because the failure it prevents is calling the wrong tool on a live place:
- Whole tokens, never substrings. `evaluate_expression` contains "lua"; it must not answer a Luau request.
- Keywords name the action as well as the subject: `list_studios`, not `studios`; `get_state`, not `state`. Never a bare subject (`luau`, `capture`, `play`): with those on the list, `execute_luau_and_delete_place` and `delete_capture` won the lookup.
- Outside an exact name hit, the candidate's token set must EQUAL the keyword's. A superset is a different tool. The candidate must also declare one of the intent's expected arguments and must be the only candidate. Two plausible tools is an error naming both.
- A destructive verb in a name (`delete`, `remove`, `close`, `reset`, `clear`, `stop`, `destroy`, `wipe`) disqualifies the tool from every convenience command, at both tiers. An intent that legitimately destroys something declares that verb in `allowed_destructive_tokens`; play control is the only one, for `start_stop_play`.

**Sanitise everything the bridge said.** Tool output, tool names and descriptions, instance and place names, the proxy's stderr: all of it can carry terminal escape sequences, invisible or reordering Unicode besides (every Cf format character and the Hangul fillers), lone surrogates that no UTF-8 stdout can encode (Cs), and runs of combining marks that draw over the rows around them (Mn, Mc and Me, trimmed to three per base character). It reaches a terminal only through `terminal.echo_server_text` or after `sanitize_terminal_text`, never a bare `typer.echo`. Anything printed as a fixed-width row or an identifier uses `sanitize_single_line` instead, before it is padded, so a newline cannot forge a second row. `--json` is exempt: `json.dumps` escapes control characters already, and a consumer needs the bytes as sent.

**Cap the chrome, never the answer.** Length is an attack on its own: a 6 MB serverInfo name of nothing but "x" carries no control character and still scrolls a report away. Two caps, and this list is exhaustive on purpose, because a field nobody capped is the whole bug.

Every field printed AROUND an answer goes through `terminal.sanitize_diagnostic_line` or `truncate_display_text` and stops at `MAX_DIAGNOSTIC_TEXT_CHARS` (200): the serverInfo name and version, each tool name in the `tools` listing, an instance id and place name (`StudioInstance.describe`), the error the bridge last answered the lister with (`attach_failure_message`), the JSON-RPC error message (`mcp_payloads.display_error_message`, measured at 5 MB on stderr), and each proxy stderr line quoted into an exception (`client.stderr_suffix`, six lines that the ring buffer lets reach 64 KB each).

Every LIST of server-chosen names stops at `MAX_ENUMERATED_NAMES` (20) through `terminal.capped_display_names`, which adds an "and N more" tail: the available tools in `discovery.no_tool_message` and in `call`'s unknown-tool error, the candidate names in that same message, the argument names under each row of the `tools` listing, the arguments in `check_required_arguments` (both the names and the `--args` example built from them) and in the two "has no such argument" errors, the registered instances in `resolve_studio_id` and `match_requested_instance`. A count the bridge reported stays exact ("40 Studio instances are registered"); the rows under it are what gets cut.

Tool output is never capped: it is the thing the caller asked for. Neither is `--json`.

**Never cache a Studio instance id.** Studio attaches to a client a few seconds after it connects, instances open and close between commands, and ids do not survive a Studio restart. `wait_for_studio_instances` polls every 0.5s for up to 12s (bounded by `--timeout`), treating both an empty list and an error from the lister as "not yet".

**Bound both pipes, and never block on either.** A cap on one frame is not a cap on memory, because parsing JSON multiplies size many times over, so stdout has three bounds, all of them in `framing`: `MAX_MESSAGE_BYTES` (8 MB) per frame with the scan resuming where it stopped, a per-request total (`MAX_REQUEST_TOTAL_BYTES`, and `MAX_TOOLS_LIST_TOTAL_BYTES` across all pages of a list), and a rule that a frame over `LARGE_FRAME_BYTES` positively carrying another request's id is dropped unparsed. That last rule searches for the AWAITED id first and drops a frame only when it is absent from both windows: a large answer quoting some other id in its payload (a place id, an instance record) is still ours, and dropping it used to cost the caller the entire timeout. stderr uses a capped `readline` into a ring buffer, keeping each over-long line's tail. The request write is non-blocking and runs against the same deadline as the read: a proxy that stops reading used to park the process inside `write()` forever once the pipe buffer filled.

**Known limits, accepted deliberately.** Two costs a hostile proxy can impose that the budgets bound rather than prevent, both worth knowing before someone reports them as bugs:

- A frame packed with decoy `{"id": N}` objects is parsed rather than dropped, and parsing amplifies it in memory: measured here, 17x for a flat list of them and 11x for the same ids nested 20,000 deep. Nothing drops such a frame on purpose, because the id peek only drops a LARGE frame that positively answers ANOTHER request, and a decoy id inside a payload must not cost us our own answer (that fix has its own test). `MAX_REQUEST_TOTAL_BYTES` is what bounds this, so the real ceiling is that budget times the amplification, not the budget.
- A flood of bare newlines, or of `{}` frames, costs CPU linear in the bytes sent: each line is split off the buffer and handed to the parser, which is cheap per frame and unbounded only in the same sense the pipe is. Memory does not grow (nothing is queued for a blank line), and the per-request byte budget ends the flood: a parsed frame is charged its length, and an empty line `EMPTY_FRAME_BUDGET_BYTES`, which is the 1 byte it really occupied. Charged nothing, as they were, a newline flood was the one hazard on this list that no bound applied to.

**Nothing the bridge sends may surface as a traceback.** A frame that cannot be read raises `StudioMcpProtocolError`; the Typer app has `pretty_exceptions_enable=False` (its rich traceback prints frames, and with locals the server bytes inside them); and `main` catches whatever still escapes and prints one sanitised line with the environment exit code.

## Exit codes

One rule, keyed on the exception class in `main.exit_code_for`, never on which command raised it.

| Code | Class | Meaning |
| --- | --- | --- |
| 0 | | Success. |
| 1 | any other `StudioMcpError` | The environment is not ready: binary missing, toggle off, nothing attached, the tool reported `isError`, a server-side JSON-RPC error, a timeout. |
| 2 | `StudioRequestError` (incl. `ToolDiscoveryError`) | The caller's request is malformed: unknown tool, bad `--args`, ambiguous `--studio`, no Luau source, unreadable `--file`, unwritable `--out`. |

Commands carry `@handles_studio_errors`, so the mapping applies identically whether the app is invoked from a shell or from a test runner.

## Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check
```

No Roblox needed: `tests/fake_studio_mcp_server.py` speaks the same wire protocol, selected by `FAKE_STUDIO_MODE`. One test file per module.

| Mode | Simulates |
| --- | --- |
| `connected` | One registered instance (the default). |
| `no-instances` | Tools work, no Studio ever registers. |
| `two-instances` | Two instances, so a target must be named. |
| `no-tools` | Answers `initialize`, never answers `tools/list`, logs the real proxy's WARN. |
| `attach-late` | Lister errors twice, returns an empty list once, then the instance. |
| `attach-never` | Lister keeps erroring until the window expires. |
| `chatty` | Notifications interleaved with responses, and both in a single write. |
| `partial` | One response split mid-JSON across two writes with a delay. |
| `noisy-stderr` | A 200 KB stderr line before answering, then an ordinary line. |
| `malformed` | A non-object stdout frame, a numeric tool name, an `error` member that is a bare string. |
| `hostile-names` | A tool whose name, description and argument key carry newlines, an OSC escape and invisible Unicode. |
| `giant-names` | A server that names itself, and its instance, in 100 KB of clean text. |
| `crowded` | Forty extra tools and forty registered instances, so every enumeration has to stop somewhere. |
| `invalid-utf8` | A stdout line that is not decodable UTF-8, then a normal answer. |
| `stray-flood` | Several 200 KB frames carrying a request id nobody awaits, ahead of the real one. |
| `decoy-id` | A large, legitimate answer quoting another id in its payload before its own. |
| `huge-list` | A `tools/list` answer larger than the whole-list byte budget. |
| `deaf-stdin` | Answers the handshake, then never reads stdin again, so a large request fills the pipe. |
| `capture-silent` | Everything works except the capture, which is accepted and never answered. |
| `capture-error` | The capture comes back `isError` with an image attached. |

Against real Studio, the end-to-end check is `roblox-studio doctor`, then `state`, then `luau 'return game.Name'`, then `screenshot --wake-display`. Expect about three seconds per command: that is Studio attaching, not the CLI being slow. A capture against a sleeping display never answers at all, which is what `--wake-display` is for.

## Conventions

Absolute imports only. No leading underscores. A docstring on every module, class and function. The version lives once, in `src/roblox_studio_cli/__init__.py`; `pyproject.toml` and `client.CLIENT_VERSION` both read it.
