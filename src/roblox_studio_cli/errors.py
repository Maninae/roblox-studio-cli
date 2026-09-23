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

# What a JSON-RPC `code` becomes when the server did not send an integer.
UNKNOWN_ERROR_CODE = -1
# How much of the code is worth printing. A server picks it, Python integers
# have no width limit, and `10 ** 4000` printed 4,028 characters of chrome in
# front of the message the caller actually needs. Same rule as every other
# server-chosen field; this one is a number, so it needs no sanitising, only a
# length.
MAX_ERROR_CODE_CHARS = 20
ERROR_CODE_TRUNCATION_MARKER = "..."


def normalize_error_code(code: object) -> int:
    """The integer a JSON-RPC error carried, or -1 when it carried something else.

    Booleans are excluded explicitly: `True` is an `int` in Python, so an
    `isinstance` check read `"code": true` as error 1, which is a code the spec
    does not define and a claim the server never made.
    """
    if isinstance(code, bool) or not isinstance(code, int):
        return UNKNOWN_ERROR_CODE
    return code


def display_error_code(code: int) -> str:
    """The code as it prints, capped at a length no server chooses."""
    text = str(code)
    if len(text) <= MAX_ERROR_CODE_CHARS:
        return text
    return text[:MAX_ERROR_CODE_CHARS] + ERROR_CODE_TRUNCATION_MARKER


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
    text that ends up on a terminal. `code` is normalised here instead: it is a
    number, so there is nothing in it to sanitise, only a type to check and a
    length to bound.
    """

    def __init__(self, code: object, message: str, data: object = None):
        code = normalize_error_code(code)
        super().__init__(f"JSON-RPC error {display_error_code(code)}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


class StudioRequestError(StudioMcpError):
    """The caller's request is malformed, so the CLI exits 2 rather than 1.

    Subclassed by `discovery.ToolDiscoveryError`; raised directly for file and
    argument problems that never reach the bridge.
    """
