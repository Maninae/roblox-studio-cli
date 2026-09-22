"""The exception taxonomy, and the exit code each class maps onto.

One rule decides every exit code `roblox-studio` produces, and it is keyed on
the exception class rather than on which command raised it:

- `StudioRequestError` exits 2. The caller asked for something that cannot be
  carried out as written: an unknown tool, malformed `--args`, an ambiguous
  `--studio`, no Luau source, an unreadable `--file`, an unwritable `--out`.
- Every other `StudioMcpError` exits 1. The environment is not ready: the proxy
  binary is missing, the Studio MCP toggle is off, no Studio instance attached,
  the tool itself reported a failure, or the server returned a JSON-RPC error.

The taxonomy lives in its own leaf module so the transport, the discovery layer
and the image writer can all raise the same classes without importing each
other. `main.exit_code_for` is the only place that turns a class into a number.
"""


class StudioMcpError(Exception):
    """Base class for every failure this package raises. Exits 1."""


class StudioMcpTimeoutError(StudioMcpError):
    """The proxy did not answer a request before its deadline."""


class StudioNotConnectedError(StudioMcpError):
    """The proxy is alive but Studio never exposed any tools.

    Almost always the "Enable Studio as MCP server" toggle being off, or Studio
    being closed or signed out. The message carries the enable instructions.
    """


class StudioNotAttachedError(StudioMcpError):
    """Tools are available, but no Studio instance attached inside the wait window.

    Distinct from `StudioNotConnectedError`: the bridge works and the fix is
    about Studio itself (open a place, wait a moment), not about the toggle.
    """


class StudioMcpProtocolError(StudioMcpError):
    """The server answered with a JSON-RPC `error` object, or with an unreadable frame.

    `message` is already sanitised by the caller, since it is server-controlled
    text that ends up on a terminal.
    """

    def __init__(self, code: int, message: str, data: object = None):
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


class StudioRequestError(StudioMcpError):
    """The caller's request is malformed, so the CLI exits 2 rather than 1.

    Subclassed by `discovery.ToolDiscoveryError`; raised directly for file and
    argument problems that never reach the bridge.
    """
