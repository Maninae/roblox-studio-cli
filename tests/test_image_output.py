"""Tests for where a returned image lands, and what it refuses to do on the way.

The tool name and the MIME type both come from the other side of the bridge, so
a default filename must not be steerable and an explicit `--out` must not be
turned into a symlink write or a silent overwrite.
"""

import base64
import os
import tempfile
from pathlib import Path

import pytest

from roblox_studio_cli.errors import StudioRequestError
from roblox_studio_cli.image_output import create_default_image_path, save_images
from roblox_studio_cli.mcp_payloads import ToolImage

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload"
PNG_IMAGE = ToolImage("image/png", base64.b64encode(PNG_BYTES).decode())
HOSTILE_TOOL_NAME = "../../../tmp/hostile_escape/pwned"


def test_a_hostile_tool_name_cannot_steer_the_default_path():
    path = create_default_image_path(HOSTILE_TOOL_NAME, ".png")
    try:
        assert path.parent == Path(tempfile.gettempdir()), "escaped the temp directory"
        assert os.sep not in path.name, "the name still carries a path separator"
        assert path.name.startswith("studio_")
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        path.unlink()


def test_default_paths_are_unique_per_capture():
    first = create_default_image_path("screen_capture", ".png")
    second = create_default_image_path("screen_capture", ".png")
    try:
        assert first != second, "back-to-back captures would overwrite each other"
    finally:
        first.unlink()
        second.unlink()


def test_out_path_is_written_once_and_not_silently_overwritten(tmp_path):
    target = tmp_path / "shot.png"
    assert save_images([PNG_IMAGE], "screen_capture", target, force=False) == [target]
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
    assert [path.name for path in written] == ["shot.png", "shot-2.png"]


def test_a_missing_parent_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "deep" / "shot.png"
    assert save_images([PNG_IMAGE], "screen_capture", target, force=False) == [target]


def test_a_mislabelled_payload_never_reaches_the_disk(tmp_path):
    target = tmp_path / "shot.png"
    script = ToolImage("image/png", base64.b64encode(b"#!/bin/sh").decode())
    with pytest.raises(Exception, match="signature mismatch"):
        save_images([script], "screen_capture", target, force=True)
    assert not target.exists()
