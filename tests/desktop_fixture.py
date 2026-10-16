"""Small authored USB export for desktop integration and frozen-app smoke tests."""
from __future__ import annotations

import wave
from pathlib import Path

from tests.unit.test_pdb import (
    _build_data_page,
    _file_header,
    _playlist_entry_row,
    _playlist_tree_row,
    _track_row,
    _write_pdb,
)


def make_drive(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "PIONEER" / "rekordbox").mkdir(parents=True, exist_ok=True)
    (root / "PIONEER" / "USBANLZ").mkdir(exist_ok=True)
    (root / "Contents").mkdir(exist_ok=True)
    audio = root / "Contents" / "test.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(bytes(44100 * 4))
    size = 4096
    pages = {0: _file_header(len_page=size, tables=[(0, 1, 1), (7, 2, 2), (8, 3, 3)])}
    rows = [
        (0, _track_row(tid=1, title="First Light", file_path="/Contents/test.wav",
                       duration=1, file_size=audio.stat().st_size)),
        (7, _playlist_tree_row(parent_id=0, sort_order=0, pl_id=1,
                               is_folder=False, name="Opening Set")),
        (8, _playlist_entry_row(0, track_id=1, playlist_id=1)),
    ]
    for index, (page_type, row) in enumerate(rows, 1):
        pages[index] = _build_data_page(len_page=size, page_index=index,
                                       page_type=page_type, next_page=99, row_blobs=[row])
    _write_pdb(root / "PIONEER" / "rekordbox" / "export.pdb", size, pages)
    return root
