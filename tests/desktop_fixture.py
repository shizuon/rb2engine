"""Small authored USB export for desktop integration and frozen-app smoke tests."""
from __future__ import annotations

import math
import sqlite3
import struct
import wave
from contextlib import closing
from pathlib import Path

from tests.unit.test_pdb import (
    _build_data_page,
    _file_header,
    _playlist_entry_row,
    _playlist_tree_row,
    _track_row,
    _write_pdb,
)


def make_drive(
    root: Path, *, tone: bool = False, analysis: bool = False, duration: int | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "PIONEER" / "rekordbox").mkdir(parents=True, exist_ok=True)
    (root / "PIONEER" / "USBANLZ").mkdir(exist_ok=True)
    (root / "Contents").mkdir(exist_ok=True)
    audio = root / "Contents" / "test.wav"
    duration = duration if duration is not None else 30 if tone else 4 if analysis else 1
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        if tone:
            # Authored A-minor chord; integration input, not an accuracy benchmark.
            for second in range(duration):
                frames = bytearray()
                for sample in range(44100):
                    t = second + sample / 44100
                    value = int(1600 * sum(math.sin(2 * math.pi * f * t)
                                           for f in (220, 261.6256, 329.6276)))
                    frames.extend(struct.pack("<hh", value, value))
                stream.writeframes(frames)
        else:
            stream.writeframes(bytes(44100 * 4 * duration))
    analyze_path = ""
    if analysis:
        from tests.unit.test_anlz import _pcob, _pcpt, _pmai, _pqt2_minimal, _pqtz

        analyze_path = "/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT"
        dat = root / analyze_path.lstrip("/")
        dat.parent.mkdir(parents=True)
        # At 120 BPM, the first stored beat (0.25 s) is bar beat 3; the first
        # downbeat is at 1.25 s. There is no tempo change in this fixture.
        beats = [((index + 2) % 4 + 1, 12000, 250 + index * 500)
                 for index in range(duration * 2)]
        dat.write_bytes(_pmai([_pqtz(beats)]))
        optional = bytearray(_pqt2_minimal())
        struct.pack_into(">I", optional, 16, 0x02000002)
        dat.with_suffix(".EXT").write_bytes(_pmai([
            bytes(optional), _pcob(1, [_pcpt(1, 1250)]),
        ]))
    size = 4096
    pages = {0: _file_header(len_page=size, tables=[(0, 1, 1), (7, 2, 2), (8, 3, 3)])}
    rows = [
        (0, _track_row(tid=1, title="First Light", file_path="/Contents/test.wav",
                       duration=duration, file_size=audio.stat().st_size,
                       tempo=12000 if analysis else 12800, analyze_path=analyze_path)),
        (7, _playlist_tree_row(parent_id=0, sort_order=0, pl_id=1,
                               is_folder=False, name="Opening Set")),
        (8, _playlist_entry_row(0, track_id=1, playlist_id=1)),
    ]
    for index, (page_type, row) in enumerate(rows, 1):
        pages[index] = _build_data_page(len_page=size, page_index=index,
                                       page_type=page_type, next_page=99, row_blobs=[row])
    _write_pdb(root / "PIONEER" / "rekordbox" / "export.pdb", size, pages)
    return root


def check_analysis(root: Path) -> dict[str, object]:
    """Check the actual database output against authored beat/Cue positions."""
    from rb2engine.writer.blobs import decode_beat_data, decode_quick_cues

    database = root / "Engine Library" / "Database2" / "m.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        beat_blob, cue_blob = conn.execute(
            "SELECT beatData, quickCues FROM PerformanceData"
        ).fetchone()
        bpm, = conn.execute("SELECT bpmAnalyzed FROM Track").fetchone()
    data = decode_beat_data(beat_blob)
    cues = decode_quick_cues(cue_blob)
    for grid in (data.default_beat_grid, data.adjusted_beat_grid):
        first, last = grid.markers
        samples_per_beat = (last.sample_offset - first.sample_offset) / (
            last.beat_number - first.beat_number
        )
        # Engine bar boundaries are beat numbers divisible by four. Index 4
        # must be 1.25 seconds, even though the stored source list starts at 3.
        downbeat = first.sample_offset + (4 - first.beat_number) * samples_per_beat
        assert abs(downbeat - 55125) < 1e-6, downbeat
        assert abs(samples_per_beat - 22050) < 1e-6, samples_per_beat
    assert cues.cues[0].sample_offset == 55125
    assert bpm == 120.0
    return {"downbeat_seconds": 1.25, "cue_seconds": 1.25, "bpm": bpm,
            "optional_pqt2_preserved_data": True}
