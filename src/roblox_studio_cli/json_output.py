"""How `--json` puts a payload on stdout: compactly, and as strict JSON.

One function, and two reasons it is not just `json.dumps` at each call site.

- `indent=2` costs two spaces per level on every line, so its output grows with
  the payload TIMES its nesting depth rather than with the payload. The depth is
  the server's choice: a 40 KB frame nested 20,000 deep printed 8 MB of `--json`
  at depth 2,000 and 800 MB at 20,000, from a frame small enough to pass every
  byte budget this CLI has. Compact separators make the output linear, and a
  consumer that wants it indented has `jq` right there. (`framing` caps nesting
  on the way in too; this is the other half of the same bug.)
- `allow_nan=False`. `json.loads` accepts `NaN` and `Infinity` as an extension
  and `json.dumps` re-emits them, which is a document strict parsers reject. A
  frame carrying one is refused on the way in, so this is the guard that keeps
  that true no matter which payload reaches here.

Nothing here sanitises: `--json` is the one output that must carry the bytes the
server actually sent, and `json.dumps` escapes control characters already. That
rule lives in `terminal`, which owns everything printed AS text.
"""

import json

# No whitespace at all, so the output is one line whose length is the payload's.
JSON_COMPACT_SEPARATORS = (",", ":")


def compact_json(payload: object) -> str:
    """Render one `--json` payload as a single line of strict JSON.

    Raises:
        ValueError: the payload carries a non-finite float, which is not JSON.
            Frames carrying one are already refused in `framing.parse_frame`, so
            reaching this means a number this CLI computed went non-finite.
    """
    return json.dumps(payload, separators=JSON_COMPACT_SEPARATORS, allow_nan=False)
