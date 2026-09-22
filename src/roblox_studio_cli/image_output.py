"""Where a returned image lands on disk, and how it gets written there.

Three of the four inputs to that decision come from the other side of the
bridge: the tool's name, the payload's MIME type, and how many images the result
carries. So this module treats all three as hostile.

- A default filename is built from the tool name with everything outside
  `[A-Za-z0-9_.-]` replaced, then handed to `tempfile.NamedTemporaryFile` for
  the unique, 0600, inside-the-temp-directory part. Before that, a tool calling
  itself `../../../tmp/hostile_escape/x` wrote exactly there. The bytes go
  through the handle that call is still holding: reserving a name, closing it,
  and reopening by name is a window something else can put a symlink into.
- An explicit `--out` is never followed through a symlink, never written to
  anything that is not a regular file (a FIFO would block the process), and
  never silently overwritten: opening uses `O_NOFOLLOW`, plus `O_EXCL` unless
  `--force` says otherwise.
- The server does not get to decide how many files land. A result carrying more
  than `MAX_IMAGES_PER_RESULT` images is refused with nothing written, and
  `--force` licenses overwriting the one path the caller named, never the
  `-2`, `-3` siblings.
- A mismatch between the caller's extension and the MIME type the tool returned
  is reported, never fixed: renaming the caller's `--out` behind their back is
  worse than handing them a `.png` that holds JPEG bytes and saying so.

The payload itself is checked in `mcp_payloads.ToolImage.decoded_bytes`, which
verifies magic bytes before any of this runs.
"""

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from roblox_studio_cli.errors import StudioMcpError, StudioRequestError
from roblox_studio_cli.mcp_payloads import ToolImage
from roblox_studio_cli.terminal import sanitize_single_line

UNSAFE_FILENAME_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
MAX_TOOL_NAME_CHARS_IN_FILENAME = 48
IMAGE_FILE_PERMISSIONS = 0o600
# A capture tool returns one image. Eight is room for a build that returns a
# frame per data model, and still a bound on how many files one call can create.
MAX_IMAGES_PER_RESULT = 8

# Suffixes that mean the same format, so `--out shot.jpeg` for `image/jpeg` is
# not reported as a mismatch.
EQUIVALENT_FILE_EXTENSIONS: dict[str, frozenset[str]] = {
    ".jpg": frozenset({".jpg", ".jpeg"}),
}


@dataclass(frozen=True)
class SavedImage:
    """One image written to disk, and anything the caller should know about it.

    `warning` is empty in the ordinary case. It carries text when the file is
    correct but not what the caller's filename implies, which is a thing to say
    out loud rather than to fix by renaming their path.
    """

    path: Path
    warning: str = ""


def safe_filename_fragment(tool_name: str) -> str:
    """A tool name reduced to characters that cannot steer a path."""
    cleaned = UNSAFE_FILENAME_CHARACTER_PATTERN.sub("_", tool_name)
    return cleaned[:MAX_TOOL_NAME_CHARS_IN_FILENAME] or "tool"


def write_temporary_image_file(tool_name: str, file_extension: str, data: bytes) -> Path:
    """Write one capture to a fresh 0600 temp file and return its path.

    `NamedTemporaryFile` does the creating and this writes through the handle it
    is still holding, so there is no moment where the name exists and the bytes
    do not. Two captures a second apart cannot collide either.
    """
    prefix = f"studio_{safe_filename_fragment(tool_name)}_"
    try:
        with tempfile.NamedTemporaryFile(
            prefix=prefix, suffix=file_extension, delete=False
        ) as handle:
            handle.write(data)
            return Path(handle.name)
    except OSError as write_error:
        raise StudioRequestError(
            f"cannot write a temporary file for the returned image: {write_error}"
        ) from write_error


def write_image_bytes(path: Path, data: bytes, force: bool) -> None:
    """Write `data` to `path`, refusing to follow a symlink or clobber by accident.

    Raises:
        StudioRequestError: the path is a directory, a symlink or anything else
            that is not a regular file, already exists without `--force`, or
            cannot be written. All caller-fixable, so they exit 2 rather than
            looking like a Studio failure.
    """
    # Directory first: on macOS /tmp is itself a symlink, so the symlink check
    # would otherwise answer "--out /tmp" with a confusing message.
    if path.is_dir():
        raise StudioRequestError(f"{path} is a directory; --out takes a file path")
    if path.is_symlink():
        raise StudioRequestError(f"refusing to write through the symlink at {path}")
    existing_mode = lstat_mode(path)
    if existing_mode is not None and not stat.S_ISREG(existing_mode):
        raise StudioRequestError(
            f"{path} is not a regular file (a FIFO or device would block this process); "
            "--out takes a file path"
        )

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


def lstat_mode(path: Path) -> int | None:
    """The st_mode of `path` without following symlinks, or None when it does not exist."""
    try:
        return os.lstat(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as stat_error:
        raise StudioRequestError(f"cannot inspect {path}: {stat_error}") from stat_error


def extension_mismatch_warning(path: Path, image: ToolImage) -> str:
    """Say so when the caller's `--out` suffix disagrees with the returned MIME type.

    Studio answers a `.png` request with `image/jpeg` often enough to matter. The
    file is written under the name the caller chose either way; this is the line
    that stops them believing the extension.
    """
    expected = image.file_extension()
    accepted = EQUIVALENT_FILE_EXTENSIONS.get(expected, frozenset({expected}))
    suffix = path.suffix.lower()
    if suffix in accepted:
        return ""
    declared = sanitize_single_line(image.mime_type) or "(none declared)"
    named = suffix or "no extension"
    return (
        f"warning: the tool returned {declared}, so {path.name} holds {expected} data "
        f"despite {named}; wrote it under the name you asked for."
    )


def save_images(
    images: list[ToolImage], tool_name: str, out_path: Path | None, force: bool
) -> list[SavedImage]:
    """Write every image content item to disk and return what was written.

    With `--out`, the first image takes the given path and any extra frames get
    `-2`, `-3` suffixes. Those siblings are never overwritten, whatever `--force`
    says: the caller licensed one path, not a family of them. Without `--out`,
    each image gets its own temp file.

    Raises:
        StudioMcpError: the result carried more images than one call may write.
            Nothing is written in that case, including the first few.
    """
    if len(images) > MAX_IMAGES_PER_RESULT:
        raise StudioMcpError(
            f"the tool returned {len(images)} images (limit {MAX_IMAGES_PER_RESULT}); "
            "wrote none of them. Use --json to get the payload as-is."
        )

    written: list[SavedImage] = []
    for index, image in enumerate(images):
        data = image.decoded_bytes()
        if out_path is None:
            temporary = write_temporary_image_file(tool_name, image.file_extension(), data)
            written.append(SavedImage(temporary))
            continue

        is_the_requested_path = index == 0
        path = (
            out_path
            if is_the_requested_path
            else out_path.with_name(f"{out_path.stem}-{index + 1}{out_path.suffix}")
        )
        prepare_output_directory(path)
        write_image_bytes(path, data, force=force and is_the_requested_path)
        written.append(SavedImage(path, extension_mismatch_warning(path, image)))
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
