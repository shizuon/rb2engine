"""SourceTrack → EngineTrack composition.

WHY: map_track is the only place key ordinals, cue merge, beatgrid compression,
and path strategy meet. A field-by-field regression here silently ships a
library that looks fine in a dump but plays wrong on hardware.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rb2engine.ir import (
    RGB,
    CueKind,
    SourceArtwork,
    SourceBeat,
    SourceBeatgrid,
    SourceCue,
    SourceTrack,
)
from rb2engine.ir_engine import EMPTY_SAMPLE_OFFSET, EngineTrack
from rb2engine.mapper.keys import key_name_to_ordinal
from rb2engine.mapper.track import map_track


def _spb(bpm: float, sample_rate: int = 44100) -> float:
    return sample_rate * 60.0 / bpm


def _grid(bpm: float = 128.0, n: int = 16, sample_rate: int = 44100) -> SourceBeatgrid:
    spb = _spb(bpm, sample_rate)
    beats = [
        SourceBeat(
            beat_in_bar=(i % 4) + 1,
            sample_offset=round(i * spb),
            bpm=bpm,
        )
        for i in range(n)
    ]
    return SourceBeatgrid(beats=beats, is_adjusted=False)


def _track(**overrides: object) -> SourceTrack:
    sample_rate = 44100
    bpm = 128.0
    duration_s = 180
    base = dict(  # noqa: C408 - kwargs form keeps this fixture readable
        rb_id=42,
        title="Test Title",
        artist="Test Artist",
        album="Test Album",
        genre="Techno",
        label="Some Label",
        comment="a comment",
        composer="Composer X",
        remixer="Remixer Y",
        year=2020,
        track_number=3,
        disc_number=1,
        bpm=bpm,
        key_name="Am",
        rating=100,
        play_count=7,
        bitrate=320,
        file_size=12_345_678,
        file_type="mp3",
        sample_rate=sample_rate,
        duration_s=duration_s,
        total_samples=duration_s * sample_rate,
        raw_path="/Contents/Artist/Album/track.mp3",
        resolved_path=None,  # filled by caller with real Path under drive
        beatgrid=_grid(bpm, 16, sample_rate),
        cues=[],
        artwork=None,
        analyze_path=None,
    )
    base.update(overrides)
    return SourceTrack(**base)  # type: ignore[arg-type]


@pytest.fixture()
def drive_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """drive_root / Contents/.../track.mp3 and Engine Library/ sibling."""
    drive = tmp_path / "stick"
    music = drive / "Contents" / "Artist" / "Album" / "track.mp3"
    music.parent.mkdir(parents=True)
    music.write_bytes(b"fake-audio")
    eng_lib = drive / "Engine Library"
    eng_lib.mkdir()
    return drive, eng_lib, music


class TestFieldByFieldMapping:
    """Scalar fields must cross the boundary without silent defaults.

    WHY: Wrong title/artist is obvious; wrong bpm/key ruins harmonic mixing
    without a clear error. Honest mapping is the whole point of this layer.
    """

    def test_core_scalars(self, drive_layout: tuple[Path, Path, Path]) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(resolved_path=music)
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)

        assert isinstance(out, EngineTrack)
        assert out.title == "Test Title"
        assert out.artist == "Test Artist"
        assert out.album == "Test Album"
        assert out.genre == "Techno"
        assert out.label == "Some Label"
        assert out.comment == "a comment"
        assert out.composer == "Composer X"
        assert out.year == 2020
        assert out.track_number == 3
        assert out.disc_number == 1
        assert out.rating == 100
        # Engine stores integer bpm + REAL bpmAnalyzed
        assert out.bpm == 128
        assert out.bpm_analyzed == pytest.approx(128.0)
        assert out.sample_rate == pytest.approx(44100.0)
        assert out.samples == 180 * 44100

    def test_path_via_engine_track_path(
        self, drive_layout: tuple[Path, Path, Path]
    ) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(resolved_path=music)
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        # Default base=engine-lib → ../Contents/...
        assert out.path == "../Contents/Artist/Album/track.mp3"
        assert "\\" not in out.path

    def test_artwork_hash_wired(
        self, drive_layout: tuple[Path, Path, Path]
    ) -> None:
        drive, eng_lib, music = drive_layout
        art = SourceArtwork(
            content_key="abc123def",
            path=None,
            source="embedded",
        )
        src = _track(resolved_path=music, artwork=art)
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        assert out.album_art_hash == "abc123def"


class TestKeyOrdinalWired:
    """key_name_to_ordinal must feed EngineTrack.key.

    WHY: A wrong ordinal is silent harmonic-mix poison; wire must be real.
    """

    def test_am_maps_to_ordinal(self, drive_layout: tuple[Path, Path, Path]) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(resolved_path=music, key_name="Am")
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        expected = key_name_to_ordinal("Am")
        assert expected == 1  # A minor
        assert out.key == expected

    def test_missing_key_is_none(self, drive_layout: tuple[Path, Path, Path]) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(resolved_path=music, key_name=None)
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        assert out.key is None


class TestCueMergeWired:
    """Loop with end point frees its pad (U3).

    WHY: A hot-cue loop must occupy a loop slot and leave the pad free so the
    DJ's pad muscle memory is not occupied by something that is not a point cue.
    """

    def test_loop_frees_pad(self, drive_layout: tuple[Path, Path, Path]) -> None:
        drive, eng_lib, music = drive_layout
        cues = [
            SourceCue(
                kind=CueKind.HOT,
                hot_slot=1,
                start_sample=1000,
                end_sample=5000,  # loop → loop slot, not pad
                color=RGB(255, 0, 0),
                name="LoopA",
            ),
            SourceCue(
                kind=CueKind.HOT,
                hot_slot=2,
                start_sample=2000,
                end_sample=None,
                color=RGB(0, 255, 0),
                name="PointB",
            ),
        ]
        src = _track(resolved_path=music, cues=cues)
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)

        assert len(out.quick_cues) == 8
        assert len(out.loops) == 8

        # Pad 1 empty (loop freed it); pad 2 has the point cue
        assert out.quick_cues[0].sample_offset == EMPTY_SAMPLE_OFFSET
        assert out.quick_cues[1].sample_offset == pytest.approx(2000.0)
        assert out.quick_cues[1].label == "PointB"

        # Loop occupies a loop slot
        populated_loops = [
            lp
            for lp in out.loops
            if lp.start_sample_offset != EMPTY_SAMPLE_OFFSET
        ]
        assert len(populated_loops) == 1
        assert populated_loops[0].start_sample_offset == pytest.approx(1000.0)
        assert populated_loops[0].end_sample_offset == pytest.approx(5000.0)
        assert populated_loops[0].label == "LoopA"
        assert populated_loops[0].is_start_set == 1
        assert populated_loops[0].is_end_set == 1


class TestBeatgridWired:
    def test_constant_grid_compressed(
        self, drive_layout: tuple[Path, Path, Path]
    ) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(resolved_path=music, beatgrid=_grid(128.0, 64))
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        assert out.beat_grid.is_beatgrid_set is True
        assert len(out.beat_grid.default_markers) == 2
        assert out.beat_grid.default_markers[0].beat_number == -4


class TestMissingFieldsDegrade:
    """None / empty inputs must not raise.

    WHY: Real sticks have incomplete rows; crashing mid-library is worse than
    writing a partial track the report can itemize.
    """

    def test_no_beatgrid(self, drive_layout: tuple[Path, Path, Path]) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(resolved_path=music, beatgrid=None)
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        assert out.beat_grid.is_beatgrid_set is False
        assert out.beat_grid.default_markers == []

    def test_no_resolved_path_uses_fallback(
        self, drive_layout: tuple[Path, Path, Path]
    ) -> None:
        drive, eng_lib, _music = drive_layout
        src = _track(resolved_path=None, raw_path="Contents/fallback.mp3")
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        # Must not raise; path is some non-crashing string
        assert isinstance(out.path, str)

    def test_none_optional_numbers(
        self, drive_layout: tuple[Path, Path, Path]
    ) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(
            resolved_path=music,
            track_number=None,
            disc_number=None,
            total_samples=None,
            artwork=None,
            key_name=None,
        )
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        assert out.track_number is None
        assert out.disc_number is None
        assert out.key is None
        assert out.album_art_hash is None
        # samples fall back from duration_s * sample_rate
        assert out.samples == 180 * 44100

    def test_empty_cues_pad_to_eight(
        self, drive_layout: tuple[Path, Path, Path]
    ) -> None:
        drive, eng_lib, music = drive_layout
        src = _track(resolved_path=music, cues=[])
        out = map_track(src, drive_root=drive, engine_library_dir=eng_lib)
        assert len(out.quick_cues) == 8
        assert len(out.loops) == 8
        assert all(q.sample_offset == EMPTY_SAMPLE_OFFSET for q in out.quick_cues)


def test_mapping_preserves_source_unicode_for_portable_database(tmp_path, monkeypatch):
    import unicodedata

    album = unicodedata.normalize("NFD", "ドラマ")
    music = tmp_path / "Contents" / album / "English.mp3"
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)
    src = _track(
        raw_path="D:/Contents/ドラマ/English.mp3",
        resolved_path=music,
    )
    out = map_track(src, drive_root=tmp_path, engine_library_dir=tmp_path / "Engine Library")
    assert out.path == "../Contents/ドラマ/English.mp3"
    assert src.resolved_path == music
