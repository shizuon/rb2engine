"""verify_library: mechanical fidelity check of written m.db vs source IR.

WHY: Today the only proof a conversion is faithful is a human opening Engine DJ
and spot-checking a few tracks. A verifier that always returns ok is worthless —
these tests deliberately mutate the written database and assert each corruption
is reported. Expected discrepancies are named from the mutation applied, never
from running verify and pasting its output.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from rb2engine.errors import FatalError, UnsupportedFormatError
from rb2engine.ir import (
    RGB,
    CueKind,
    SourceArtwork,
    SourceBeat,
    SourceBeatgrid,
    SourceCue,
    SourceLibrary,
    SourcePlaylist,
    SourceTrack,
)
from rb2engine.report import ConversionReport
from rb2engine.writer.blobs import (
    Color,
    QuickCue,
    decode_beat_data,
    decode_loops,
    decode_quick_cues,
    decode_track_data,
    encode_beat_data,
    encode_loops,
    encode_quick_cues,
    encode_track_data,
)
from rb2engine.writer.build import build_library

# ---------------------------------------------------------------------------
# Fixtures — hand-authored small library (not derived from verify output)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100
BPM = 128.0
# Dense grid at constant tempo — mapper compresses to sparse markers.
_BEATS = [
    SourceBeat(beat_in_bar=(i % 4) + 1, sample_offset=i * 20672, bpm=BPM)
    for i in range(16)
]


def _source_track(
    rb_id: int,
    *,
    drive: Path,
    title: str,
    filename: str,
    bpm: float = BPM,
    key_name: str = "Am",
    cues: list[SourceCue] | None = None,
    artwork: SourceArtwork | None = None,
) -> SourceTrack:
    audio = drive / "Contents" / filename
    audio.parent.mkdir(parents=True, exist_ok=True)
    if not audio.is_file():
        audio.write_bytes(b"fake-audio-" + filename.encode())
    return SourceTrack(
        rb_id=rb_id,
        title=title,
        artist="Artist",
        album="Album",
        genre="House",
        label="Label",
        comment="",
        composer="",
        remixer="",
        year=2020,
        track_number=1,
        disc_number=None,
        bpm=bpm,
        key_name=key_name,
        rating=0,
        play_count=0,
        bitrate=320,
        file_size=1000,
        file_type="mp3",
        sample_rate=SAMPLE_RATE,
        duration_s=10,
        total_samples=SAMPLE_RATE * 10,
        raw_path=f"/Contents/{filename}",
        resolved_path=audio,
        beatgrid=SourceBeatgrid(beats=list(_BEATS), is_adjusted=False),
        cues=cues
        if cues is not None
        else [
            SourceCue(
                kind=CueKind.HOT,
                hot_slot=1,
                start_sample=44100,
                end_sample=None,
                color=RGB(255, 0, 0),
                name="Drop",
            ),
            SourceCue(
                kind=CueKind.HOT,
                hot_slot=2,
                start_sample=88200,
                end_sample=132300,
                color=RGB(0, 255, 0),
                name="Loop",
            ),
        ],
        artwork=artwork,
    )


def _build_fixture(
    tmp_path: Path, *, with_artwork: bool = False
) -> tuple[Path, SourceLibrary, Path]:
    """Build a 3-track library with one playlist into tmp_path; return drive, lib, m.db."""
    drive = tmp_path / "stick"
    drive.mkdir()
    (drive / "Contents").mkdir()
    (drive / "PIONEER").mkdir()

    artwork: SourceArtwork | None = None
    if with_artwork:
        # Minimal valid 1×1 PNG so insert_artwork can load bytes.
        art_file = drive / "cover.png"
        art_file.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
            )
        )
        from rb2engine.ir_engine import artwork_content_hash

        artwork = SourceArtwork(
            content_key=artwork_content_hash(art_file.read_bytes()),
            path=art_file,
            source="pdb",
        )

    tracks = {
        10: _source_track(
            10, drive=drive, title="Alpha", filename="a.mp3", bpm=120.0, artwork=artwork
        ),
        20: _source_track(
            20, drive=drive, title="Beta", filename="b.mp3", bpm=128.0, artwork=artwork
        ),
        30: _source_track(
            30, drive=drive, title="Gamma", filename="c.mp3", bpm=130.0, artwork=artwork
        ),
    }
    playlists = [
        SourcePlaylist(
            rb_id=1,
            parent_rb_id=0,
            name="Main Set",
            sort_order=0,
            is_folder=False,
            track_rb_ids=[10, 20, 30],
        ),
    ]
    lib = SourceLibrary(
        drive_root=drive, tracks=tracks, playlists=playlists, warnings=[]
    )
    m_db = build_library(
        lib, drive_root=drive, report=ConversionReport(), with_artwork=with_artwork
    )
    return drive, lib, m_db


def _patch_read_library(
    monkeypatch: pytest.MonkeyPatch, lib: SourceLibrary
) -> None:
    """verify_library always re-parses the source stick; unit tests inject IR."""
    import rb2engine.verify as verify_mod

    def _read(drive_root: Path, *, with_anlz: bool = True, with_artwork: bool = True):
        del drive_root, with_anlz, with_artwork
        return lib

    monkeypatch.setattr(verify_mod, "read_library", _read)


def _fields(discrepancies: list) -> set[str]:
    return {d.field for d in discrepancies}


def _track_ids(discrepancies: list) -> set[int | None]:
    return {d.track_id for d in discrepancies}


def _disc(result, field: str):
    """Return the first discrepancy with this field name (must exist)."""
    for d in result.discrepancies:
        if d.field == field:
            return d
    raise AssertionError(
        f"expected field {field!r} in {_fields(result.discrepancies)}"
    )


def _track_row_id(conn: sqlite3.Connection, title: str) -> int:
    row = conn.execute("SELECT id FROM Track WHERE title = ?", (title,)).fetchone()
    assert row is not None, title
    return int(row[0])


# ---------------------------------------------------------------------------
# Happy path — a faithful build must verify clean
# ---------------------------------------------------------------------------


def test_verify_clean_build_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end build must produce zero discrepancies.

    WHY: If a correct conversion fails verify, operators will ignore the tool
    and go back to manual spot-checks — the entire feature collapses.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    result = verify_library(drive, with_artwork=False)

    assert result.ok is True
    assert result.discrepancies == []
    assert result.checked == 3
    assert result.matched == 3
    assert result.mismatched == 0
    text = result.render_text()
    assert "ok" in text.lower() or "0 discrepancy" in text.lower() or "matched" in text.lower()


def test_sample_limits_tracks_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sample=N verifies only the first N tracks (by rb_id) — USB-scale safety.

    WHY: A full 3,600-track verify over USB is slow; operators need a cheap
    smoke path that still exercises real decode+diff logic.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    result = verify_library(drive, with_artwork=False, sample=2)

    assert result.ok is True
    assert result.checked == 2
    assert result.matched == 2


def test_sample_zero_checks_no_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sample=0 is a valid empty smoke: zero tracks checked, playlists still run."""
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    result = verify_library(drive, with_artwork=False, sample=0)

    assert result.checked == 0
    assert result.matched == 0
    assert result.mismatched == 0


def test_sample_negative_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative sample is programmer error, not a silent full-library verify."""
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    with pytest.raises(ValueError, match="sample"):
        verify_library(drive, with_artwork=False, sample=-1)


# ---------------------------------------------------------------------------
# Failure modes — prove verify can fail
# ---------------------------------------------------------------------------


def test_verify_catches_bpm_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberately wrong bpm in m.db must surface as a bpm discrepancy.

    WHY: BPM is the most-used harmonic-mixing field; silent drift would ship
    unmixable libraries that still 'open' fine in Engine.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    # Mutate Alpha (title known) — expected bpm remains 120 from source.
    conn.execute(
        "UPDATE Track SET bpm = 999, bpmAnalyzed = 999.0 WHERE title = ?",
        ("Alpha",),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    assert result.mismatched >= 1
    assert "bpm" in _fields(result.discrepancies) or "bpm_analyzed" in _fields(
        result.discrepancies
    )
    assert 10 in _track_ids(result.discrepancies)


def test_verify_catches_corrupt_quick_cues_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupting PerformanceData.quickCues must fail the cue comparison.

    WHY: Cue pad positions are the muscle-memory contract. A blob that
    decompresses to garbage (or fails to decode) is exactly the silent
    failure mode this tool exists to catch before the gig.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    row = conn.execute(
        "SELECT id FROM Track WHERE title = ?", ("Beta",)
    ).fetchone()
    assert row is not None
    track_id = row[0]
    # Replace with non-zlib garbage that decode_quick_cues cannot parse.
    conn.execute(
        "UPDATE PerformanceData SET quickCues = ? WHERE trackId = ?",
        (b"\x00\x01\x02\x03not-a-valid-quickcues-blob", track_id),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    fields = _fields(result.discrepancies)
    assert any(
        f.startswith("quick_cue") or f in {"quick_cues", "quickCues"} for f in fields
    ), fields
    assert 20 in _track_ids(result.discrepancies)
    d = _disc(result, "quick_cues")
    assert d.expected == "decodable"
    assert "decode_error" in str(d.actual)


def test_verify_catches_deleted_track_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source track missing from m.db is a discrepancy, not a crash.

    WHY: A silent skip during conversion looks exactly like this — the track
    is in the source library, gone from Engine, and no exception was raised.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    row = conn.execute(
        "SELECT id FROM Track WHERE title = ?", ("Gamma",)
    ).fetchone()
    assert row is not None
    tid = row[0]
    conn.execute("DELETE FROM PlaylistEntity WHERE trackId = ?", (tid,))
    conn.execute("DELETE FROM PerformanceData WHERE trackId = ?", (tid,))
    conn.execute("DELETE FROM Track WHERE id = ?", (tid,))
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    assert 30 in _track_ids(result.discrepancies)
    assert any(
        d.field in {"missing", "presence", "track"} for d in result.discrepancies
    ), _fields(result.discrepancies)


def test_verify_catches_playlist_chain_reorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reordering nextEntityId must fail playlist track-order verification.

    WHY: Engine reconstructs order solely from the linked list. A wrong chain
    plays the set in the wrong order with no GUI error — the operator only
    notices mid-set.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    # Original order of engine track ids via entities for listId=1: head→…→tail.
    # Swap so the chain is no longer [Alpha, Beta, Gamma] = rb [10,20,30].
    entities = conn.execute(
        "SELECT id, trackId, nextEntityId FROM PlaylistEntity "
        "WHERE listId = (SELECT id FROM Playlist WHERE title = ?) "
        "ORDER BY id",
        ("Main Set",),
    ).fetchall()
    assert len(entities) == 3
    # entities currently: e1→e2→e3→0 with tracks t1,t2,t3.
    # Reorder to t1→t3→t2 (swap Beta and Gamma in the chain).
    e1, e2, e3 = entities[0][0], entities[1][0], entities[2][0]
    # Make head e1 → e3 → e2 → 0
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = ? WHERE id = ?", (e3, e1)
    )
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = ? WHERE id = ?", (e2, e3)
    )
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = 0 WHERE id = ?", (e2,)
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    d = _disc(result, "playlist[Main Set].track_order")
    assert d.expected != d.actual
    assert d.track_id is None


def test_render_text_lists_discrepancies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_text must surface field names so CLI operators can act on them."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute("UPDATE Track SET title = 'WRONG' WHERE title = 'Alpha'")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    text = result.render_text()

    assert result.ok is False
    assert "title" in text.lower() or "Alpha" in text or "WRONG" in text
    assert "10" in text or "Alpha" in text


def test_render_text_with_many_discrepancies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many independent mutations must all appear in render_text.

    WHY: Operators fix from the text report; truncating or eliding fields would
    hide half the damage after a bad convert.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "UPDATE Track SET title = 'T1', artist = 'A1', key = 23 WHERE title = 'Alpha'"
    )
    conn.execute(
        "UPDATE Track SET title = 'T2', artist = 'A2' WHERE title = 'Beta'"
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    text = result.render_text()

    assert result.ok is False
    assert len(result.discrepancies) >= 4
    assert "FAILED" in text
    assert "Discrepancies:" in text
    # At least one expected/actual pair for a known field name.
    assert "title" in text
    assert "expected=" in text and "actual=" in text


# ---------------------------------------------------------------------------
# Beatgrid mutations
# ---------------------------------------------------------------------------


def test_verify_catches_beatgrid_offset_shift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shifting a beat marker sample_offset must report beatgrid.default_markers.

    WHY: A grid shifted by even a few hundred samples makes every downbeat
    land off the kick — the set feels 'wrong' with no Engine error dialog.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT beatData FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    bd = decode_beat_data(blob)
    markers = list(bd.default_beat_grid.markers)
    assert markers, "fixture must write at least one beat marker"
    # Shift the first marker by a known amount (not derived from verify output).
    shifted = replace(markers[0], sample_offset=markers[0].sample_offset + 4410.0)
    new_grid = replace(bd.default_beat_grid, markers=[shifted, *markers[1:]])
    new_bd = replace(bd, default_beat_grid=new_grid)
    conn.execute(
        "UPDATE PerformanceData SET beatData = ? WHERE trackId = ?",
        (encode_beat_data(new_bd), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    d = _disc(result, "beatgrid.default_markers")
    assert d.track_id == 10
    assert d.expected != d.actual
    # One discrepancy covers the offset and beat numbering together.
    assert len([item for item in result.discrepancies
                if item.field.startswith("beatgrid.")]) == 1
    assert int(shifted.sample_offset) in [int(marker[0]) for marker in d.actual]


def test_verify_catches_beatgrid_marker_count_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping a beat marker changes the offset list and must be reported."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Beta")
    blob = conn.execute(
        "SELECT beatData FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    bd = decode_beat_data(blob)
    markers = list(bd.default_beat_grid.markers)
    assert len(markers) >= 2
    # Drop the last marker — expected count stays at the source-mapped length.
    new_grid = replace(bd.default_beat_grid, markers=markers[:-1])
    conn.execute(
        "UPDATE PerformanceData SET beatData = ? WHERE trackId = ?",
        (encode_beat_data(replace(bd, default_beat_grid=new_grid)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    d = _disc(result, "beatgrid.default_markers")
    assert d.track_id == 20
    assert len(d.expected) == len(markers)
    assert len(d.actual) == len(markers) - 1


def test_verify_catches_corrupt_beat_data_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undecodable beatData is a discrepancy, not a crash or silent skip."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Gamma")
    conn.execute(
        "UPDATE PerformanceData SET beatData = ? WHERE trackId = ?",
        (b"\xff\xfe not-beat-data", tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "beat_data")
    assert d.expected == "decodable"
    assert "decode_error" in str(d.actual)
    assert d.track_id == 30


# ---------------------------------------------------------------------------
# Quick-cue mutations (sample offset, colour, label, pad index)
# ---------------------------------------------------------------------------


def test_verify_catches_quick_cue_sample_offset_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving a cue pad's sample offset must name pad index + expected/actual."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT quickCues FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    qc = decode_quick_cues(blob)
    cues = list(qc.cues)
    # Pad 0 holds the HOT cue "Drop" at 44100 in the fixture.
    assert cues[0].sample_offset == 44100.0
    new_offset = 99999.0
    cues[0] = QuickCue(cues[0].label, new_offset, cues[0].color)
    conn.execute(
        "UPDATE PerformanceData SET quickCues = ? WHERE trackId = ?",
        (encode_quick_cues(replace(qc, cues=cues)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "quick_cue[0].sample_offset")
    assert d.track_id == 10
    assert d.expected == 44100
    assert d.actual == int(new_offset)


def test_verify_catches_quick_cue_color_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARGB colour drift on a pad is reported with expected vs actual tuples."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT quickCues FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    qc = decode_quick_cues(blob)
    cues = list(qc.cues)
    # Fixture Drop is red ARGB(255,255,0,0); flip to blue.
    cues[0] = QuickCue(cues[0].label, cues[0].sample_offset, Color(255, 0, 0, 255))
    conn.execute(
        "UPDATE PerformanceData SET quickCues = ? WHERE trackId = ?",
        (encode_quick_cues(replace(qc, cues=cues)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "quick_cue[0].color")
    assert d.expected == (255, 255, 0, 0)
    assert d.actual == (255, 0, 0, 255)
    assert d.track_id == 10


def test_verify_catches_quick_cue_label_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pad label text is muscle memory — a silent rename must surface."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT quickCues FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    qc = decode_quick_cues(blob)
    cues = list(qc.cues)
    assert cues[0].label == "Drop"
    cues[0] = QuickCue("RENAMED", cues[0].sample_offset, cues[0].color)
    conn.execute(
        "UPDATE PerformanceData SET quickCues = ? WHERE trackId = ?",
        (encode_quick_cues(replace(qc, cues=cues)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "quick_cue[0].label")
    assert d.expected == "Drop"
    assert d.actual == "RENAMED"


def test_verify_catches_quick_cue_pad_index_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving a cue to a different pad reports the empty source pad's offset.

    WHY: Pad A vs pad C is a different button under the finger. Verify must
    notice the source pad is empty in m.db, not only that 'some pad' has a cue.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT quickCues FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    qc = decode_quick_cues(blob)
    cues = list(qc.cues)
    moved = cues[0]
    # Clear pad 0 (empty sentinel) and place the cue on pad 3.
    cues[0] = QuickCue("", -1.0, Color(0, 0, 0, 0))
    cues[3] = QuickCue(moved.label, moved.sample_offset, moved.color)
    conn.execute(
        "UPDATE PerformanceData SET quickCues = ? WHERE trackId = ?",
        (encode_quick_cues(replace(qc, cues=cues)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    # Source still expects the cue on pad 0.
    d = _disc(result, "quick_cue[0].sample_offset")
    assert d.expected == 44100
    assert d.actual == -1 or d.actual is None


# ---------------------------------------------------------------------------
# Loop mutations
# ---------------------------------------------------------------------------


def test_verify_catches_loop_start_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop start sample drift is reported as loop[i].start with both values."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT loops FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    lp = decode_loops(blob)
    loops = list(lp.loops)
    # Fixture loop-cue lands in loop slot 0 at start 88200.
    assert loops[0].start_sample_offset == 88200.0
    new_start = 1000.0
    loops[0] = replace(loops[0], start_sample_offset=new_start)
    conn.execute(
        "UPDATE PerformanceData SET loops = ? WHERE trackId = ?",
        (encode_loops(replace(lp, loops=loops)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "loop[0].start")
    assert d.expected == 88200
    assert d.actual == int(new_start)
    assert d.track_id == 10


def test_verify_catches_loop_end_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop end sample drift is reported as loop[i].end with both values."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT loops FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    lp = decode_loops(blob)
    loops = list(lp.loops)
    assert loops[0].end_sample_offset == 132300.0
    new_end = 200000.0
    loops[0] = replace(loops[0], end_sample_offset=new_end)
    conn.execute(
        "UPDATE PerformanceData SET loops = ? WHERE trackId = ?",
        (encode_loops(replace(lp, loops=loops)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "loop[0].end")
    assert d.expected == 132300
    assert d.actual == int(new_end)


def test_verify_catches_corrupt_loops_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undecodable loops blob surfaces as field=loops, not a traceback."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Beta")
    # loops is uncompressed LE; a short blob fails the count/length checks.
    conn.execute(
        "UPDATE PerformanceData SET loops = ? WHERE trackId = ?",
        (b"\x01\x02", tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "loops")
    assert d.expected == "decodable"
    assert "decode_error" in str(d.actual)
    assert d.track_id == 20


# ---------------------------------------------------------------------------
# Track metadata: title, artist, key, path existence
# ---------------------------------------------------------------------------


def test_verify_catches_title_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute("UPDATE Track SET title = 'WRONG' WHERE title = 'Alpha'")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "title")
    assert d.expected == "Alpha"
    assert d.actual == "WRONG"
    assert d.track_id == 10


def test_verify_catches_artist_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute("UPDATE Track SET artist = 'Other' WHERE title = 'Beta'")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "artist")
    assert d.expected == "Artist"
    assert d.actual == "Other"
    assert d.track_id == 20


def test_verify_catches_key_ordinal_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Track.key ordinal must match the mapped source key (Am → 1)."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    # Am maps to ordinal 1; force C major (0) into the column.
    conn.execute("UPDATE Track SET key = 0 WHERE title = 'Alpha'")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "key")
    assert d.expected == 1
    assert d.actual == 0
    assert d.track_id == 10


def test_verify_catches_track_data_key_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """trackData blob key ordinal is checked independently of Track.key."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Alpha")
    blob = conn.execute(
        "SELECT trackData FROM PerformanceData WHERE trackId = ?", (tid,)
    ).fetchone()[0]
    td = decode_track_data(blob)
    assert td.key == 1  # Am
    conn.execute(
        "UPDATE PerformanceData SET trackData = ? WHERE trackId = ?",
        (encode_track_data(replace(td, key=12)), tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "track_data.key")
    assert d.expected == 1
    assert d.actual == 12


def test_verify_catches_corrupt_track_data_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    tid = _track_row_id(conn, "Gamma")
    conn.execute(
        "UPDATE PerformanceData SET trackData = ? WHERE trackId = ?",
        (b"\x00\x00\x00\x04notz", tid),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "track_data")
    assert d.expected == "decodable"
    assert "decode_error" in str(d.actual)


def test_verify_catches_missing_audio_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Track.path that no longer resolves on disk is path_exists=False.

    WHY: m.db can look perfect while every file on the stick is gone — the
    classic 'library opens, nothing plays' failure.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    audio = drive / "Contents" / "a.mp3"
    assert audio.is_file()
    audio.unlink()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "path_exists")
    assert d.expected is True
    assert d.actual is False
    assert d.track_id == 10


# ---------------------------------------------------------------------------
# Playlist: drop, rename, count, duplicate-name resolution, chain cycle
# ---------------------------------------------------------------------------


def test_verify_catches_dropped_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source playlist with no matching m.db row is playlist[name].missing."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute("DELETE FROM PlaylistEntity")
    conn.execute("DELETE FROM Playlist")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    assert "playlist_count" in _fields(result.discrepancies)
    d = _disc(result, "playlist[Main Set].missing")
    assert d.expected == "present"
    assert d.actual == "absent"
    assert d.track_id is None


def test_verify_catches_playlist_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming a playlist so neither exact nor 'Name (N)' matches → missing."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "UPDATE Playlist SET title = ? WHERE title = ?",
        ("Totally Different Name", "Main Set"),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    # Count still 1==1, but the named playlist is gone.
    d = _disc(result, "playlist[Main Set].missing")
    assert d.expected == "present"
    assert d.actual == "absent"


def test_verify_reports_externally_renamed_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list retitled in the database must be reported, not silently matched.

    WHY: the writer only produces a " (N)" suffix when the SOURCE holds two
    playlists of that name in one folder — see
    test_verify_pairs_same_folder_duplicates_with_their_own_lists, which covers
    that case. With a single source playlist no rename can occur, so a database
    titled "Main Set (2)" has been changed by something other than us. Treating
    it as a match assumes the database is right whenever its title merely looks
    like a rename, which is how "House" came to be verified against the
    unrelated list "House (old)".
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "UPDATE Playlist SET title = ? WHERE title = ?",
        ("Main Set (2)", "Main Set"),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    assert "playlist[Main Set].missing" in _fields(result.discrepancies)


def test_verify_catches_extra_playlist_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extra Playlist rows (not in source) must bump playlist_count."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "INSERT INTO Playlist (title, parentListId, isPersisted, nextListId, "
        "lastEditTime, isExplicitlyExported) VALUES ('Orphan', 0, 1, 0, "
        "'1970-01-01 00:00:00', 1)"
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    d = _disc(result, "playlist_count")
    assert d.expected == 1
    assert d.actual == 2
    assert result.external_edits == []


# ---------------------------------------------------------------------------
# External-edit classifier — Engine DJ merging its desktop library is not
# corruption. Measured 2026-07-31 on real hardware: opening Engine DJ with a
# populated desktop library turned 45 converted playlists into 48, every
# addition carrying a real lastEditTime against our pinned epoch, with
# Playlist ids reassigned up to 84 against our contiguous 1..45.
# ---------------------------------------------------------------------------


def test_verify_classifies_engine_added_playlist_as_external_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extra playlist with a real lastEditTime is informational, not a defect.

    WHY: verify used to report an Engine desktop-library merge with the same
    playlist_count discrepancy as corruption (expected=45 actual=48 in the
    field incident). Users who see corruption reports for a legitimate Engine
    feature stop trusting verify. The pinned-epoch rule attributes the rows.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "INSERT INTO Playlist (title, parentListId, isPersisted, nextListId, "
        "lastEditTime, isExplicitlyExported) VALUES ('Setlist Bigroom 2', 0, 1, "
        "0, '2026-07-25 04:04:07', 1)"
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    # Informational only: verification still passes, exit code unchanged.
    assert result.ok is True
    assert "playlist_count" not in _fields(result.discrepancies)
    assert len(result.external_edits) == 1
    edit = result.external_edits[0]
    assert edit.label == "Setlist Bigroom 2"
    assert edit.last_edit_time == "2026-07-25 04:04:07"
    assert edit.beyond_watermark is True

    # The rendered output must tell the troubleshooting story: names the
    # playlist, attributes it to Engine DJ, says the conversion is fine, and
    # points at re-running convert.
    text = result.render_text()
    assert "Setlist Bigroom 2" in text
    assert "Engine DJ" in text
    assert "not corrupt" in text
    assert "Re-run convert" in text


def test_verify_external_edit_exit_code_stays_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI verify must exit 0 when the only finding is an Engine-added playlist.

    WHY: exit 1 means "conversion does not match the source, act on it".
    Scripting around verify (CI, pre-gig checklists) must not go red because
    Engine DJ synced its own desktop library — that is the benign class the
    classifier exists to separate.
    """
    from click.testing import CliRunner

    from rb2engine.cli import main

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "INSERT INTO Playlist (title, parentListId, isPersisted, nextListId, "
        "lastEditTime, isExplicitlyExported) VALUES ('Merged', 0, 1, 0, "
        "'2026-07-30 09:20:00', 1)"
    )
    conn.commit()
    conn.close()

    result = CliRunner().invoke(main, ["verify", str(drive), "--no-artwork"])

    assert result.exit_code == 0, result.output
    assert "Merged" in result.output
    assert "Engine DJ" in result.output


def test_verify_pinned_epoch_wrong_membership_is_still_a_discrepancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A playlist carrying OUR epoch with wrong membership stays a hard finding.

    WHY: the classifier separates Engine's additions from defects — it must
    never launder a genuine wrong write. A row pinned to our epoch claims to be
    ours, so its membership diverging from the source is exactly the incident
    class verify exists to catch.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    # Drop Gamma from the playlist with a coherent chain (tail rewired to 0)
    # and lastEditTime still pinned — a faithful-looking wrong write, the same
    # shape as the original phantom-entry incident.
    gamma = _track_row_id(conn, "Gamma")
    row = conn.execute(
        "SELECT id FROM PlaylistEntity WHERE trackId = ?", (gamma,)
    ).fetchone()
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = 0 WHERE nextEntityId = ?",
        (row[0],),
    )
    conn.execute("DELETE FROM PlaylistEntity WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    assert "playlist[Main Set].track_order" in _fields(result.discrepancies)
    assert result.external_edits == []


def test_verify_id_reassignment_with_pinned_epoch_is_not_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows renumbered above the id watermark but carrying our epoch: no report.

    WHY: the id-reassignment trap. Engine provably renumbers Playlist ids when
    it rewrites m.db (ids ran to 84 against our contiguous 1..45), so an id
    above our allocation can be one of our own rows moved. Classifying on ids
    would mass-report the whole library as externally edited after a rewrite
    that changed nothing logical. Only a foreign lastEditTime attributes a row
    to Engine.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    # Simulate Engine renumbering every playlist far above our 1..N allocation
    # while preserving content and our pinned epoch.
    conn.execute("UPDATE PlaylistEntity SET listId = listId + 100")
    conn.execute(
        "UPDATE Playlist SET id = id + 100, "
        "parentListId = CASE parentListId WHEN 0 THEN 0 "
        "ELSE parentListId + 100 END, "
        "nextListId = CASE nextListId WHEN 0 THEN 0 ELSE nextListId + 100 END"
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.external_edits == []
    assert result.ok is True


def test_verify_mixed_extra_playlists_split_by_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Engine-added extra and one pinned-epoch extra must split cleanly.

    WHY: subtracting Engine's additions from the count must not absorb an
    extra row that claims to be ours — that one still has no source playlist
    behind it and stays a playlist_count discrepancy.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "INSERT INTO Playlist (title, parentListId, isPersisted, nextListId, "
        "lastEditTime, isExplicitlyExported) VALUES ('From Engine', 0, 1, 0, "
        "'2026-07-25 04:11:00', 1)"
    )
    conn.execute(
        "INSERT INTO Playlist (title, parentListId, isPersisted, nextListId, "
        "lastEditTime, isExplicitlyExported) VALUES ('Claims To Be Ours', 0, 1, "
        "0, '1970-01-01 00:00:00', 1)"
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert [e.label for e in result.external_edits] == ["From Engine"]
    d = _disc(result, "playlist_count")
    assert d.expected == 1
    assert d.actual == 2  # ours + the pinned impostor; Engine's row excluded
    assert result.ok is False


def test_verify_playlist_chain_cycle_does_not_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cyclic nextEntityId chain must terminate and still be flagged.

    WHY: Without a cycle guard, a corrupted stick could hang verify forever —
    worse than reporting a discrepancy.

    The cycle orphans e3, so it surfaces as a `.chain` discrepancy naming the
    unreachable row. It used to be reported only indirectly, as whatever track
    order the truncated walk happened to produce.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    entities = conn.execute(
        "SELECT id FROM PlaylistEntity WHERE listId = 1 ORDER BY id"
    ).fetchall()
    e1, e2, e3 = entities[0][0], entities[1][0], entities[2][0]
    # Cycle: e1 → e2 → e1 (e3 orphaned). Tail sentinel never reached cleanly.
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = ? WHERE id = ?", (e2, e1)
    )
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = ? WHERE id = ?", (e1, e2)
    )
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = 0 WHERE id = ?", (e3,)
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    assert result.ok is False
    assert "playlist[Main Set].chain" in _fields(result.discrepancies)


def test_verify_reports_unreachable_entity_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphaned row must be reported, not quietly dropped from the order.

    WHY: verify used to walk the chain and return whatever it reached, so a row
    Engine may still honour vanished from the comparison and the library was
    reported clean — while the writer's own gate refuses to publish that exact
    database. Both now share rb2engine.chain, so they cannot disagree about
    whether there is a defect.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    entities = conn.execute(
        "SELECT id FROM PlaylistEntity WHERE listId = 1 ORDER BY id"
    ).fetchall()
    e2, e3 = entities[1][0], entities[2][0]
    # Make e2 the tail and strand e3 behind a successor id that does not exist.
    # The chain is otherwise well-formed, so only the row count reveals e3.
    conn.execute("UPDATE PlaylistEntity SET nextEntityId = 0 WHERE id = ?", (e2,))
    conn.execute("UPDATE PlaylistEntity SET nextEntityId = 8888 WHERE id = ?", (e3,))
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    assert result.ok is False
    fields = _fields(result.discrepancies)
    assert "playlist[Main Set].chain" in fields


def test_verify_playlist_skips_unknown_and_unresolved_source_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expected entity list ignores missing rb_ids and unresolved paths.

    WHY: Source playlists can reference dropped tracks; verify must not invent
    Engine ids for them, and must not crash — only compare the resolvable
    prefix against the written chain.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)

    # Replace playlist membership: unknown id, unresolved path, plus real tracks
    # with a deliberate duplicate (first occurrence wins in expected list).
    unresolved = replace(lib.tracks[30], resolved_path=None)
    tracks = dict(lib.tracks)
    tracks[30] = unresolved
    playlists = [
        SourcePlaylist(
            rb_id=1,
            parent_rb_id=0,
            name="Main Set",
            sort_order=0,
            is_folder=False,
            # 999 missing from tracks; 30 has no path; 10 duplicated.
            track_rb_ids=[10, 10, 999, 30, 20],
        )
    ]
    lib2 = SourceLibrary(
        drive_root=lib.drive_root, tracks=tracks, playlists=playlists, warnings=[]
    )
    _patch_read_library(monkeypatch, lib2)

    result = verify_library(drive, with_artwork=False)
    # Expected resolvable unique order is [track10, track20]; actual DB still
    # has [10,20,30] engine ids — order mismatch proves the skip path ran
    # (if 999/30/dup were not skipped, expected would be a different shape).
    d = _disc(result, "playlist[Main Set].track_order")
    assert len(d.expected) == 2  # 10 once, then 20 — 999/30 skipped, dup collapsed
    assert len(d.actual) == 3


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------


def test_verify_catches_deleted_album_art_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting AlbumArt while source still has artwork keys → album_art_count."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path, with_artwork=True)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    assert conn.execute("SELECT COUNT(*) FROM AlbumArt").fetchone()[0] >= 1
    conn.execute("DELETE FROM AlbumArt")
    # Clear FKs so the delete is the only intentional damage under test.
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("UPDATE Track SET albumArtId = NULL")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=True)
    d = _disc(result, "album_art_count")
    assert d.expected >= 1
    assert d.actual == 0
    assert d.track_id is None


def test_verify_artwork_count_matches_unique_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """with_artwork=True on a faithful build: album art count agrees (0 disc)."""
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path, with_artwork=True)
    _patch_read_library(monkeypatch, lib)

    result = verify_library(drive, with_artwork=True)
    assert result.ok is True
    assert "album_art_count" not in _fields(result.discrepancies)


def test_verify_catches_extra_album_art_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extra AlbumArt blob (e.g. stale row after re-convert) fails the count.

    Note: verify compares counts of unique content keys, not per-row bytes —
    byte-level art drift with the same count is out of scope of this check.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path, with_artwork=True)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "INSERT INTO AlbumArt (hash, albumArt) VALUES (?, ?)",
        ("deadbeef", b"not-real-png-bytes"),
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM AlbumArt").fetchone()[0]
    conn.close()

    result = verify_library(drive, with_artwork=True)
    d = _disc(result, "album_art_count")
    assert d.actual == before
    assert d.expected == before - 1


# ---------------------------------------------------------------------------
# Cannot-verify paths — must raise, never report a false OK
# ---------------------------------------------------------------------------


def test_verify_raises_when_no_engine_library(tmp_path: Path) -> None:
    """No Engine Library/Database2/m.db → FatalError (CLI exit 2)."""
    from rb2engine.verify import verify_library

    drive = tmp_path / "empty_stick"
    drive.mkdir()
    (drive / "PIONEER").mkdir()

    with pytest.raises(FatalError, match=r"no m.db"):
        verify_library(drive)


def test_verify_raises_on_unreadable_mdb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage bytes at m.db path must not verify as OK."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)
    m_db.write_bytes(b"this is not a sqlite database at all")

    with pytest.raises(FatalError, match=r"unreadable|non-Engine"):
        verify_library(drive, with_artwork=False)


def test_verify_raises_on_unsupported_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsupported Information schema triple → UnsupportedFormatError, not OK."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute(
        "UPDATE Information SET schemaVersionMajor = 99, "
        "schemaVersionMinor = 0, schemaVersionPatch = 0"
    )
    conn.commit()
    conn.close()

    with pytest.raises(UnsupportedFormatError, match="Unsupported Engine schema"):
        verify_library(drive, with_artwork=False)


def test_verify_catches_corrupted_artwork_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapped or corrupted image BLOBs must be caught, not just row counts.

    WHY: comparing AlbumArt row COUNT alone passes when the bytes are wrong —
    re-encoded, truncated, or the wrong cover entirely. The count is identical;
    the DJ sees the wrong artwork. Verify recomputes the dedup key over the
    stored blob, so any byte drift surfaces as a key the source never produced.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path, with_artwork=True)
    _patch_read_library(monkeypatch, lib)

    assert verify_library(drive, with_artwork=True).ok

    conn = sqlite3.connect(m_db)
    try:
        # Same row count, different bytes — the case a count check cannot see.
        conn.execute("UPDATE AlbumArt SET albumArt = ?", (b"\x89PNG\r\n\x1a\nDIFFERENT",))
        conn.commit()
    finally:
        conn.close()

    result = verify_library(drive, with_artwork=True)

    assert not result.ok
    assert any("content_key" in d.field for d in result.discrepancies), (
        f"expected an artwork content_key discrepancy, got {_fields(result.discrepancies)}"
    )
    # Row count is unchanged, proving the count check alone would have passed.
    assert not any(d.field == "album_art_count" for d in result.discrepancies)


def test_verify_catches_empty_artwork_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An AlbumArt row with no bytes is a writer bug that must not read as OK."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path, with_artwork=True)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(m_db)
    try:
        conn.execute("UPDATE AlbumArt SET albumArt = ?", (b"",))
        conn.commit()
    finally:
        conn.close()

    result = verify_library(drive, with_artwork=True)

    assert not result.ok
    assert any(".bytes" in d.field for d in result.discrepancies)


# ---------------------------------------------------------------------------
# Playlist pairing — verify must compare each source list against ITS OWN
# engine list. Getting this wrong invents discrepancies on a faithful build,
# which is worse than missing one: it trains the operator to ignore verify.
# ---------------------------------------------------------------------------


def _build_with_playlists(
    tmp_path: Path, playlists: list[SourcePlaylist]
) -> tuple[Path, SourceLibrary, Path]:
    """Build a 3-track library with caller-supplied playlists."""
    drive = tmp_path / "stick"
    drive.mkdir()
    (drive / "Contents").mkdir()
    (drive / "PIONEER").mkdir()

    tracks = {
        10: _source_track(10, drive=drive, title="Alpha", filename="a.mp3"),
        20: _source_track(20, drive=drive, title="Beta", filename="b.mp3"),
        30: _source_track(30, drive=drive, title="Gamma", filename="c.mp3"),
    }
    lib = SourceLibrary(
        drive_root=drive, tracks=tracks, playlists=playlists, warnings=[]
    )
    m_db = build_library(
        lib, drive_root=drive, report=ConversionReport(), with_artwork=False
    )
    return drive, lib, m_db


def test_verify_pairs_same_name_playlists_in_different_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same title under two different folders must not collapse to one list.

    WHY: Engine's uniqueness constraint is per-parent, so "Chill" may legally
    exist in several folders. Keying the lookup on title alone makes the last
    row scanned win, and every same-named list is then compared against that
    one — reporting a wrong-tracks discrepancy on a build that is correct.
    """
    from rb2engine.verify import verify_library

    playlists = [
        SourcePlaylist(
            rb_id=1, parent_rb_id=0, name="Folder A", sort_order=0,
            is_folder=True, track_rb_ids=[],
        ),
        SourcePlaylist(
            rb_id=2, parent_rb_id=0, name="Folder B", sort_order=1,
            is_folder=True, track_rb_ids=[],
        ),
        SourcePlaylist(
            rb_id=3, parent_rb_id=1, name="Chill", sort_order=0,
            is_folder=False, track_rb_ids=[10, 20],
        ),
        SourcePlaylist(
            rb_id=4, parent_rb_id=2, name="Chill", sort_order=0,
            is_folder=False, track_rb_ids=[10, 30],
        ),
    ]
    drive, lib, _ = _build_with_playlists(tmp_path, playlists)
    _patch_read_library(monkeypatch, lib)

    result = verify_library(drive, with_artwork=False)

    assert result.ok is True, (
        "faithful build reported discrepancies: "
        f"{[(d.field, d.expected, d.actual) for d in result.discrepancies]}"
    )


def test_verify_pairs_same_folder_duplicates_with_their_own_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renamed duplicates must each verify against the list they became.

    WHY: rekordbox allows two playlists of the same name in one folder; the
    writer renames the second to "Name (2)". Both source lists still carry the
    original name, so an exact-title lookup resolves BOTH to the first engine
    list — the second is then diffed against the first's tracks.
    """
    from rb2engine.verify import verify_library

    playlists = [
        SourcePlaylist(
            rb_id=1, parent_rb_id=0, name="Sets", sort_order=0,
            is_folder=True, track_rb_ids=[],
        ),
        SourcePlaylist(
            rb_id=2, parent_rb_id=1, name="Setlist", sort_order=0,
            is_folder=False, track_rb_ids=[10, 20],
        ),
        SourcePlaylist(
            rb_id=3, parent_rb_id=1, name="Setlist", sort_order=1,
            is_folder=False, track_rb_ids=[10, 30],
        ),
    ]
    drive, lib, m_db = _build_with_playlists(tmp_path, playlists)
    _patch_read_library(monkeypatch, lib)

    # Precondition: the writer really did rename the second duplicate.
    conn = sqlite3.connect(str(m_db))
    titles = {
        str(r[0]) for r in conn.execute("SELECT title FROM Playlist").fetchall()
    }
    conn.close()
    assert {"Setlist", "Setlist (2)"} <= titles, titles

    result = verify_library(drive, with_artwork=False)

    assert result.ok is True, (
        "faithful build reported discrepancies: "
        f"{[(d.field, d.expected, d.actual) for d in result.discrepancies]}"
    )


def test_verify_does_not_match_unrelated_suffixed_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing "House" must not silently resolve to "House (old)".

    WHY: the duplicate-suffix scan matches any title starting with "House (",
    which is a real, differently-named playlist — not a rename of this one. The
    absent list is then reported as a track mismatch against an unrelated set
    instead of as missing, pointing the operator at the wrong playlist.
    """
    from rb2engine.verify import verify_library

    playlists = [
        SourcePlaylist(
            rb_id=1, parent_rb_id=0, name="House", sort_order=0,
            is_folder=False, track_rb_ids=[10, 20],
        ),
        SourcePlaylist(
            rb_id=2, parent_rb_id=0, name="House (old)", sort_order=1,
            is_folder=False, track_rb_ids=[30],
        ),
    ]
    drive, lib, m_db = _build_with_playlists(tmp_path, playlists)
    _patch_read_library(monkeypatch, lib)

    # Drop the "House" list so its lookup genuinely fails.
    conn = sqlite3.connect(str(m_db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM Playlist WHERE title = 'House'")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    fields = _fields(result.discrepancies)
    assert "playlist[House].missing" in fields, fields
    # It must NOT have been diffed against "House (old)".
    assert "playlist[House].track_order" not in fields, fields


def test_db_playlist_paths_survives_a_malformed_parent_chain() -> None:
    """A cyclic or orphaned parent chain must not hang or crash verify.

    WHY: these paths are walked in a database rb2engine did not necessarily
    write — Engine and the hardware also modify it. Verify's job on a corrupt
    library is to report, so the walk has to terminate on structures the writer
    would have refused to create.
    """
    from rb2engine.playlist_check import db_playlist_paths as _db_playlist_paths

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE Playlist (id INTEGER, title TEXT, parentListId INTEGER)"
    )
    conn.executemany(
        "INSERT INTO Playlist (id, title, parentListId) VALUES (?, ?, ?)",
        [
            (1, "Root", 0),
            (2, "Child", 1),
            (3, "Orphan", 99),  # parent does not exist
            (4, "CycleA", 5),  # 4 → 5 → 4
            (5, "CycleB", 4),
        ],
    )
    conn.commit()

    paths = _db_playlist_paths(conn)
    conn.close()

    assert paths[("Root",)] == 1
    assert paths[("Root", "Child")] == 2
    # Orphan truncates at the missing parent rather than looping forever.
    assert paths[("Orphan",)] == 3
    # Both cycle members terminate; each yields a finite path.
    assert any(p[-1] == "CycleA" for p in paths)
    assert any(p[-1] == "CycleB" for p in paths)
