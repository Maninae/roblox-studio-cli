# AGENTS.md

Orientation for anyone (human or agent) changing this repo. The README explains what the tool does; this file explains how it is built and what must stay true.

## Module map

`src/roblox_studio_cli/`, in dependency order. Nothing imports upward, so the list is also a reading order.

| Module | Responsibility |
| --- | --- |
| `errors.py` | The exception taxonomy, the three exit statuses, and the exit code each class maps onto. A leaf, so every layer can raise the same classes. |
| `terminal.py` | Strip terminal control sequences out of server-controlled text before echoing it. |
| `mcp_payloads.py` | The payload shapes a server answers with: tool definitions, tool results, images, the JSON-RPC error envelope. |
| `framing.py` | Stdout bytes to JSON-RPC messages: line buffering, defensive parsing, the byte budgets, the large-frame id peek, and which parsed frame answers the request in flight. Knows nothing of processes. |
| `stderr_capture.py` | The proxy's other pipe: drain it on a thread into a bounded ring buffer, quote the last few lines into an exception, and spot the one WARN that means Studio's MCP toggle is off. Knows nothing of requests. |
| `client.py` | Process and protocol. Spawn the proxy, run the handshake, write requests, read the answer, decline the questions the server asks back, reap the child. Owns one `ProxyStderrCapture` and quotes it into its own errors. |
| `tool_discovery.py` | Which live tool to call, and which of its arguments carries the value. Token matching over a `tools/list` payload, the destructive-name deny-list, and the `ToolIntent` each convenience command declares. Pure functions; no client. |
| `instance_discovery.py` | Which Studio instance to call it on: the attach poll, the lister payload parser, and `--studio` resolution. Asks `tool_discovery` for one thing, the lister. |
| `image_output.py` | Where a returned image lands on disk, and the refusals on the way (traversal, symlink, clobber). |
| `luau_source.py` | Where one `luau` call's source comes from: an argument, stdin, or a file, with the same byte cap on the two that read somebody else's bytes. |
| `display_wake.py` | macOS only: wake the display for a capture and hold it awake, because a dark display captures nothing. |
| `json_output.py` | How `--json` reaches stdout: one compact line, and strict JSON. A leaf, imported by both `main` and `doctor_report`. |
| `result_output.py` | What one tool result becomes on the way out: the failure gate, the images, and the two output modes. |
| `doctor_report.py` | The `doctor` health check: gather the four facts, render them, name the verdict. |
| `main.py` | Typer commands, output, exit codes. No protocol knowledge. |

Sizes are a budget, not a suggestion: `wc -l src/roblox_studio_cli/*.py` should show nothing over ~600 lines. When one grows, extract a responsibility you can name in a short phrase, not an arbitrary half. `framing.py` came out of `client.py` that way, and the seam it left is worth keeping: framing takes bytes and gives back messages, so a framing bug reproduces by calling `feed()` with a literal, and the client stays the only module that knows there is a child process. `main.py` went the same way when threading `--timeout` through every command pushed it to 602: what came out is `result_output`, the only module that reads a `ToolCallResult`, so the rules about a call that failed and about `--json` next to `--out` are decided once instead of in each command. What is left there is app plumbing (the flags, the error decorator, the exit-code rule) and eight commands, and the plumbing is the seam to take next, because the commands are what the file is for.

`client.py` came off that ceiling the way `framing` did, and `stderr_capture` is what came out: the ring buffer, the sanitised suffix an exception quotes, and the no-tools marker that tells the toggle from an ordinary silence. None of it knows a request exists, so the client hands it the child's stderr stream once and reads diagnostics back.

`discovery.py` split along the two halves of its own one-line summary, and is now `tool_discovery` plus `instance_discovery`. The dependency runs one way: resolving an instance means calling the lister, so `instance_discovery` imports `find_tool`, `LIST_INSTANCES_INTENT` and `ToolDiscoveryError`, and nothing runs back the other way. A new convenience command touches only the tool half; a change to how a Studio is chosen touches only the instance half.

## Invariants

**Runtime discovery. Never hardcode a tool name or an argument key.** Roblox iterates on this surface; a name in a constant is a bug waiting for the next Studio release. Every lookup starts from the `tools/list` payload that this run received. Add a new convenience command by declaring a `ToolIntent` in `tool_discovery.py` (purpose, name keywords, expected arguments), never by writing `"execute_luau"` in `main.py`.

Matching is strict on purpose, because the failure it prevents is calling the wrong tool on a live place:
- Whole tokens, never substrings. `evaluate_expression` contains "lua"; it must not answer a Luau request.
- Keywords name the action as well as the subject: `list_studios`, not `studios`; `get_state`, not `state`. Never a bare subject (`luau`, `capture`, `play`): with those on the list, `execute_luau_and_delete_place` and `delete_capture` won the lookup.
- Outside an exact name hit, the candidate's token set must EQUAL the keyword's. A superset is a different tool. The candidate must also declare one of the intent's expected arguments and must be the only candidate. Two plausible tools is an error naming both.
- A destructive verb in a name (`delete`, `remove`, `close`, `reset`, `clear`, `stop`, `destroy`, `wipe`) disqualifies the tool from every convenience command, at both tiers. An intent that legitimately destroys something declares that verb in `allowed_destructive_tokens`; play control is the only one, for `start_stop_play`.

**Sanitise everything the bridge said.** Tool output, tool names and descriptions, instance and place names, the proxy's stderr: all of it can carry terminal escape sequences, invisible or reordering Unicode besides (every Cf format character and the Hangul fillers), lone surrogates that no UTF-8 stdout can encode (Cs), and runs of combining marks that draw over the rows around them (Mn, Mc and Me, trimmed to three per base character). It reaches a terminal only through `terminal.echo_server_text` or after `sanitize_terminal_text`, never a bare `typer.echo`. Anything printed as a fixed-width row or an identifier is folded onto one line before it is padded, so a newline cannot forge a second row: `sanitize_diagnostic_line` wherever the bridge chooses the length too, which is most of them, and `sanitize_single_line` only where something upstream already bounded it (`ToolDefinition.description_preview`, and a MIME type that reached the extension warning by being one this build recognises). `--json` is exempt: `json.dumps` escapes control characters already, and a consumer needs the bytes as sent.

**`--json` is one compact line of strict JSON.** Both halves are load-bearing, and both live in `json_output.compact_json`, which every `--json` path goes through. Compact, because `indent=2` writes two spaces per level on every line, so its output is the payload TIMES its nesting depth, and the depth is the server's choice (see the depth bound below). Strict, because `json.loads` accepts `NaN` and `Infinity` as an extension and `json.dumps` writes them back, which is a document a strict consumer refuses: `framing` refuses such a frame on the way in and `allow_nan=False` keeps it true for anything this CLI computed itself.

**Cap the chrome, never the answer.** Length is an attack on its own: a 6 MB serverInfo name of nothing but "x" carries no control character and still scrolls a report away. Two caps, and this list is exhaustive on purpose, because a field nobody capped is the whole bug.

Every field printed AROUND an answer goes through `terminal.sanitize_diagnostic_line` or `truncate_display_text` and stops at `MAX_DIAGNOSTIC_TEXT_CHARS` (200): the serverInfo name and version, each tool name in the `tools` listing, an instance id and place name (`StudioInstance.describe`), the error the bridge last answered the lister with (`attach_failure_message`), the JSON-RPC error message (`mcp_payloads.display_error_message`, measured at 5 MB on stderr), each proxy stderr line quoted into an exception (`stderr_capture.stderr_suffix`, six lines that the ring buffer lets reach 64 KB each), the matched tool's name in `main.missing_argument_message`, and the unrecognised MIME type quoted back by `mcp_payloads.ToolImage.file_extension`. The JSON-RPC error CODE is on this list too, with its own shorter cap (`errors.display_error_code`, 20 characters): a Python integer has no width limit, so `10 ** 4000` printed 4,028 characters of chrome in front of the message. That cap counts code points, not terminal columns, so 200 double-width characters still wrap; the field it exists for is a 6 MB name, not a tidy line.

Every LIST of server-chosen names stops at `MAX_ENUMERATED_NAMES` (20) through `terminal.capped_display_names`, which adds an "and N more" tail: the available tools in `tool_discovery.no_tool_message` and in `call`'s unknown-tool error, the candidate names in that same message, the argument names under each row of the `tools` listing, the arguments in `check_required_arguments` (both the names and the `--args` example built from them) and in the two "has no such argument" errors, the registered instances in `resolve_studio_id` and `match_requested_instance`. A count the bridge reported stays exact ("40 Studio instances are registered"); the rows under it are what gets cut.

Tool output is never capped: it is the thing the caller asked for. Neither is `--json`.

**`--timeout` bounds every exchange, not only the last one.** A command runs three exchanges before the tool call (the handshake, `tools/list`, and the attach poll), and each has a default of its own: 15 s, 30 s and 12 s. A default left in place is a promise broken, and silently, since what the caller sees is a command that runs for half a minute after being told to take one second. `client.handshake_timeout` and `client.tools_list_timeout` are the rule, both `min(caller, default)`, so `--timeout` only ever shortens a wait; `main.studio_client` applies the first and every convenience command applies the second. The exception is the pair whose `--timeout` IS the tools/list wait rather than a cap on it, `doctor` and `tools`, which pass the caller's number straight through. Measured before this held: against a proxy that never answers `tools/list`, `luau 'return 1' --timeout 1` took 30.3 s; against one that never answers `initialize`, 18.1 s.

**Never cache a Studio instance id.** Studio attaches to a client a few seconds after it connects, instances open and close between commands, and ids do not survive a Studio restart. `wait_for_studio_instances` polls every 0.5s for up to 12s (bounded by `--timeout`), treating both an empty list and an error from the lister as "not yet".

**Bound both pipes, and never block on either.** A cap on one frame is not a cap on memory, because parsing JSON multiplies size many times over, so stdout has four bounds, all of them in `framing`: `MAX_MESSAGE_BYTES` (8 MB) per frame with the scan resuming where it stopped, a per-request total (`MAX_REQUEST_TOTAL_BYTES`, and `MAX_TOOLS_LIST_TOTAL_BYTES` across all pages of a list), `MAX_FRAME_CONTAINER_DEPTH` (512) on nesting, and a rule that a frame over `LARGE_FRAME_BYTES` positively carrying another request's id is dropped unparsed. The depth bound is the one that is not about size: depth costs the sender nothing and cost us an object per level plus, while `--json` was indented, two spaces per level on every line, so a 40 KB frame nested 20,000 deep printed 800 MB. It is checked before the parse, by a scan that counts openers in the raw line first (C speed, and the answer for every ordinary frame) and past that walks the bytes once, tracking string and escape state so a brace in tool output stays text, and stopping the moment depth passes the cap. That walk replaced a regex that blanked the string literals first, which a string opened, padded with escaped quotes and never closed backtracked quadratically: 42 s on 128 KB, hours on a whole frame, inside `parse_frame` where `--timeout` cannot reach, so `tools --timeout 3` ran for 85 seconds. That last rule searches for the AWAITED id first and drops a frame only when it is absent from both windows: a large answer quoting some other id in its payload (a place id, an instance record) is still ours, and dropping it used to cost the caller the entire timeout. stderr uses a capped `readline` into a ring buffer, keeping each over-long line's tail. The request write is non-blocking and runs against the same deadline as the read: a proxy that stops reading used to park the process inside `write()` forever once the pipe buffer filled.

**A frame carrying a string `method` is a question, never our answer.** MCP runs in both directions, and a server numbers the requests it makes of its client (`roots/list`, `sampling/createMessage`, `elicitation/create`) from 1 exactly as we number ours, so one can arrive wearing the id we are waiting on. `framing.response_matches_request` refuses any frame with a string `method`, because a JSON-RPC response never has one; read as the answer, such a frame has no `result`, so `tools/list` failed with "returned a non-object result (NoneType)" while the real answer sat one frame behind it. That test is `isinstance(method, str)` in both places that read it, and `parse_frame` refuses a frame whose `method` is anything else: written as `"method" in message` on one side and `isinstance` on the other, the two disagreed about `"method": null`, so such a frame wearing our id was neither taken as the answer nor declined as a question, and the caller waited out the deadline for it. `client.decline_server_request` then answers -32601, since a conformant server waits for a reply to every request it sends. That reply is ONE non-blocking write: it is dropped when the pipe is full and skipped when it would exceed `PIPE_BUF` (512 bytes here, the size at or under which a pipe write is all-or-nothing, so a dropped reply never leaves half a frame on the wire), and the id it echoes is capped by digits as well as by characters. Written on the caller's deadline instead, being polite cost them their answer: a server that asks a burst of questions and then pauses reading stalled the client for the entire deadline at 150 questions and timed out at 700, with the answer already on the pipe.

**Known limits, accepted deliberately.** Three costs a hostile proxy can impose that the budgets bound rather than prevent, all worth knowing before someone reports them as bugs:

- A frame packed with decoy `{"id": N}` objects is parsed rather than dropped, and parsing amplifies it in memory: measured here, 17x for a flat list of them. (Doing it by NESTING those ids is no longer available: past 512 containers the frame is refused unparsed.) Nothing drops such a frame on purpose, because the id peek only drops a LARGE frame that positively answers ANOTHER request, and a decoy id inside a payload must not cost us our own answer (that fix has its own test). `MAX_REQUEST_TOTAL_BYTES` is what bounds this, so the real ceiling is that budget times the amplification, not the budget.
- A flood of LARGE frames addressed to another request is dropped by the id peek and charged nothing, so it can run as long as the proxy likes, and while it runs the process holds a few hundred MB of physical memory. Measured: 8 GB of 8 MB frames peaked at 562 MB of RSS, and at 562 MB whether the flood was 200 frames or 1,000, so it is bounded rather than cumulative. Nothing of it is live. `tracemalloc` reports 0 bytes held and a 16.5 MB peak per frame, which is the frame in the buffer plus the one copy taken out of it (`consume_buffered_lines` slices through a `memoryview`; slicing the bytearray made that two copies and 24.5 MB). The rest is libmalloc holding freed dirty pages in its large-block cache: the same run under `MallocSpaceEfficient=1` peaks at 63 MB, and left alone the allocator gives the pages back within a couple of seconds of the flood stopping (542 MB during, 97 MB one second after, 31 MB three seconds after). It is worth knowing because the number a process monitor shows during such a flood is alarming and is not a leak.
- A flood of bare newlines, or of `{}` frames, costs CPU linear in the bytes sent: each line is split off the buffer and handed to the parser, which is cheap per frame and unbounded only in the same sense the pipe is. Linear is a property every pass before the parse has to keep, the depth scan included, and it is the reason that scan is a byte walk rather than a regex. Memory does not grow (nothing is queued for a blank line), and the per-request byte budget ends the flood: a parsed frame is charged its length, and an empty line `EMPTY_FRAME_BUDGET_BYTES`, which is the 1 byte it really occupied. Charged nothing, as they were, a newline flood was the one hazard on this list that no bound applied to.

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

No Roblox needed: `tests/fake_studio_mcp_server.py` speaks the same wire protocol, selected by `FAKE_STUDIO_MODE`. One test file per module. CI runs the same two commands on macOS against Python 3.10, 3.12 and 3.14: the ends of what `requires-python` allows plus the middle, since what differs between them is syntax and standard-library behaviour rather than this code's logic.

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
| `giant-names` | A server that names itself, its instance, a tool and one of that tool's arguments in 100 KB of clean text, with forty arguments besides. |
| `crowded` | Forty extra tools and forty registered instances, so every enumeration has to stop somewhere. |
| `invalid-utf8` | A stdout line that is not decodable UTF-8, then a normal answer. |
| `stray-flood` | Several 200 KB frames carrying a request id nobody awaits, ahead of the real one. |
| `decoy-id` | A large, legitimate answer quoting another id in its payload before its own. |
| `huge-list` | A `tools/list` answer larger than the whole-list byte budget. |
| `deaf-stdin` | Answers the handshake, then never reads stdin again, so a large request fills the pipe. |
| `handshake-silent` | Reads `initialize` and never answers it, so nothing after it happens. |
| `capture-silent` | Everything works except the capture, which is accepted and never answered. |
| `capture-error` | The capture comes back `isError` with an image attached. |
| `odd-mime` | The capture is an image type no extension fits, so the bytes exist and the file cannot. |
| `odd-serverinfo` | The handshake names the server with a bare string where the spec has an object. |
| `lone-surrogate` | A `\udcff` in the server name, the instance name and a tool result: legal JSON, unencodable as UTF-8. |
| `string-ids` | Every response echoes the request id as a string, which JSON-RPC allows. |
| `client-request-collision` | The server asks the client `roots/list` wearing the id of the request it is about to answer. |
| `question-flood` | Three thousand of those questions ahead of the answer, then a reader that pauses, so every reply past the first pipeful has nowhere to go. |

Against real Studio, the end-to-end check is `roblox-studio doctor`, then `state`, then `luau 'return game.Name'`, then `screenshot --wake-display`. Expect about three seconds per command: that is Studio attaching, not the CLI being slow. A capture against a sleeping display never answers at all, which is what `--wake-display` is for.

## Conventions

Absolute imports only. No leading underscores. A docstring on every module, class and function. The version lives once, in `src/roblox_studio_cli/__init__.py`; `pyproject.toml` and `client.CLIENT_VERSION` both read it.
