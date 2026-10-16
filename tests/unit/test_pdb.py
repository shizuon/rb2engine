"""export.pdb DeviceSQL page walker — structural rules and fail-loud gates.

Why these tests exist
---------------------
export.pdb is a paged heap with a presence bitmask: deleted rows leave gaps in
the row-offset index. Parsing "every offset" invents ghost tracks; ignoring the
bitmask loses real ones. The page chain is a linked list (first→next→last), not
a contiguous range — treating it as an array range either crashes on holes or
walks into another table's pages.

Gates G1a/G1b/G1c are the plan's answer to "plausible-looking wrong output":
header insanity and unparseable *consumed* tables must abort (exit 2), while an
unknown page_type on a table we never read is a counted warning (exit 0). These
tests pin that distinction so a future "be more lenient" change cannot re-open
silent metadata loss.

Expectations come from the crate-digger rekordbox_pdb layout and from values
that are self-evidently sane on a real stick (BPM 60–200, sample rate 44100,
paths under Contents/ that exist). They are NOT transcripts of this parser.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

from rb2engine.errors import UnsupportedFormatError
from rb2engine.reader.pdb import _iter_present_row_bases, parse_export_pdb

# ---------------------------------------------------------------------------
# Synthetic DeviceSQL builders (hand-authored bytes, not from our parser)
# ---------------------------------------------------------------------------

PAGE_HEADER = 40
REAL_STICK = Path("/Volumes/USB DISK")
REAL_PDB = REAL_STICK / "PIONEER" / "rekordbox" / "export.pdb"


def _enc_short_ascii(text: str) -> bytes:
    """DeviceSQL short-ASCII: disc = (len+1)<<1 | 1, then payload."""
    payload = text.encode("ascii")
    field_len = len(payload) + 1
    disc = (field_len << 1) | 1
    assert disc <= 0xFF
    return bytes([disc]) + payload


def _u16(n: int) -> bytes:
    return struct.pack("<H", n)


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _page_header(
    *,
    page_index: int,
    page_type: int,
    next_page: int,
    num_row_offsets: int,
    num_rows: int,
    page_flags: int = 0x24,
    free_size: int = 0,
    used_size: int = 0,
) -> bytes:
    """40-byte page header matching crate-digger / real export.pdb packing."""
    word = (
        (num_row_offsets & 0x1FFF)
        | ((num_rows & 0x7FF) << 13)
        | ((page_flags & 0xFF) << 24)
    )
    return b"".join(
        [
            b"\x00\x00\x00\x00",
            _u32(page_index),
            _u32(page_type),
            _u32(next_page),
            _u32(1),  # sequence
            _u32(0),  # unknown
            _u32(word),
            _u16(free_size),
            _u16(used_size),
            _u16(0x1FFF),
            _u16(0x1FFF),
            _u16(0),
            _u16(0),
        ]
    )


def _pack_row_index(
    page: bytearray,
    len_page: int,
    offsets: list[int | None],
) -> None:
    """Write the backward-growing row index + presence bitmask.

    ``offsets[i]`` is the heap-relative row offset (bytes past page header),
    or None for a deleted/absent slot that still occupies an index entry.
    """
    n = len(offsets)
    if n == 0:
        return
    num_groups = (n - 1) // 16 + 1
    for g in range(num_groups):
        group_base = len_page - (g * 0x24)
        present = 0
        for r in range(16):
            abs_i = g * 16 + r
            if abs_i >= n:
                break
            ofs = offsets[abs_i]
            if ofs is None:
                # Deleted slot: write a dummy offset; bit stays clear.
                struct.pack_into("<H", page, group_base - (6 + 2 * r), 0)
            else:
                present |= 1 << r
                struct.pack_into("<H", page, group_base - (6 + 2 * r), ofs)
        struct.pack_into("<H", page, group_base - 4, present)
        # Transaction flags sit in the last 2 bytes of the group
        # (base-2 when base == len_page); never write past the page end.
        struct.pack_into("<H", page, group_base - 2, 0)


def _build_data_page(
    *,
    len_page: int,
    page_index: int,
    page_type: int,
    next_page: int,
    row_blobs: list[bytes | None],
    page_flags: int = 0x24,
) -> bytes:
    """Build one fixed-size page with optional deleted-row gaps (None blobs)."""
    page = bytearray(len_page)
    present_count = sum(1 for b in row_blobs if b is not None)
    header = _page_header(
        page_index=page_index,
        page_type=page_type,
        next_page=next_page,
        num_row_offsets=len(row_blobs),
        num_rows=present_count,
        page_flags=page_flags,
    )
    page[0:PAGE_HEADER] = header

    heap_cursor = PAGE_HEADER
    offsets: list[int | None] = []
    for blob in row_blobs:
        if blob is None:
            offsets.append(None)
            continue
        ofs = heap_cursor - PAGE_HEADER
        page[heap_cursor : heap_cursor + len(blob)] = blob
        heap_cursor += len(blob)
        # rows are typically 4-byte aligned in real files
        pad = (-heap_cursor) % 4
        heap_cursor += pad
        offsets.append(ofs)

    _pack_row_index(page, len_page, offsets)
    return bytes(page)


def _build_nondata_page(
    *, len_page: int, page_index: int, page_type: int, next_page: int
) -> bytes:
    """Strange/index page (flags 0x40 set) — zero rows, still part of the chain."""
    page = bytearray(len_page)
    page[0:PAGE_HEADER] = _page_header(
        page_index=page_index,
        page_type=page_type,
        next_page=next_page,
        num_row_offsets=0,
        num_rows=0,
        page_flags=0x64,
    )
    return bytes(page)


def _file_header(
    *,
    len_page: int,
    tables: list[tuple[int, int, int]],
    next_unused: int | None = None,
) -> bytes:
    """tables: list of (page_type, first_page, last_page)."""
    num = len(tables)
    if next_unused is None:
        next_unused = max((last for _, _, last in tables), default=0) + 1
    parts = [
        _u32(0),
        _u32(len_page),
        _u32(num),
        _u32(next_unused),
        _u32(0),
        _u32(1),
        b"\x00\x00\x00\x00",
    ]
    for ptype, first, last in tables:
        parts += [_u32(ptype), _u32(0), _u32(first), _u32(last)]
    hdr = b"".join(parts)
    # Pad to a full page so page indices align.
    assert len(hdr) <= len_page, "header overflowed page 0"
    return hdr.ljust(len_page, b"\x00")


def _playlist_entry_row(entry_index: int, track_id: int, playlist_id: int) -> bytes:
    return _u32(entry_index) + _u32(track_id) + _u32(playlist_id)


def _playlist_tree_row(
    *, parent_id: int, sort_order: int, pl_id: int, is_folder: bool, name: str
) -> bytes:
    return b"".join(
        [
            _u32(parent_id),
            _u32(0),
            _u32(sort_order),
            _u32(pl_id),
            _u32(1 if is_folder else 0),
            _enc_short_ascii(name),
        ]
    )


def _genre_row(gid: int, name: str) -> bytes:
    return _u32(gid) + _enc_short_ascii(name)


def _artist_row(aid: int, name: str) -> bytes:
    # subtype 0x60, near name offset at byte 9 → name starts at row+10
    name_bytes = _enc_short_ascii(name)
    # layout: subtype u2, index_shift u2, id u4, pad u1, ofs_name_near u1, name...
    # put name immediately after the 10-byte fixed header; ofs_name_near = 10
    fixed = _u16(0x60) + _u16(0) + _u32(aid) + bytes([0x03, 10])
    assert len(fixed) == 10
    return fixed + name_bytes


def _album_row(album_id: int, artist_id: int, name: str) -> bytes:
    # subtype 0x80; ofs_name_near at 0x15 = 21; name follows 22-byte header
    name_bytes = _enc_short_ascii(name)
    fixed = (
        _u16(0x80)
        + _u16(0)
        + _u32(0)
        + _u32(artist_id)
        + _u32(album_id)
        + _u32(0)
        + bytes([0x03, 22])
    )
    assert len(fixed) == 22
    return fixed + name_bytes


def _key_row(kid: int, name: str) -> bytes:
    return _u32(kid) + _u32(kid) + _enc_short_ascii(name)


def _label_row(lid: int, name: str) -> bytes:
    return _u32(lid) + _enc_short_ascii(name)


def _color_row(cid: int, name: str) -> bytes:
    return bytes(5) + _u16(cid) + bytes([0]) + _enc_short_ascii(name)


def _track_row(
    *,
    tid: int,
    title: str,
    file_path: str,
    artist_id: int = 0,
    album_id: int = 0,
    genre_id: int = 0,
    key_id: int = 0,
    label_id: int = 0,
    composer_id: int = 0,
    remixer_id: int = 0,
    sample_rate: int = 44100,
    tempo: int = 12800,  # BPM * 100
    duration: int = 180,
    bitrate: int = 320,
    file_size: int = 1_000_000,
    track_number: int = 1,
    disc_number: int = 1,
    play_count: int = 0,
    year: int = 2024,
    rating: int = 0,
    comment: str = "",
    filename: str | None = None,
    analyze_path: str = "",
) -> bytes:
    """Minimal track_row with string-offset table; file_path at index 20."""
    if filename is None:
        filename = file_path.rsplit("/", 1)[-1]

    # Fixed header through rating: 94 bytes, then 21 × u2 offsets.
    # Place all strings after the offset table (94 + 42 = 136).
    strings = [""] * 21
    strings[14] = analyze_path
    strings[16] = comment
    strings[17] = title
    strings[19] = filename
    strings[20] = file_path

    encoded = [_enc_short_ascii(s) for s in strings]
    cursor = 136
    ofs_list: list[int] = []
    blob_tail = bytearray()
    for enc in encoded:
        ofs_list.append(cursor)
        blob_tail += enc
        cursor += len(enc)

    fixed = bytearray(94)
    struct.pack_into("<H", fixed, 0, 0x24)  # subtype
    struct.pack_into("<I", fixed, 8, sample_rate)
    struct.pack_into("<I", fixed, 12, composer_id)
    struct.pack_into("<I", fixed, 16, file_size)
    struct.pack_into("<I", fixed, 32, key_id)
    struct.pack_into("<I", fixed, 40, label_id)
    struct.pack_into("<I", fixed, 44, remixer_id)
    struct.pack_into("<I", fixed, 48, bitrate)
    struct.pack_into("<I", fixed, 52, track_number)
    struct.pack_into("<I", fixed, 56, tempo)
    struct.pack_into("<I", fixed, 60, genre_id)
    struct.pack_into("<I", fixed, 64, album_id)
    struct.pack_into("<I", fixed, 68, artist_id)
    struct.pack_into("<I", fixed, 72, tid)
    struct.pack_into("<H", fixed, 76, disc_number)
    struct.pack_into("<H", fixed, 78, play_count)
    struct.pack_into("<H", fixed, 80, year)
    struct.pack_into("<H", fixed, 82, 16)  # sample_depth
    struct.pack_into("<H", fixed, 84, duration)
    fixed[89] = rating & 0xFF

    ofs_bytes = b"".join(_u16(o) for o in ofs_list)
    assert len(ofs_bytes) == 42
    return bytes(fixed) + ofs_bytes + bytes(blob_tail)


def _write_pdb(path: Path, len_page: int, pages: dict[int, bytes]) -> None:
    """pages maps page_index → exact len_page bytes (index 0 = header page)."""
    max_idx = max(pages)
    blob = bytearray((max_idx + 1) * len_page)
    for idx, page in pages.items():
        assert len(page) == len_page
        blob[idx * len_page : (idx + 1) * len_page] = page
    path.write_bytes(blob)


# ---------------------------------------------------------------------------
# Presence bitmask + deleted-row gaps
# ---------------------------------------------------------------------------


def test_presence_bitmask_skips_deleted_row_gaps(tmp_path: Path) -> None:
    """Deleted slots stay in the offset table; only present bits yield rows.

    If the walker treats num_row_offsets as a dense array it would invent a
    phantom entry for the cleared bit — or crash on garbage at that offset.
    """
    len_page = 512
    # Three slots: entry 0 present, entry 1 deleted, entry 2 present.
    rows: list[bytes | None] = [
        _playlist_entry_row(0, track_id=10, playlist_id=1),
        None,
        _playlist_entry_row(1, track_id=20, playlist_id=1),
    ]
    # Minimal valid library: empty tracks + tree + entries we care about.
    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[
                (0, 1, 1),  # tracks (empty non-data → still "present")
                (7, 2, 2),  # playlist_tree
                (8, 3, 3),  # playlist_entries
            ],
        ),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=99
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="Set A"
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=8,
            next_page=99,
            row_blobs=rows,
        ),
    }
    pdb_path = tmp_path / "gap.pdb"
    _write_pdb(pdb_path, len_page, pages)

    lib = parse_export_pdb(pdb_path, tmp_path)

    assert len(lib.playlists) == 1
    assert lib.playlists[0].track_rb_ids == [10, 20]


# ---------------------------------------------------------------------------
# Page-chain traversal
# ---------------------------------------------------------------------------


def test_page_chain_follows_next_page_not_index_range(tmp_path: Path) -> None:
    """Table pages are a linked list; first+1 may belong to another table.

    Real exports leave gaps (and other tables) between a table's first and last
    page indices. Walking [first, last] as a range would mis-parse foreign pages.
    """
    len_page = 512
    # playlist_entries: page 3 → page 5 (page 4 is intentionally something else)
    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[
                (0, 1, 1),
                (7, 2, 2),
                (8, 3, 5),  # first=3, last=5, but chain is 3→5 (skip 4)
            ],
        ),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=99
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="Chain"
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=8,
            next_page=5,  # jump over 4
            row_blobs=[_playlist_entry_row(0, 100, 1)],
        ),
        4: _build_data_page(
            len_page=len_page,
            page_index=4,
            page_type=1,  # genres — must NOT be read as playlist entries
            next_page=99,
            row_blobs=[_genre_row(1, "House")],
        ),
        5: _build_data_page(
            len_page=len_page,
            page_index=5,
            page_type=8,
            next_page=99,
            row_blobs=[_playlist_entry_row(1, 200, 1)],
        ),
    }
    pdb_path = tmp_path / "chain.pdb"
    _write_pdb(pdb_path, len_page, pages)

    lib = parse_export_pdb(pdb_path, tmp_path)

    assert lib.playlists[0].track_rb_ids == [100, 200]


# ---------------------------------------------------------------------------
# Gate G1a — header insanity
# ---------------------------------------------------------------------------


def test_g1a_len_page_not_power_of_two_raises(tmp_path: Path) -> None:
    """Non-power-of-two page size cannot address pages; must fail loud (exit 2)."""
    len_page = 512
    # Build a structurally OK file then patch len_page to 3000.
    pages = {
        0: _file_header(len_page=len_page, tables=[(0, 1, 1), (7, 1, 1), (8, 1, 1)]),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=2
        ),
    }
    pdb_path = tmp_path / "bad_page.pdb"
    _write_pdb(pdb_path, len_page, pages)
    blob = bytearray(pdb_path.read_bytes())
    struct.pack_into("<I", blob, 4, 3000)  # not a power of two
    pdb_path.write_bytes(blob)

    with pytest.raises(UnsupportedFormatError, match="len_page"):
        parse_export_pdb(pdb_path, tmp_path)


def test_g1a_implausible_num_tables_raises(tmp_path: Path) -> None:
    """Huge num_tables would run the table list off the page; fail loud."""
    len_page = 512
    pages = {
        0: _file_header(len_page=len_page, tables=[(0, 1, 1)]),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=2
        ),
    }
    pdb_path = tmp_path / "bad_n.pdb"
    _write_pdb(pdb_path, len_page, pages)
    blob = bytearray(pdb_path.read_bytes())
    struct.pack_into("<I", blob, 8, 0xFFFF)  # absurd table count
    pdb_path.write_bytes(blob)

    with pytest.raises(UnsupportedFormatError, match="num_tables"):
        parse_export_pdb(pdb_path, tmp_path)


# ---------------------------------------------------------------------------
# Gate G1b — unknown page_type on unconsumed table
# ---------------------------------------------------------------------------


def test_g1b_unknown_page_type_on_unconsumed_table_warns(tmp_path: Path) -> None:
    """Unknown type on a table we never read: warn + continue, do not abort.

    Tables are independently addressed from the header, so skipping one cannot
    shift another. Exiting 2 here would reject perfectly usable sticks.
    """
    len_page = 512
    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[
                (0, 1, 1),  # tracks
                (7, 2, 2),  # playlist_tree
                (8, 3, 3),  # playlist_entries
                (99, 4, 4),  # outside known enum, unconsumed
            ],
        ),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=99
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="OK"
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=8,
            next_page=99,
            row_blobs=[_playlist_entry_row(0, 1, 1)],
        ),
        4: _build_nondata_page(
            len_page=len_page, page_index=4, page_type=99, next_page=99
        ),
    }
    pdb_path = tmp_path / "unknown_type.pdb"
    _write_pdb(pdb_path, len_page, pages)

    lib = parse_export_pdb(pdb_path, tmp_path)

    assert any("99" in w or "page_type" in w.lower() for w in lib.warnings)
    assert len(lib.playlists) == 1
    assert lib.playlists[0].name == "OK"


# ---------------------------------------------------------------------------
# Gate G1a — duplicate page_type in the table list
# ---------------------------------------------------------------------------


def test_g1a_duplicate_page_type_first_wins_with_warning(tmp_path: Path) -> None:
    """Two header entries with the same page_type: first-wins + warning, not silent.

    WHY: accidental last-wins (or undetermined) behaviour would make conversion
    depend on header-order luck. The policy is explicit: keep the first pointer,
    warn about the later one, continue. Fatal is defensible too, but first-wins
    keeps usable sticks alive when the second entry is garbage.
    """
    len_page = 512
    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[
                (0, 1, 1),  # tracks
                (7, 2, 2),  # playlist_tree
                (8, 3, 3),  # playlist_entries — FIRST (track 100)
                (8, 5, 5),  # playlist_entries — DUPLICATE (track 999 must be ignored)
            ],
        ),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=99
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="Dup"
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=8,
            next_page=99,
            row_blobs=[_playlist_entry_row(0, 100, 1)],
        ),
        5: _build_data_page(
            len_page=len_page,
            page_index=5,
            page_type=8,
            next_page=99,
            row_blobs=[_playlist_entry_row(0, 999, 1)],
        ),
    }
    pdb_path = tmp_path / "dup_type.pdb"
    _write_pdb(pdb_path, len_page, pages)

    lib = parse_export_pdb(pdb_path, tmp_path)

    assert lib.playlists[0].track_rb_ids == [100], (
        "first-wins required: second playlist_entries table must be ignored"
    )
    assert any(
        "duplicate" in w.lower() and "page_type=8" in w for w in lib.warnings
    ), f"expected duplicate page_type warning, got {lib.warnings!r}"


# ---------------------------------------------------------------------------
# Gate G1c — required missing / optional-consumed malformed
# ---------------------------------------------------------------------------


def _minimal_required_pages(
    len_page: int,
    *,
    include_artists: bool = False,
    artist_page: bytes | None = None,
) -> dict[int, bytes]:
    """Hand-built required tables (tracks / tree / entries) for G1c fixtures."""
    tables: list[tuple[int, int, int]] = [
        (0, 1, 1),  # tracks
        (7, 2, 2),  # playlist_tree
        (8, 3, 3),  # playlist_entries
    ]
    pages: dict[int, bytes] = {
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=99
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="P"
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=8,
            next_page=99,
            row_blobs=[],
        ),
    }
    if include_artists:
        tables.insert(1, (2, 4, 4))  # artists
        if artist_page is None:
            artist_page = _build_data_page(
                len_page=len_page,
                page_index=4,
                page_type=2,
                next_page=99,
                row_blobs=[_artist_row(7, "Cour T_")],
            )
        pages[4] = artist_page
    pages[0] = _file_header(len_page=len_page, tables=tables)
    return pages


def test_g1c_missing_tracks_table_raises(tmp_path: Path) -> None:
    """tracks is required; absence is fatal, not an empty library."""
    len_page = 512
    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[
                (7, 1, 1),
                (8, 2, 2),
            ],
        ),
        1: _build_data_page(
            len_page=len_page,
            page_index=1,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="X"
                )
            ],
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=8,
            next_page=99,
            row_blobs=[],
        ),
    }
    pdb_path = tmp_path / "no_tracks.pdb"
    _write_pdb(pdb_path, len_page, pages)

    with pytest.raises(UnsupportedFormatError, match="tracks"):
        parse_export_pdb(pdb_path, tmp_path)


@pytest.mark.parametrize(
    "missing_type,match",
    [
        (0, "tracks"),
        (7, "playlist_tree"),
        (8, "playlist_entries"),
    ],
)
def test_g1c_missing_required_table_raises(
    tmp_path: Path, missing_type: int, match: str
) -> None:
    """Each required table is independently fatal when absent.

    WHY: a missing playlist_tree is not "no playlists" — it means the export
    is incomplete. Softening any of these to empty would invent structure.
    """
    len_page = 512
    all_required = [
        (0, 1, 1),
        (7, 2, 2),
        (8, 3, 3),
    ]
    tables = [t for t in all_required if t[0] != missing_type]
    pages = {
        0: _file_header(len_page=len_page, tables=tables),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=99
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="X"
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=8,
            next_page=99,
            row_blobs=[],
        ),
    }
    pdb_path = tmp_path / f"no_{match}.pdb"
    _write_pdb(pdb_path, len_page, pages)

    with pytest.raises(UnsupportedFormatError, match=match):
        parse_export_pdb(pdb_path, tmp_path)


def test_g1c_missing_optional_artists_tolerated_and_counted(tmp_path: Path) -> None:
    """artists is optional-consumed: absence is OK but must be counted.

    WHY: without a warning, "no artist names because the table was never
    listed" is indistinguishable from "FKs resolve to empty" — the plan
    requires absence of optional-consumed tables to be visible, not silent.
    """
    len_page = 512
    pages = _minimal_required_pages(len_page, include_artists=False)
    pdb_path = tmp_path / "no_artists.pdb"
    _write_pdb(pdb_path, len_page, pages)

    lib = parse_export_pdb(pdb_path, tmp_path)

    assert any(
        "artists" in w.lower() and "absent" in w.lower() for w in lib.warnings
    ), f"expected counted absence warning, got {lib.warnings!r}"
    # Must not be fatal — complementary to present-but-unparseable.
    assert len(lib.playlists) == 1


def test_g1c_corrupt_artists_table_byte_patched_raises(tmp_path: Path) -> None:
    """Byte-patch a *valid* artists page so present-but-unparseable is fatal.

    WHY: silently zeroing every artist name is the exact plausible-wrong-output
    failure G1c/N1 exists to block. Building junk from scratch is easy to get
    wrong about layout; patching a known-good page at test time (no committed
    binary) proves the gate fires on real-layout corruption — not a silent
    skip, and not an uncaught stack trace.
    """
    len_page = 512
    pages = _minimal_required_pages(len_page, include_artists=True)
    valid_path = tmp_path / "artists_ok.pdb"
    _write_pdb(valid_path, len_page, pages)

    # Unpatched copy must parse — otherwise the fixture is wrong, not the gate.
    lib_ok = parse_export_pdb(valid_path, tmp_path)
    assert len(lib_ok.playlists) == 1

    # Byte-patch a COPY: keep header + table pointers, replace the artists data
    # page with a present row whose body is garbage (not a silent empty table).
    blob = bytearray(valid_path.read_bytes())
    artists_page_idx = 4
    page_start = artists_page_idx * len_page
    junk = bytearray(len_page)
    junk[0:PAGE_HEADER] = _page_header(
        page_index=artists_page_idx,
        page_type=2,  # ARTISTS
        next_page=99,
        num_row_offsets=1,
        num_rows=1,
        page_flags=0x24,  # data page
    )
    # Present bit set, row at heap offset 0, only 2 garbage payload bytes —
    # forces the artist row parser (or DeviceSQL decode) to fail loud.
    junk[PAGE_HEADER : PAGE_HEADER + 2] = b"\xff\xff"
    _pack_row_index(junk, len_page, [0])
    blob[page_start : page_start + len_page] = junk

    corrupt_path = tmp_path / "corrupt_artists.pdb"
    corrupt_path.write_bytes(bytes(blob))

    with pytest.raises(UnsupportedFormatError, match="artists") as exc_info:
        parse_export_pdb(corrupt_path, tmp_path)

    msg = str(exc_info.value).lower()
    assert "unparseable" in msg or "artists" in msg
    # Fail-loud with a typed error — not a bare Exception / traceback path.
    assert type(exc_info.value) is UnsupportedFormatError


def test_g1c_malformed_artists_table_raises(tmp_path: Path) -> None:
    """artists is optional-but-consumed: present-but-unparseable is fatal.

    Kept as a direct (non-patch) construction for clarity; the byte-patched
    fixture above is the plan's required pinning form.
    """
    len_page = 512
    junk_page = bytearray(len_page)
    junk_page[0:PAGE_HEADER] = _page_header(
        page_index=4,
        page_type=2,
        next_page=99,
        num_row_offsets=1,
        num_rows=1,
        page_flags=0x24,
    )
    junk_page[PAGE_HEADER : PAGE_HEADER + 2] = b"\xff\xff"
    _pack_row_index(junk_page, len_page, [0])

    pages = _minimal_required_pages(
        len_page, include_artists=True, artist_page=bytes(junk_page)
    )
    pdb_path = tmp_path / "bad_artists.pdb"
    _write_pdb(pdb_path, len_page, pages)

    with pytest.raises(UnsupportedFormatError, match="artists"):
        parse_export_pdb(pdb_path, tmp_path)


# ---------------------------------------------------------------------------
# Track field mapping + path resolution
# ---------------------------------------------------------------------------


def test_track_row_maps_fields_and_resolves_path(tmp_path: Path) -> None:
    """BPM is tempo/100; path re-roots via Contents/; missing FKs become ''."""
    len_page = 1024
    drive = tmp_path / "stick"
    audio = drive / "Contents" / "Artist" / "Album" / "Track.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake")

    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[
                (0, 1, 2),  # tracks: nondata 1 → data 2
                (1, 3, 3),  # genres
                (2, 4, 4),  # artists
                (3, 5, 5),  # albums
                (5, 6, 6),  # keys
                (7, 7, 7),  # playlist_tree
                (8, 8, 8),  # playlist_entries
            ],
        ),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=2
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=0,
            next_page=99,
            row_blobs=[
                _track_row(
                    tid=42,
                    title="Labirinto",
                    file_path="/Contents/Artist/Album/Track.m4a",
                    artist_id=7,
                    album_id=3,
                    genre_id=1,
                    key_id=2,
                    sample_rate=44100,
                    tempo=12500,
                    duration=264,
                    comment="Key: F Minor",
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=1,
            next_page=99,
            row_blobs=[_genre_row(1, "House")],
        ),
        4: _build_data_page(
            len_page=len_page,
            page_index=4,
            page_type=2,
            next_page=99,
            row_blobs=[_artist_row(7, "Cour T_")],
        ),
        5: _build_data_page(
            len_page=len_page,
            page_index=5,
            page_type=3,
            next_page=99,
            row_blobs=[_album_row(3, 7, "All Stars 07")],
        ),
        6: _build_data_page(
            len_page=len_page,
            page_index=6,
            page_type=5,
            next_page=99,
            row_blobs=[_key_row(2, "4A")],
        ),
        7: _build_data_page(
            len_page=len_page,
            page_index=7,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="Root PL"
                )
            ],
        ),
        8: _build_data_page(
            len_page=len_page,
            page_index=8,
            page_type=8,
            next_page=99,
            row_blobs=[_playlist_entry_row(0, 42, 1)],
        ),
    }
    pdb_path = tmp_path / "track.pdb"
    _write_pdb(pdb_path, len_page, pages)

    lib = parse_export_pdb(pdb_path, drive)

    assert 42 in lib.tracks
    t = lib.tracks[42]
    assert t.title == "Labirinto"
    assert t.artist == "Cour T_"
    assert t.album == "All Stars 07"
    assert t.genre == "House"
    assert t.key_name == "4A"
    assert t.bpm == 125.0
    assert t.sample_rate == 44100
    assert t.duration_s == 264
    assert t.comment == "Key: F Minor"
    assert t.raw_path == "/Contents/Artist/Album/Track.m4a"
    assert t.resolved_path == audio
    assert t.beatgrid is None
    assert t.cues == []
    assert lib.playlists[0].track_rb_ids == [42]


def test_unresolvable_path_yields_none_and_warning(tmp_path: Path) -> None:
    """Missing audio must not crash; resolved_path is None + a warning."""
    len_page = 1024
    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[(0, 1, 1), (7, 2, 2), (8, 3, 3)],
        ),
        1: _build_data_page(
            len_page=len_page,
            page_index=1,
            page_type=0,
            next_page=99,
            row_blobs=[
                _track_row(
                    tid=1,
                    title="Ghost",
                    file_path="/Contents/No/Such/File.mp3",
                )
            ],
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=True, name="F"
                )
            ],
        ),
        3: _build_data_page(
            len_page=len_page,
            page_index=3,
            page_type=8,
            next_page=99,
            row_blobs=[],
        ),
    }
    pdb_path = tmp_path / "missing.pdb"
    _write_pdb(pdb_path, len_page, pages)

    lib = parse_export_pdb(pdb_path, tmp_path)

    assert lib.tracks[1].resolved_path is None
    assert any("path" in w.lower() or "resolve" in w.lower() for w in lib.warnings)


# ---------------------------------------------------------------------------
# Real stick (Tier B) — skipped in CI when unmounted
# ---------------------------------------------------------------------------


@pytest.mark.real_stick
@pytest.mark.skipif(
    not REAL_PDB.is_file(),
    reason="real stick not mounted at /Volumes/USB DISK",
)
def test_real_export_pdb_scale_and_sanity(tmp_path: Path) -> None:
    """Tier B: thousands of tracks, plausible BPM/rate, real path, playlists.

    Copies export.pdb first — the stick is read-only reference data.
    """
    copy = tmp_path / "export.pdb"
    copy.write_bytes(REAL_PDB.read_bytes())

    t0 = time.perf_counter()
    lib = parse_export_pdb(copy, REAL_STICK)
    elapsed = time.perf_counter() - t0

    assert len(lib.tracks) >= 3000, f"expected thousands of tracks, got {len(lib.tracks)}"
    # Self-evident field sanity — wrong offsets show up immediately here.
    bpms = [t.bpm for t in lib.tracks.values() if t.bpm > 0]
    assert bpms and min(bpms) >= 60 and max(bpms) <= 200
    rates = {t.sample_rate for t in lib.tracks.values()}
    assert rates <= {44100, 48000, 88200, 96000}
    assert rates & {44100, 48000}

    # Sample a resolved path that must exist on the stick.
    with_path = [t for t in lib.tracks.values() if t.resolved_path is not None]
    assert with_path, "no track resolved under drive_root"
    sample = with_path[0]
    assert sample.resolved_path is not None and sample.resolved_path.is_file()
    assert sample.raw_path.startswith("/") or "Contents" in sample.raw_path

    named = [p for p in lib.playlists if p.name and not p.is_folder]
    assert named, "expected named playlists"
    with_members = [p for p in named if p.track_rb_ids]
    assert with_members, "expected playlists with membership"

    # Leave timing breadcrumb for the worker report (not a hard gate).
    assert elapsed < 120.0, f"parse took too long: {elapsed:.1f}s"
    # Attach for -s visibility
    print(
        f"\nreal_stick: tracks={len(lib.tracks)} playlists={len(lib.playlists)} "
        f"elapsed={elapsed:.3f}s sample={sample.title!r} path={sample.resolved_path}"
    )


# ---------------------------------------------------------------------------
# G1d — torn/mid-export page images
#
# A real conversion published two playlist entries that the settled export.pdb
# does not contain (tracks 444 and 2984, 2026-07-29). The file was unchanged
# between that convert and the verify that caught it, so the difference arose
# at read time: convert started 14 s after rekordbox's last write to the pdb.
# A page whose present-bit set disagrees with its declared num_rows is the
# signature that resurrects a stale row, and the reader decoded num_rows and
# then discarded it. These tests pin the gate.
# ---------------------------------------------------------------------------


def _resurrect_slot(
    page: bytes, len_page: int, slot: int, heap_offset: int
) -> bytes:
    """Set slot's present bit and point it at a row body, leaving num_rows alone.

    This is what a torn page image looks like to the walker: one more row
    reachable than the page header says it holds.
    """
    buf = bytearray(page)
    g, r = divmod(slot, 16)
    group_base = len_page - (g * 0x24)
    present = struct.unpack_from("<H", buf, group_base - 4)[0]
    present |= 1 << r
    struct.pack_into("<H", buf, group_base - 4, present)
    struct.pack_into("<H", buf, group_base - (6 + 2 * r), heap_offset)
    return bytes(buf)


def _torn_pdb(tmp_path: Path, entry_page: bytes, len_page: int) -> Path:
    pages = {
        0: _file_header(
            len_page=len_page,
            tables=[(0, 1, 1), (7, 2, 2), (8, 3, 3)],
        ),
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=99
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0, sort_order=0, pl_id=1, is_folder=False, name="Set A"
                )
            ],
        ),
        3: entry_page,
    }
    path = tmp_path / "torn.pdb"
    _write_pdb(path, len_page, pages)
    return path


def test_g1d_resurrected_row_beyond_num_rows_raises(tmp_path: Path) -> None:
    """A present bit the page header does not account for must be refused.

    WHY: this is the observed failure. The stale entry parses cleanly and lands
    in a coherent chain, so nothing downstream can tell it from a real one —
    the conversion exits 0 and publishes a library with a track the DJ removed.
    Refusing the parse is the only point where the two disagree.
    """
    len_page = 512
    rows: list[bytes | None] = [
        _playlist_entry_row(0, track_id=10, playlist_id=1),
        None,  # deleted slot — its bytes are still in the heap
        _playlist_entry_row(1, track_id=20, playlist_id=1),
    ]
    page = _build_data_page(
        len_page=len_page, page_index=3, page_type=8, next_page=99, row_blobs=rows
    )
    # Resurrect slot 1, pointing it at the first row body. num_rows still says 2.
    torn = _resurrect_slot(page, len_page, slot=1, heap_offset=0)
    path = _torn_pdb(tmp_path, torn, len_page)

    with pytest.raises(UnsupportedFormatError) as exc:
        parse_export_pdb(path, tmp_path)
    assert "num_rows" in str(exc.value) or "present" in str(exc.value)


def test_g1d_row_offset_inside_row_index_raises(tmp_path: Path) -> None:
    """A row offset pointing into the backward-growing index is not a row.

    WHY: across 997 pages of a real 3,673-track export every present row body
    sits between the page header and the index; nothing legitimate points into
    the index area. The reader only bounded offsets by the page size, so stale
    index bytes could be parsed as a row.
    """
    len_page = 512
    rows: list[bytes | None] = [
        _playlist_entry_row(0, track_id=10, playlist_id=1),
        _playlist_entry_row(1, track_id=20, playlist_id=1),
    ]
    page = _build_data_page(
        len_page=len_page, page_index=3, page_type=8, next_page=99, row_blobs=rows
    )
    buf = bytearray(page)
    # Point slot 0 into the row-index region (heap-relative → absolute ≥ heap end).
    struct.pack_into("<H", buf, len_page - 6, len_page - PAGE_HEADER)
    path = _torn_pdb(tmp_path, bytes(buf), len_page)

    with pytest.raises(UnsupportedFormatError) as exc:
        parse_export_pdb(path, tmp_path)
    assert "row offset" in str(exc.value).lower()


def test_g1d_well_formed_page_with_deleted_rows_still_parses(tmp_path: Path) -> None:
    """The gate must not fire on ordinary deleted slots.

    WHY: tombstones are normal — a real export carried 104 of them. A gate that
    rejected those would refuse every library the tool exists to convert.
    """
    len_page = 512
    rows: list[bytes | None] = [
        _playlist_entry_row(0, track_id=10, playlist_id=1),
        None,
        _playlist_entry_row(1, track_id=20, playlist_id=1),
    ]
    page = _build_data_page(
        len_page=len_page, page_index=3, page_type=8, next_page=99, row_blobs=rows
    )
    path = _torn_pdb(tmp_path, page, len_page)

    lib = parse_export_pdb(path, tmp_path)

    assert lib.playlists[0].track_rb_ids == [10, 20]


def _partial_index_page(*, last_row: int, used_end: int) -> bytes:
    """129 slots in 4096 bytes: the final group has one offset, not sixteen.

    Eight complete groups occupy 288 bytes. The last offset and its flags
    occupy six, placing the compact index at 3802; a padded group starts at
    3772. These are hand-authored layout boundaries, not parser output.
    """
    page = bytearray(4096)
    page[:PAGE_HEADER] = _page_header(
        page_index=3,
        page_type=8,
        next_page=99,
        num_row_offsets=129,
        num_rows=129,
        used_size=used_end - PAGE_HEADER,
    )
    offsets: list[int | None] = [index * 12 for index in range(128)]
    offsets.append(last_row - PAGE_HEADER)
    _pack_row_index(page, 4096, offsets)
    return bytes(page)


def test_compact_final_index_group_preserves_lexicon_last_row() -> None:
    """Do not reject Lexicon's final row just because padded offsets overlap it."""
    page = _partial_index_page(last_row=3772, used_end=3802)
    bases = _iter_present_row_bases(page, 4096, 129, 129, 3)
    assert bases == [PAGE_HEADER + index * 12 for index in range(128)] + [3772]


def test_padded_final_group_still_accepts_original_layout() -> None:
    """A regular padded group keeps the same row boundary and order."""
    page = _partial_index_page(last_row=3760, used_end=3772)
    bases = _iter_present_row_bases(page, 4096, 129, 129, 3)
    assert bases == [PAGE_HEADER + index * 12 for index in range(128)] + [3760]


@pytest.mark.parametrize("used_end", [PAGE_HEADER, 3772])
def test_padding_is_not_reclassified_as_heap_without_used_size(used_end: int) -> None:
    """A damaged row offset in padded space must not become valid accidentally."""
    page = _partial_index_page(last_row=3772, used_end=used_end)
    with pytest.raises(UnsupportedFormatError, match="row offset"):
        _iter_present_row_bases(page, 4096, 129, 129, 3)


def test_compact_group_rejects_offset_past_declared_used_heap() -> None:
    """Only the documented used heap can reclaim a partial group's padding."""
    page = _partial_index_page(last_row=3790, used_end=3790)
    with pytest.raises(UnsupportedFormatError, match="row offset"):
        _iter_present_row_bases(page, 4096, 129, 129, 3)


def test_compact_group_rejects_used_heap_overlapping_allocated_index() -> None:
    page = _partial_index_page(last_row=3772, used_end=3803)
    with pytest.raises(UnsupportedFormatError, match="overlapping the row index"):
        _iter_present_row_bases(page, 4096, 129, 129, 3)


def test_compact_group_rejects_offset_inside_allocated_index() -> None:
    page = _partial_index_page(last_row=3802, used_end=3802)
    with pytest.raises(UnsupportedFormatError, match="row offset"):
        _iter_present_row_bases(page, 4096, 129, 129, 3)


def test_partial_group_rejects_present_flag_without_allocated_offset() -> None:
    """An out-of-range flag cannot silently resurrect an unallocated row."""
    page = bytearray(_partial_index_page(last_row=3772, used_end=3802))
    struct.pack_into("<H", page, 4096 - 8 * 36 - 4, 0b11)
    with pytest.raises(UnsupportedFormatError, match="allocated row offsets"):
        _iter_present_row_bases(bytes(page), 4096, 129, 129, 3)
