"""Tests for writer/paths.py — PM-1 Track.path strategy.

Why these cases exist:
- engine-lib is the DEFAULT because Engine's desktop library stores
  ../-escaping relative paths rooted at Engine Library/. Audio on this stick
  lives at <drive>/Contents/... while m.db is at
  <drive>/Engine Library/Database2/m.db, so stored paths become
  ../Contents/... . That geometry is the project's top open risk (PM-1):
  Engine's own exporter copies into Engine Library/Music/ instead, so this
  configuration is one Engine's tooling never produces.
- absolute must never be the default: a stick with absolute paths breaks the
  moment it mounts at a different path, and no oracle in this project would
  catch that regression.
- Forward slashes always: Engine and the sqlite file are cross-platform; a
  Windows backslash in Track.path is a latent missing-file bug on players.
- Paths outside drive_root must raise rather than emit a silently wrong
  relative string that would show as missing in Engine.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path, PurePosixPath

import pytest

from rb2engine.writer.paths import engine_track_path


def _geometry(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Real stick layout: audio under Contents/, DB under Engine Library/."""
    drive_root = tmp_path / "USB DISK"
    engine_library_dir = drive_root / "Engine Library"
    music_abs = drive_root / "Contents" / "A" / "b.mp3"
    music_abs.parent.mkdir(parents=True)
    music_abs.write_bytes(b"fake-audio")
    engine_library_dir.mkdir(parents=True)
    return drive_root, engine_library_dir, music_abs


# ---------------------------------------------------------------------------
# Default and three bases
# ---------------------------------------------------------------------------


def test_default_base_is_engine_lib_not_absolute() -> None:
    """absolute must be unreachable as the default — C-MAJOR-2 / PM-1.

    If the default ever becomes absolute, every converted stick breaks when
    remounted under a different path, and no field test would catch it until
    the user is on stage.
    """
    sig = inspect.signature(engine_track_path)
    default = sig.parameters["base"].default
    assert default == "engine-lib"
    assert default != "absolute"


def test_engine_lib_base_escapes_to_contents(tmp_path: Path) -> None:
    """Default geometry: path relative to Engine Library/ reaches Contents/.

    m.db lives at <drive>/Engine Library/Database2/m.db; Engine resolves
    Track.path from the Engine Library/ folder (not Database2/). Audio at
    <drive>/Contents/A/b.mp3 must therefore store as ../Contents/A/b.mp3.
    """
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    got = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
    )
    assert got == "../Contents/A/b.mp3"

    # Explicit base matches the default.
    explicit = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
        base="engine-lib",
    )
    assert explicit == got


def test_drive_root_base_is_contents_relative(tmp_path: Path) -> None:
    """drive-root stores paths relative to the volume root (no ../ escape).

    Fallback candidate if M1 shows Engine rejects engine-lib on removable media.
    """
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    got = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
        base="drive-root",
    )
    assert got == "Contents/A/b.mp3"


def test_absolute_base_is_diagnostic_only(tmp_path: Path) -> None:
    """absolute returns a full path string — only when explicitly requested."""
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    got = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
        base="absolute",
    )
    assert got == music_abs.resolve().as_posix()
    # Absolute means "rooted", which looks different per platform: "/..." on
    # POSIX, "C:/..." on Windows. Assert it is genuinely absolute rather than
    # assuming a leading slash.
    assert PurePosixPath(got).is_absolute() or re.match(r"^[A-Za-z]:/", got)
    assert "Contents/A/b.mp3" in got


# ---------------------------------------------------------------------------
# Cross-platform and safety
# ---------------------------------------------------------------------------


def test_forward_slashes_on_every_platform(tmp_path: Path) -> None:
    """Track.path must use / even when Path would use \\ on Windows.

    Engine and FAT32 players do not want host-native separators in sqlite.
    """
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    for base in ("engine-lib", "drive-root", "absolute"):
        got = engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base=base,
        )
        assert "\\" not in got
        assert "/" in got


def test_nested_contents_path_preserves_structure(tmp_path: Path) -> None:
    """Deep artist/album trees must not be collapsed or re-rooted incorrectly."""
    drive_root = tmp_path / "stick"
    engine_library_dir = drive_root / "Engine Library"
    music_abs = drive_root / "Contents" / "Artist" / "Album" / "Track Name.mp3"
    music_abs.parent.mkdir(parents=True)
    music_abs.write_bytes(b"x")
    engine_library_dir.mkdir(parents=True)

    assert (
        engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base="engine-lib",
        )
        == "../Contents/Artist/Album/Track Name.mp3"
    )
    assert (
        engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base="drive-root",
        )
        == "Contents/Artist/Album/Track Name.mp3"
    )


def test_path_outside_drive_root_raises(tmp_path: Path) -> None:
    """Outside the stick must not become a silently wrong relative path.

    Emitting ../../elsewhere/file.mp3 or an absolute host path without an
    explicit absolute base would produce missing-file ghosts in Engine.
    """
    drive_root = tmp_path / "stick"
    engine_library_dir = drive_root / "Engine Library"
    drive_root.mkdir()
    engine_library_dir.mkdir()

    outside = tmp_path / "elsewhere" / "track.mp3"
    outside.parent.mkdir()
    outside.write_bytes(b"x")

    for base in ("engine-lib", "drive-root", "absolute"):
        with pytest.raises((ValueError, OSError)):
            engine_track_path(
                outside,
                drive_root=drive_root,
                engine_library_dir=engine_library_dir,
                base=base,
            )


def test_unknown_base_raises(tmp_path: Path) -> None:
    """Typos in --path-base must fail loud, not fall through to a guess."""
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    with pytest.raises(ValueError):
        engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base="music-folder",
        )


@pytest.mark.parametrize("same_file", [True, False])
def test_export_spelling_survives_host_normalization(tmp_path, monkeypatch, same_file):
    """A FAT host's decomposed listing must not replace the portable source name."""
    import unicodedata

    host_name = unicodedata.normalize("NFD", "ドラマ")
    music = tmp_path / "Contents" / host_name / "English Title.mp3"
    source = "D:/Contents/ドラマ/English Title.mp3"
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: same_file)
    result = engine_track_path(
        music,
        drive_root=tmp_path,
        engine_library_dir=tmp_path / "Engine Library",
        source_path=source,
    )
    expected = "ドラマ" if same_file else host_name
    assert result == f"../Contents/{expected}/English Title.mp3"


@pytest.mark.parametrize(
    "source",
    [
        "D:/Contents/Other/track.mp3",
        "D:/Contents/../outside/track.mp3",
        "D:/Contents/artist/track.mp3",
        "D:/Elsewhere/Artist/track.mp3",
    ],
)
def test_source_hint_cannot_redirect_or_change_casing(tmp_path, source):
    music = tmp_path / "Contents" / "Artist" / "track.mp3"
    assert (
        engine_track_path(
            music,
            drive_root=tmp_path,
            engine_library_dir=tmp_path / "Engine Library",
            source_path=source,
        )
        == "../Contents/Artist/track.mp3"
    )


def test_missing_source_spelling_keeps_resolved_path(tmp_path):
    import unicodedata

    folder = unicodedata.normalize("NFD", "ドラマ")
    music = tmp_path / "Contents" / folder / "track.mp3"
    assert (
        engine_track_path(
            music,
            drive_root=tmp_path,
            engine_library_dir=tmp_path / "Engine Library",
            source_path="D:/Contents/ドラマ/track.mp3",
        )
        == f"../Contents/{folder}/track.mp3"
    )


def test_unicode_preservation_also_corrects_source_casing(tmp_path, monkeypatch):
    import unicodedata

    host_name = unicodedata.normalize("NFD", "ドラマ")
    music = tmp_path / "Contents" / "ARTIST" / host_name / "English.mp3"
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)
    assert (
        engine_track_path(
            music,
            drive_root=tmp_path,
            engine_library_dir=tmp_path / "Engine Library",
            source_path="D:/Contents/Artist/ドラマ/English.mp3",
        )
        == "../Contents/ARTIST/ドラマ/English.mp3"
    )


def test_sfm_alias_is_exported_only_when_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rb2engine.writer import paths

    monkeypatch.setattr(paths.sys, "platform", "darwin")
    root = tmp_path
    music = root / "Contents" / "Artist" / "with: guest*.mp3"
    target = root / "Contents" / "Artist" / "with\uf022 guest\uf021.mp3"
    monkeypatch.setattr(Path, "samefile", lambda self, other: self == target and other == music)
    assert paths._portable_special_spelling(music, root) == target


@pytest.mark.skipif(sys.platform == "win32", reason="Windows rejects literal colons")
def test_literal_colon_is_not_blindly_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rb2engine.writer import paths

    monkeypatch.setattr(paths.sys, "platform", "darwin")
    music = tmp_path / "literal: name.mp3"
    music.write_bytes(b"original")
    (tmp_path / "literal\uf022 name.mp3").write_bytes(b"other file")
    assert paths._portable_special_spelling(music, tmp_path) == music


@pytest.mark.skipif(sys.platform == "win32", reason="Windows rejects literal colons")
def test_sfm_symlink_alias_is_not_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rb2engine.writer import paths

    monkeypatch.setattr(paths.sys, "platform", "darwin")
    music = tmp_path / "literal: name.mp3"
    music.write_bytes(b"original")
    (tmp_path / "literal\uf022 name.mp3").symlink_to(music)
    assert paths._portable_special_spelling(music, tmp_path) == music
