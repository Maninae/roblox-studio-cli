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
  anything that is not a regular file (a FIFO would block the process), never
  written to a path that shares its inode with another name (truncating one
  hard link rewrites the file under all of them), and never silently
  overwritten: creating uses `O_NOFOLLOW | O_EXCL`, and a `--force`
  replacement fills a temp file beside the target and renames it into place.
  Rename replaces a name rather than following it, and it is what makes a
  failed write (a full disk) leave the previous capture untouched.
- The server does not get to decide how many files land. A result carrying more
  than `MAX_IMAGES_PER_RESULT` images is refused with nothing written, and
  `--force` licenses overwriting the one path the caller named, never the
  `-2`, `-3` siblings.
- Every frame is decoded and signature-checked before any of them is written,
  because a result whose later frame is a mislabelled payload used to leave the
  earlier ones on disk for a call the CLI then reported as failed. Every
  TARGET is checked before any of them is opened, for the same reason: with a
  `-2` sibling already on disk, `--out a.png --force` used to overwrite
  `a.png`, meet the sibling, and exit 2 having spent the caller's one `--force`
  on a call it then reported as failed.
- A mismatch between the caller's extension and the MIME type the tool returned
  is reported, never fixed: renaming the caller's `--out` behind their back is
  worse than handing them a `.png` that holds JPEG bytes and saying so.

The payload itself is checked in `mcp_payloads.ToolImage.decoded_bytes`, which
verifies magic bytes before any of this runs.
"""

import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from roblox_studio_cli.errors import StudioMcpError, StudioRequestError
from roblox_studio_cli.mcp_payloads import ToolImage
from roblox_studio_cli.terminal import sanitize_single_line

logger = logging.getLogger(__name__)

UNSAFE_FILENAME_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
MAX_TOOL_NAME_CHARS_IN_FILENAME = 48
IMAGE_FILE_PERMISSIONS = 0o600
# Create or fail: O_EXCL is the guard against a path that appeared between the
# check and the open, and O_NOFOLLOW against one that became a symlink.
CREATE_EXCLUSIVELY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
PARTIAL_FILE_SUFFIX = ".partial"
# Said in two places on purpose: once by the check that runs before any target
# is opened, once by the O_EXCL that catches a file created since that check.
ALREADY_EXISTS_MESSAGE = "{path} already exists; pass --force to overwrite it"
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


def check_target_is_writable(path: Path, force: bool) -> None:
    """Refuse a target the caller can fix, before anything at all is written.

    Every target a result will use goes through this before the first one is
    opened, so a blocked `-2` sibling cannot cost the caller the file they
    named. Nothing here is the actual race guard: the checks are for the
    message, and `O_EXCL` (or the rename) is what holds at the moment of truth.

    Raises:
        StudioRequestError: the path is a directory, a symlink or anything else
            that is not a regular file, is a second name for a file something
            else also holds, or already exists without `--force`.
    """
    # Directory first: on macOS /tmp is itself a symlink, so the symlink check
    # would otherwise answer "--out /tmp" with a confusing message.
    if path.is_dir():
        raise StudioRequestError(f"{path} is a directory; --out takes a file path")
    if path.is_symlink():
        raise StudioRequestError(f"refusing to write through the symlink at {path}")
    existing = lstat_status(path)
    if existing is None:
        return
    if not stat.S_ISREG(existing.st_mode):
        raise StudioRequestError(
            f"{path} is not a regular file (a FIFO or device would block this process); "
            "--out takes a file path"
        )
    # A hard link is the same file under another name, and replacing it through
    # one name would rewrite what every other name points at.
    if existing.st_nlink > 1:
        raise StudioRequestError(
            f"{path} has {existing.st_nlink} hard links, so writing it would replace the "
            "contents of the other names too; point --out somewhere of its own"
        )
    if not force:
        raise StudioRequestError(ALREADY_EXISTS_MESSAGE.format(path=path))


def write_image_bytes(path: Path, data: bytes, force: bool) -> None:
    """Put `data` at `path`, without clobbering anything the caller did not license.

    Two ways in, and neither can leave a half-written file behind:

    - Without `--force`, `O_CREAT | O_EXCL | O_NOFOLLOW` creates the file or
      says it is already there. The fd is a brand new regular file by
      construction, so nothing about the path needs re-checking.
    - With `--force`, the bytes fill a temp file beside the target and are
      renamed onto it. Rename is atomic and replaces the NAME rather than
      following it, so a symlink planted after the check is replaced rather
      than written through, and a write that fails partway (a full disk) leaves
      the previous capture exactly as it was. The cost is that `--force` now
      needs write permission on the DIRECTORY, which truncating in place did
      not; a writable file in a read-only directory is the case that buys.

    Raises:
        StudioRequestError: the path exists without `--force`, or the write
            failed. Caller-fixable either way, so they exit 2.
    """
    if not force:
        try:
            descriptor = os.open(path, CREATE_EXCLUSIVELY_FLAGS, IMAGE_FILE_PERMISSIONS)
        except FileExistsError as exists_error:
            raise StudioRequestError(ALREADY_EXISTS_MESSAGE.format(path=path)) from exists_error
        except OSError as open_error:
            raise StudioRequestError(f"cannot write to {path}: {open_error}") from open_error
        fill_descriptor(descriptor, path, data)
        return

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=PARTIAL_FILE_SUFFIX
        )
    except OSError as temp_error:
        raise StudioRequestError(f"cannot write to {path}: {temp_error}") from temp_error
    temporary = Path(temporary_name)
    fill_descriptor(descriptor, temporary, data)
    try:
        os.replace(temporary, path)
    except OSError as replace_error:
        remove_partial_file(temporary)
        raise StudioRequestError(f"cannot write to {path}: {replace_error}") from replace_error


def fill_descriptor(descriptor: int, path: Path, data: bytes) -> None:
    """Write `data` through an open descriptor, taking the file back if it fails.

    We created this file, so a half-written one is ours to remove: a disk that
    fills up mid-capture used to leave a truncated PNG under the caller's name.
    """
    try:
        with os.fdopen(descriptor, "wb") as image_file:
            image_file.write(data)
    except OSError as write_error:
        remove_partial_file(path)
        raise StudioRequestError(f"cannot write to {path}: {write_error}") from write_error


def remove_partial_file(path: Path) -> None:
    """Delete a file this module created and could not finish, never anyone else's."""
    try:
        os.unlink(path)
    except OSError as unlink_error:
        logger.debug("could not remove the partial file at %s: %s", path, unlink_error)


def lstat_status(path: Path) -> os.stat_result | None:
    """The lstat of `path`, not following symlinks, or None when it does not exist."""
    try:
        return os.lstat(path)
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

    Every frame is decoded and signature-checked, and every target is checked,
    before any of them is written. A result whose second frame is a mislabelled
    payload, or whose `-2` sibling is already on disk, leaves nothing behind and
    does not spend the caller's single `--force` on a call that then fails.

    Raises:
        StudioMcpError: the result carried more images than one call may write,
            or a frame did not decode as the format it claimed. Nothing is
            written in either case, including the frames that were fine.
        StudioRequestError: one of the targets is not writable as asked.
    """
    if len(images) > MAX_IMAGES_PER_RESULT:
        raise StudioMcpError(
            f"the tool returned {len(images)} images (limit {MAX_IMAGES_PER_RESULT}); "
            "wrote none of them. Rerun with --json and no --out for the payload as sent."
        )
    payloads = [(image, image.decoded_bytes()) for image in images]

    if out_path is None:
        return [
            SavedImage(write_temporary_image_file(tool_name, image.file_extension(), data))
            for image, data in payloads
        ]

    # Every sibling shares the named path's directory, so one mkdir covers them,
    # and the checks below need that directory to exist to mean anything.
    prepare_output_directory(out_path)
    targets = [sibling_path(out_path, index) for index in range(len(payloads))]
    for index, path in enumerate(targets):
        # `--force` licenses the one path the caller named, never the siblings.
        check_target_is_writable(path, force=force and index == 0)

    written: list[SavedImage] = []
    for index, (path, (image, data)) in enumerate(zip(targets, payloads)):
        write_image_bytes(path, data, force=force and index == 0)
        written.append(SavedImage(path, extension_mismatch_warning(path, image)))
    return written


def sibling_path(out_path: Path, index: int) -> Path:
    """The path frame `index` lands on: the caller's own, then `-2`, `-3`."""
    if index == 0:
        return out_path
    return out_path.with_name(f"{out_path.stem}-{index + 1}{out_path.suffix}")


def prepare_output_directory(path: Path) -> None:
    """Create the parent directory of an explicit `--out` path when it is missing."""
    parent = path.parent
    if not parent or parent.exists():
        return
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as mkdir_error:
        raise StudioRequestError(f"cannot create {parent}: {mkdir_error}") from mkdir_error
