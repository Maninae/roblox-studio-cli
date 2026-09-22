"""Where a returned image lands on disk, and how it gets written there.

Two of the three inputs to that decision come from the other side of the bridge:
the tool's name and the payload's MIME type. So this module treats both as
hostile.

- A default filename is built from the tool name with everything outside
  `[A-Za-z0-9_.-]` replaced, then handed to `tempfile.NamedTemporaryFile` for
  the unique, 0600, inside-the-temp-directory part. Before that, a tool calling
  itself `../../../tmp/hostile_escape/x` wrote exactly there.
- An explicit `--out` is never followed through a symlink and never silently
  overwritten: opening uses `O_NOFOLLOW`, plus `O_EXCL` unless `--force` says
  otherwise.

The payload itself is checked in `mcp_payloads.ToolImage.decoded_bytes`, which
verifies magic bytes before any of this runs.
"""

import os
import re
import tempfile
from pathlib import Path

from roblox_studio_cli.errors import StudioRequestError
from roblox_studio_cli.mcp_payloads import ToolImage

UNSAFE_FILENAME_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
MAX_TOOL_NAME_CHARS_IN_FILENAME = 48
IMAGE_FILE_PERMISSIONS = 0o600


def safe_filename_fragment(tool_name: str) -> str:
    """A tool name reduced to characters that cannot steer a path."""
    cleaned = UNSAFE_FILENAME_CHARACTER_PATTERN.sub("_", tool_name)
    return cleaned[:MAX_TOOL_NAME_CHARS_IN_FILENAME] or "tool"


def create_default_image_path(tool_name: str, file_extension: str) -> Path:
    """Reserve a fresh temp-directory path for one capture, and return it.

    `NamedTemporaryFile(delete=False)` does the creating, so two captures a
    second apart cannot collide and nothing else can win a race to the name.
    """
    prefix = f"studio_{safe_filename_fragment(tool_name)}_"
    handle = tempfile.NamedTemporaryFile(prefix=prefix, suffix=file_extension, delete=False)
    handle.close()
    return Path(handle.name)


def write_image_bytes(path: Path, data: bytes, force: bool) -> None:
    """Write `data` to `path`, refusing to follow a symlink or clobber by accident.

    Raises:
        StudioRequestError: the path is a symlink or a directory, already exists
            without `--force`, or cannot be written. All caller-fixable, so they
            exit 2 rather than looking like a Studio failure.
    """
    # Directory first: on macOS /tmp is itself a symlink, so the symlink check
    # would otherwise answer "--out /tmp" with a confusing message.
    if path.is_dir():
        raise StudioRequestError(f"{path} is a directory; --out takes a file path")
    if path.is_symlink():
        raise StudioRequestError(f"refusing to write through the symlink at {path}")

    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_TRUNC if force else os.O_EXCL
    try:
        file_descriptor = os.open(path, flags, IMAGE_FILE_PERMISSIONS)
    except FileExistsError as exists_error:
        raise StudioRequestError(
            f"{path} already exists; pass --force to overwrite it"
        ) from exists_error
    except OSError as open_error:
        raise StudioRequestError(f"cannot write to {path}: {open_error}") from open_error

    try:
        with os.fdopen(file_descriptor, "wb") as image_file:
            image_file.write(data)
    except OSError as write_error:
        raise StudioRequestError(f"cannot write to {path}: {write_error}") from write_error


def save_images(
    images: list[ToolImage], tool_name: str, out_path: Path | None, force: bool
) -> list[Path]:
    """Write every image content item to disk and return the paths written.

    With `--out`, the first image takes the given path and any extra frames get
    `-2`, `-3` suffixes, so a multi-image tool cannot silently overwrite the one
    the caller asked for. Without it, each image gets its own temp file.
    """
    written: list[Path] = []
    for index, image in enumerate(images):
        data = image.decoded_bytes()
        if out_path is None:
            path = create_default_image_path(tool_name, image.file_extension())
            path.write_bytes(data)
            written.append(path)
            continue

        path = out_path if index == 0 else out_path.with_name(
            f"{out_path.stem}-{index + 1}{out_path.suffix}"
        )
        prepare_output_directory(path)
        write_image_bytes(path, data, force=force)
        written.append(path)
    return written


def prepare_output_directory(path: Path) -> None:
    """Create the parent directory of an explicit `--out` path when it is missing."""
    parent = path.parent
    if not parent or parent.exists():
        return
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as mkdir_error:
        raise StudioRequestError(f"cannot create {parent}: {mkdir_error}") from mkdir_error
