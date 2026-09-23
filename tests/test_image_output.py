"""Tests for where a returned image lands, and what it refuses to do on the way.

The tool name, the MIME type and the number of images all come from the other
side of the bridge, so a default filename must not be steerable, an explicit
`--out` must not be turned into a symlink write or a silent overwrite, and one
result must not be able to scatter files across the caller's directory.
"""

import base64
import errno
import os
import tempfile
from pathlib import Path

import pytest

from roblox_studio_cli import image_output as image_output_module
from roblox_studio_cli.errors import StudioMcpError, StudioRequestError
from roblox_studio_cli.image_output import (
    MAX_IMAGES_PER_RESULT,
    save_images,
    write_temporary_image_file,
)
from roblox_studio_cli.mcp_payloads import ToolImage


class FullDiskFile:
    """A file object that creates nothing and fails the way a full disk fails.

    The seam is `os.fdopen`, which is where both write paths (a fresh file and
    the temp file a `--force` replacement fills) turn a descriptor into
    something writable, so one fake covers both.
    """

    def __init__(self, descriptor: int):
        """Hold the descriptor so closing it stays this object's job."""
        self.descriptor = descriptor

    def __enter__(self) -> "FullDiskFile":
        return self

    def __exit__(self, *details) -> bool:
        os.close(self.descriptor)
        return False

    def write(self, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


def full_disk_handle(descriptor: int, mode: str) -> FullDiskFile:
    """Stand in for `os.fdopen`, keeping its signature."""
    return FullDiskFile(descriptor)


PREVIOUS_CAPTURE = b"\x89PNG\r\n\x1a\n" + b"the capture that was already there"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload"
PNG_IMAGE = ToolImage("image/png", base64.b64encode(PNG_BYTES).decode())
JPEG_BYTES = b"\xff\xd8\xff" + b"payload"
JPEG_IMAGE = ToolImage("image/jpeg", base64.b64encode(JPEG_BYTES).decode())
# Declares PNG, carries a shell script: refused by the signature check.
MISLABELLED_IMAGE = ToolImage("image/png", base64.b64encode(b"#!/bin/sh").decode())
HOSTILE_TOOL_NAME = "../../../tmp/hostile_escape/pwned"
# 249 plus ".png" is 253, inside the 255-byte NAME_MAX every filesystem here has.
LONGEST_LEGAL_OUT_NAME_CHARS = 249
# Any mode that is not mkstemp's 0600, so an overwrite that dropped to 0600 shows.
SHARED_FILE_PERMISSIONS = 0o644


def test_a_hostile_tool_name_cannot_steer_the_default_path():
    path = write_temporary_image_file(HOSTILE_TOOL_NAME, ".png", PNG_BYTES)
    try:
        assert path.parent == Path(tempfile.gettempdir()), "escaped the temp directory"
        assert os.sep not in path.name, "the name still carries a path separator"
        assert path.name.startswith("studio_")
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_bytes() == PNG_BYTES
    finally:
        path.unlink()


def test_default_paths_are_unique_per_capture():
    first = write_temporary_image_file("screen_capture", ".png", PNG_BYTES)
    second = write_temporary_image_file("screen_capture", ".png", PNG_BYTES)
    try:
        assert first != second, "back-to-back captures would overwrite each other"
    finally:
        first.unlink()
        second.unlink()


def test_a_temp_capture_is_written_through_the_handle_not_reopened_by_name(monkeypatch):
    """Reserve, close, reopen by name leaves a window for a symlink at that name."""
    def reopened_by_name(*args, **kwargs):
        raise AssertionError("wrote by path instead of through the open handle")

    monkeypatch.setattr(Path, "write_bytes", reopened_by_name)
    written = save_images([PNG_IMAGE], "screen_capture", None, force=False)
    try:
        assert written[0].path.read_bytes() == PNG_BYTES
    finally:
        written[0].path.unlink()


def test_out_path_is_written_once_and_not_silently_overwritten(tmp_path):
    target = tmp_path / "shot.png"
    assert [item.path for item in save_images([PNG_IMAGE], "screen_capture", target, False)] == [
        target
    ]
    assert target.read_bytes() == PNG_BYTES

    with pytest.raises(StudioRequestError, match="--force"):
        save_images([PNG_IMAGE], "screen_capture", target, force=False)
    save_images([PNG_IMAGE], "screen_capture", target, force=True)


def test_a_symlink_at_out_is_never_followed(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    link = tmp_path / "shot.png"
    link.symlink_to(victim)

    with pytest.raises(StudioRequestError, match="symlink"):
        save_images([PNG_IMAGE], "screen_capture", link, force=True)
    assert victim.read_text() == "do not touch"


def test_a_directory_at_out_says_so(tmp_path):
    with pytest.raises(StudioRequestError, match="is a directory"):
        save_images([PNG_IMAGE], "screen_capture", tmp_path, force=True)


def test_extra_frames_get_suffixes_instead_of_overwriting(tmp_path):
    target = tmp_path / "shot.png"
    written = save_images([PNG_IMAGE, PNG_IMAGE], "screen_capture", target, force=False)
    assert [item.path.name for item in written] == ["shot.png", "shot-2.png"]


def test_force_licenses_the_path_the_caller_named_and_not_its_siblings(tmp_path):
    """The server decides how many frames come back; --force was about one file.

    All or nothing, like every other refusal here: the blocked sibling is found
    before the first byte is written, so the call that reports failure has not
    spent the caller's `--force` on the way to it.
    """
    target = tmp_path / "shot.png"
    target.write_text("stale")
    sibling = tmp_path / "shot-2.png"
    sibling.write_text("precious")

    with pytest.raises(StudioRequestError, match="already exists"):
        save_images([PNG_IMAGE, PNG_IMAGE], "screen_capture", target, force=True)
    assert target.read_text() == "stale", "the named path was overwritten for a call that failed"
    assert sibling.read_text() == "precious", "--force reached a file the caller never named"


@pytest.mark.parametrize(
    "label, blocked",
    [
        ("a symlink at the sibling", "symlink"),
        ("a directory at the sibling", "directory"),
    ],
)
def test_any_unwritable_sibling_stops_the_call_before_the_first_write(tmp_path, label, blocked):
    """Every target is checked before any of them is opened, not one at a time."""
    target = tmp_path / "shot.png"
    sibling = tmp_path / "shot-2.png"
    if blocked == "symlink":
        sibling.symlink_to(tmp_path / "victim.txt")
    else:
        sibling.mkdir()

    with pytest.raises(StudioRequestError):
        save_images([PNG_IMAGE, PNG_IMAGE], "screen_capture", target, force=True)
    assert not target.exists(), f"{label} was found only after the first frame was written"


def test_a_write_that_fails_leaves_no_half_written_file(tmp_path, monkeypatch):
    """A full disk creates the file, then fails: the caller must not keep the stub."""
    monkeypatch.setattr(image_output_module.os, "fdopen", full_disk_handle)
    target = tmp_path / "shot.png"
    with pytest.raises(StudioRequestError, match="cannot write"):
        save_images([PNG_IMAGE], "screen_capture", target, force=False)
    assert list(tmp_path.iterdir()) == [], "a zero-byte capture was left behind"


def test_a_failed_overwrite_leaves_the_previous_capture_in_place(tmp_path, monkeypatch):
    """`--force` licenses a replacement, not the destruction of what was there."""
    monkeypatch.setattr(image_output_module.os, "fdopen", full_disk_handle)
    target = tmp_path / "shot.png"
    target.write_bytes(PREVIOUS_CAPTURE)
    with pytest.raises(StudioRequestError, match="cannot write"):
        save_images([PNG_IMAGE], "screen_capture", target, force=True)
    assert target.read_bytes() == PREVIOUS_CAPTURE, "the old capture was truncated first"
    assert list(tmp_path.iterdir()) == [target], "a partial file was left in the directory"


def test_too_many_images_is_refused_with_nothing_written(tmp_path):
    target = tmp_path / "shot.png"
    crowd = [PNG_IMAGE] * (MAX_IMAGES_PER_RESULT + 1)
    with pytest.raises(StudioMcpError, match="limit"):
        save_images(crowd, "screen_capture", target, force=True)
    assert not target.exists(), "files were written before the count was checked"
    assert list(tmp_path.iterdir()) == []


def test_the_image_limit_allows_an_ordinary_multi_frame_result(tmp_path):
    written = save_images([PNG_IMAGE] * MAX_IMAGES_PER_RESULT, "x", tmp_path / "s.png", False)
    assert len(written) == MAX_IMAGES_PER_RESULT


def test_a_mime_type_that_contradicts_the_extension_warns_and_keeps_the_name(tmp_path):
    """Studio answers a .png request with image/jpeg; renaming the caller's path is worse."""
    target = tmp_path / "shot.png"
    written = save_images([JPEG_IMAGE], "screen_capture", target, force=False)
    assert written[0].path == target, "the path the caller asked for was changed"
    assert target.read_bytes() == JPEG_BYTES
    assert "image/jpeg" in written[0].warning
    assert ".jpg" in written[0].warning


def test_a_matching_extension_says_nothing_and_jpeg_counts_as_jpg(tmp_path):
    assert save_images([PNG_IMAGE], "x", tmp_path / "a.png", False)[0].warning == ""
    assert save_images([JPEG_IMAGE], "x", tmp_path / "b.jpeg", False)[0].warning == ""
    assert save_images([JPEG_IMAGE], "x", tmp_path / "c.JPG", False)[0].warning == ""


def test_a_fifo_at_out_is_refused_rather_than_blocking_the_process(tmp_path):
    target = tmp_path / "shot.png"
    os.mkfifo(target)
    with pytest.raises(StudioRequestError, match="not a regular file"):
        save_images([PNG_IMAGE], "screen_capture", target, force=True)


def test_a_missing_parent_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "deep" / "shot.png"
    assert save_images([PNG_IMAGE], "screen_capture", target, force=False)[0].path == target


def test_a_mislabelled_payload_never_reaches_the_disk(tmp_path):
    target = tmp_path / "shot.png"
    script = ToolImage("image/png", base64.b64encode(b"#!/bin/sh").decode())
    with pytest.raises(Exception, match="signature mismatch"):
        save_images([script], "screen_capture", target, force=True)
    assert not target.exists()


def test_a_bad_frame_leaves_the_frames_before_it_unwritten(tmp_path):
    """The server chooses how many frames come back and what is inside each one.

    Writing them one at a time handed the caller the first two of three and then
    reported failure, which is the worst of both: files on disk for a call the
    CLI says did not happen.
    """
    target = tmp_path / "shot.png"
    with pytest.raises(StudioMcpError, match="signature mismatch"):
        save_images([PNG_IMAGE, MISLABELLED_IMAGE], "screen_capture", target, force=False)
    assert list(tmp_path.iterdir()) == [], "an earlier frame was written before the later one failed"


def test_a_bad_frame_does_not_spend_the_force_the_caller_gave_it(tmp_path):
    """Same rule as a failed call: the one `--force` must not buy a broken result."""
    target = tmp_path / "shot.png"
    target.write_text("precious")
    with pytest.raises(StudioMcpError, match="signature mismatch"):
        save_images([PNG_IMAGE, MISLABELLED_IMAGE], "screen_capture", target, force=True)
    assert target.read_text() == "precious", "the named path was overwritten for a failed result"


def test_a_long_out_name_can_still_be_replaced(tmp_path):
    """The temp file `--force` fills is named by us, not by the caller's `--out`.

    Prefixed with the target's own name, a 249-character `--out` (legal, and it
    writes fine without `--force`) plus a dot plus mkstemp's eight random
    characters plus `.partial` came to 267, over NAME_MAX, so the replacement
    failed with ENAMETOOLONG on a path the first write had accepted.
    """
    target = tmp_path / ("s" * LONGEST_LEGAL_OUT_NAME_CHARS + ".png")
    save_images([PNG_IMAGE], "screen_capture", target, force=False)
    save_images([JPEG_IMAGE], "screen_capture", target, force=True)
    assert target.read_bytes() == JPEG_BYTES, "the replacement never landed"


def test_a_force_replacement_keeps_the_permissions_it_replaced(tmp_path):
    """`--force` swaps in a new inode, and mkstemp makes it 0600.

    A capture the caller had deliberately made readable came back private the
    first time Studio was asked for a fresh one under the same name.
    """
    target = tmp_path / "shot.png"
    target.write_bytes(PREVIOUS_CAPTURE)
    target.chmod(SHARED_FILE_PERMISSIONS)

    save_images([PNG_IMAGE], "screen_capture", target, force=True)
    assert target.stat().st_mode & 0o777 == SHARED_FILE_PERMISSIONS
    assert target.read_bytes() == PNG_BYTES


def test_a_hard_linked_out_path_is_refused_rather_than_truncated(tmp_path):
    """Truncating one name of a shared inode rewrites the file under every other name."""
    target = tmp_path / "shot.png"
    target.write_text("precious")
    twin = tmp_path / "same-file.png"
    os.link(target, twin)

    with pytest.raises(StudioRequestError, match="hard link"):
        save_images([PNG_IMAGE], "screen_capture", target, force=True)
    assert twin.read_text() == "precious", "--force reached a file the caller never named"
