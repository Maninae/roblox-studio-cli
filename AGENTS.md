# AGENTS.md

Orientation for anyone (human or agent) changing this repo. The README explains what the tool does; this file explains how it is built and what must stay true.

## Module map

`src/roblox_studio_cli/`, in dependency order. Nothing imports upward, so the list is also a reading order.

| Module | Lines | Responsibility |
| --- | --- | --- |
| `errors.py` | 62 | The exception taxonomy, and the exit code each class maps onto. A leaf, so every layer can raise the same classes. |
| `terminal.py` | 57 | Strip terminal control sequences out of server-controlled text before echoing it. |
| `mcp_payloads.py` | 238 | The payload shapes a server answers with: tool definitions, tool results, images, the JSON-RPC error envelope. |
| `client.py` | 500 | Transport only. Spawn the proxy, run the handshake, frame JSON-RPC over stdin/stdout, drain stderr, reap the child. |
| `discovery.py` | 472 | Which live tool to call, with which arguments, on which Studio instance. Includes the attach poll. |
| `image_output.py` | 118 | Where a returned image lands on disk, and the refusals on the way (traversal, symlink, clobber). |
| `doctor_report.py` | 162 | The `doctor` health check: gather the four facts, render them, name the verdict. |
| `main.py` | 462 | Typer commands, output, exit codes. No protocol knowledge. |

Keep every module under ~500 lines and `main.py` near 450. When one grows, extract a responsibility you can name in a short phrase, not an arbitrary half.

## Invariants

**Runtime discovery. Never hardcode a tool name or an argument key.** Roblox iterates on this surface; a name in a constant is a bug waiting for the next Studio release. Every lookup starts from the `tools/list` payload that this run received. Add a new convenience command by declaring a `ToolIntent` in `discovery.py` (purpose, name keywords, expected arguments), never by writing `"execute_luau"` in `main.py`.

Matching is strict on purpose, because the failure it prevents is calling the wrong tool on a live place:
- Whole tokens, never substrings. `evaluate_expression` contains "lua"; it must not answer a Luau request.
- Keywords name the action as well as the subject: `list_studios`, not `studios`; `get_state`, not `state`. A `close_studios` or a `reset_state` must never win a read.
- Outside an exact name hit, a candidate must declare one of the intent's expected arguments and must be the only candidate. Two plausible tools is an error naming both.

**Sanitise everything the bridge said.** Tool output, tool names and descriptions, instance and place names, the proxy's stderr: all of it can carry terminal escape sequences. It reaches a terminal only through `terminal.echo_server_text` or after `sanitize_terminal_text`, never a bare `typer.echo`. `--json` is exempt: `json.dumps` escapes control characters already.

**Never cache a Studio instance id.** Studio attaches to a client a few seconds after it connects, instances open and close between commands, and ids do not survive a Studio restart. `wait_for_studio_instances` polls every 0.5s for up to 12s (bounded by `--timeout`), treating both an empty list and an error from the lister as "not yet".

**Bound both pipes.** stdout is capped at `MAX_MESSAGE_BYTES` (64 MB) with the scan resuming where it stopped; stderr uses a capped `readline` into a ring buffer. A proxy that never sends a newline must not grow this process.

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

Against real Studio, the end-to-end check is `roblox-studio doctor`, then `state`, then `luau 'return game.Name'`. Expect about three seconds per command: that is Studio attaching, not the CLI being slow.

## Conventions

Absolute imports only. No leading underscores. A docstring on every module, class and function. The version lives once, in `src/roblox_studio_cli/__init__.py`; `pyproject.toml` and `client.CLIENT_VERSION` both read it.
