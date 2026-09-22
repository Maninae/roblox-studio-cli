# roblox-studio

Roblox Studio 0.739 and later ships an MCP server inside the application itself, so a Studio you already have open can run Luau, read its data model, and capture its viewport on request. Reaching it normally means wiring an MCP client and registering that server in every session. `roblox-studio` wraps the same bridge as a plain command, so an agent or a person can drive a live Studio session straight from a shell: one process, one call, text or JSON back.

```console
$ roblox-studio luau 'return #game:GetService("Workspace"):GetChildren()'
12
```

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Or, for a command on your PATH without a venv to remember:

```bash
pipx install roblox-studio-cli
```

Python 3.10 or newer. macOS is the tested platform, since that is where the Studio binary path below applies.

## Enable the bridge in Studio (once)

1. Open Roblox Studio and sign in.
2. Open a place. A Studio sitting on the start page registers no instance and nothing can be called.
3. Assistant menu > three dots > Manage MCP Servers > **Enable Studio as MCP server**.
4. If the toggle was already on but nothing connects, quit and relaunch Studio so it takes effect.

Then check the whole chain at once:

```console
$ roblox-studio doctor
Binary:    /Applications/RobloxStudio.app/Contents/MacOS/StudioMCP (found)
Handshake: RobloxStudio 1.0.0
Tools:     28 available
Instances: 8f3c1b2e (Baseplate)

CONNECTED (28 tools, 1 instance)
```

The two failures need different fixes, so the verdict names which one it is. `NOT CONNECTED` means the toggle is off and no tools arrived. `NOT READY` means the bridge is up but no Studio instance registered, so open a place.

## Commands

| Command | What it does | Example |
| --- | --- | --- |
| `doctor` | Binary, handshake, tools, instances, verdict. `status` is an alias. | `roblox-studio doctor` |
| `instances` | Which Studio processes the bridge can see. | `roblox-studio instances` |
| `tools` | Every tool this Studio build exposes, with argument names. | `roblox-studio tools` |
| `luau` | Run Luau in the live data model. | `roblox-studio luau 'return workspace.Name' --context Edit` |
| `screenshot` | Capture the viewport to a PNG. | `roblox-studio screenshot --out /tmp/studio.png` |
| `state` | The open place, play mode, selection. | `roblox-studio state` |
| `play` | Enter or leave play mode. | `roblox-studio play --start` |
| `call` | Any tool by name, raw JSON arguments. | `roblox-studio call insert_asset --args '{"asset_id": 123}'` |

Shared flags:

- `--json` on any subcommand prints the raw MCP result instead of the formatted text.
- `--studio <id-or-name>` picks the target when several Studio windows are open. `ROBLOX_STUDIO_ID` does the same. With exactly one open, it is inferred.
- `--timeout <seconds>` bounds a single call.
- `--args '<json>'` adds or overrides arguments on any subcommand, which is the escape hatch when a Studio build wants something the flags do not cover.

`luau` takes its source three ways: as an argument, from a file with `--file script.luau`, or from stdin with `-`.

```bash
echo 'return #workspace:GetDescendants()' | roblox-studio luau -
roblox-studio luau --file scripts/audit.luau --context Server
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The call succeeded. |
| 1 | Studio is not ready (MCP toggle off, no place open) or the tool itself reported a failure. |
| 2 | The request cannot be carried out as asked: unknown tool, malformed `--args`, no Studio instance to target, or several instances and no `--studio`. |

## How it works

The Studio proxy binary lives inside the application bundle at `/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP`. Set `ROBLOX_STUDIO_MCP_BIN` if a Studio update moves it. This CLI spawns that binary, speaks MCP revision 2024-11-05 as newline-delimited JSON-RPC 2.0 over stdin and stdout, and shuts it down when the command ends.

Tool names and argument keys are never hardcoded. Every run reads `tools/list` and matches intent onto whatever the live server reports, so a rename from `execute_luau` to something else, or from `code` to `source`, keeps working. Every Studio tool except the instance lister takes a required `studio_id`; the CLI fills that in by asking which instances are registered, and nothing about that answer is cached, because a Studio can open or close between two commands.

The failure mode worth knowing: with the toggle off, the proxy answers the MCP handshake normally and then goes silent on `tools/list`, logging one warning on stderr after about twenty seconds. A raw MCP client reports that as a timeout. `doctor` reports it as the toggle.

## Where this fits

Verified 2026-09-22.

| Tool | What it is | Runs Luau in a live Studio session? | Installs into Studio? | Shell-friendly one-shot calls? | Knows Studio (instances, toggle, diagnosis)? | Status |
| --- | --- | --- | --- | --- | --- | --- |
| **`roblox-studio`** (this) | CLI over the MCP server built into Studio | Yes | Nothing | Yes, argument-shaped subcommands, Luau from stdin or file | Yes, instance resolution and a doctor that names the toggle | Active |
| [Studio's built-in MCP server](https://create.roblox.com/docs/studio/mcp) | The bridge itself | Yes | Built in | No, MCP JSON-RPC over stdio only | n/a | Shipped in Studio |
| [Roblox/studio-rust-mcp-server](https://github.com/Roblox/studio-rust-mcp-server) | Roblox's earlier open-source MCP server plus plugin | Yes | A plugin | No | No | Archived April 2026 in favour of the built-in server |
| [revvy02/rodeo](https://github.com/revvy02/rodeo) | Studio CLI and Luau runtime | Yes | Its own Studio plugin, with StudioMCP used only for elevated context | Yes | Partial | Active, 21 stars, breaking changes may happen |
| [mcporter](https://github.com/openclaw/mcporter), [inspector --cli](https://github.com/modelcontextprotocol/inspector), [f/mcptools](https://github.com/f/mcptools), [wong2/mcp-cli](https://github.com/wong2/mcp-cli) | Generic MCP-to-shell bridges | Yes, when pointed at the Studio binary | Nothing | Yes, but raw JSON arguments and a fresh handshake per call | No, opaque errors when the toggle is off and no instance handling | Active (mcptools stale since Dec 2025) |
| [Rojo](https://github.com/rojo-rbx/rojo), [Argon](https://github.com/argon-rbx/argon) | File sync into Studio | No | A plugin | n/a | n/a | Active |
| [Lune](https://github.com/lune-org/lune) | Standalone Luau runtime | No Studio data model | No | Yes | n/a | Active |
| [rojo-rbx/run-in-roblox](https://github.com/rojo-rbx/run-in-roblox) | Launches its own Studio instance to run a script | Not your live session | No | Yes | No | Dormant since March 2024 |
| [Open Cloud Luau Execution API](https://create.roblox.com/docs/cloud/guides/luau-execution), [rbxcloud](https://github.com/Sleitnick/rbxcloud) | Runs Luau on a cloud server against a published place | No live Studio | No | Yes | n/a | Active |

The niche: an agent that already has a Studio open wants shell-shaped calls with the Studio-specific failure modes explained, without installing a plugin and without registering an MCP server in every session. That is the whole scope, and a generic MCP bridge is the better choice when you need to reach many different MCP servers.

## Development

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

The suite runs with no Roblox installed. `tests/fake_studio_mcp_server.py` speaks the same protocol and simulates the situations that are awkward to reproduce by hand: one Studio open, none open, two open, and the toggle left off.

## License

MIT
