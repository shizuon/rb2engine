"""ANLZ reader: allowlist tag walk, beatgrids, cues/loops, PSSI survival.

WHY (not just what):
- pyrekordbox AnlzFile.parse_file aborts the whole file when a known tag fails
  (issue #199, PSSI version=1). Mid-set, that loses every cue/grid on the track.
  The allowlist walk is the structural fix — these tests pin it.
- end_sample is the U3 loop hinge: wrong loop-out detection steals pads.
- ms→samples must happen exactly once at this boundary; wrong rate = wrong pads.
- Real-stick tests guard against synthetic-only false confidence.
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
from pathlib import Path

import pytest

from rb2engine.errors import TrackSkipped
from rb2engine.ir import RGB, CueKind
from rb2engine.reader.anlz import ALLOWLIST, read_anlz, tag_index
from rb2engine.units import ms_to_samples

# ---------------------------------------------------------------------------
# Synthetic ANLZ builders (hand-authored layouts; not from our parser)
# ---------------------------------------------------------------------------


def _U32(n: int) -> bytes:
    return struct.pack(">I", n & 0xFFFFFFFF)


def _U16(n: int) -> bytes:
    return struct.pack(">H", n & 0xFFFF)


def _I32(n: int) -> bytes:
    return struct.pack(">i", n)


def _pmai(tags: list[bytes], len_header: int = 28) -> bytes:
    body = b"".join(tags)
    len_file = len_header + len(body)
    header = b"PMAI" + _U32(len_header) + _U32(len_file) + b"\x00" * (len_header - 12)
    return header + body


def _tag(fourcc: str, len_header: int, content: bytes) -> bytes:
    """Build one tag envelope: fourcc + len_header + len_tag + content."""
    len_tag = 12 + len(content)
    return fourcc.encode("ascii") + _U32(len_header) + _U32(len_tag) + content


def _pqtz(entries: list[tuple[int, int, int]]) -> bytes:
    """entries: (beat_in_bar 1–4, tempo centi-BPM, time_ms)."""
    content = (
        b"\x00" * 4
        + _U32(0x80000)
        + _U32(len(entries))
        + b"".join(_U16(b) + _U16(tempo) + _U32(t) for b, tempo, t in entries)
    )
    return _tag("PQTZ", 24, content)


def _pqt2_minimal(entry_count: int = 0) -> bytes:
    """Minimal valid PQT2 header (two bpm anchors, optional empty entries)."""
    # After common 12: pad4, const 0x01000002, pad4, 2× AnlzQuantizeTick, entry_count, u3,u4,u5
    tick = _U16(1) + _U16(12800) + _U32(0)
    tick2 = _U16(4) + _U16(12800) + _U32(60_000)
    content = (
        b"\x00" * 4
        + _U32(0x01000002)
        + b"\x00" * 4
        + tick
        + tick2
        + _U32(entry_count)
        + _U32(0)
        + _U32(0)
        + _U32(0)
        + b"\x00" * (2 * entry_count)
    )
    return _tag("PQT2", 56, content)


def _pcpt(
    hot_cue: int,
    time_ms: int,
    *,
    cue_type: int = 1,
    loop_time: int = 0xFFFFFFFF,
    status: int = 0,
    u1: int = 0x10000,
) -> bytes:
    """One PCOB entry (56 bytes)."""
    return (
        b"PCPT"
        + _U32(0x1C)
        + _U32(0x38)
        + _U32(hot_cue)
        + _U32(status)
        + _U32(u1)
        + _U16(0xFFFF)
        + _U16(0xFFFF)
        + bytes([cue_type, 0])
        + _U16(1000)
        + _U32(time_ms)
        + _U32(loop_time)
        + b"\x00" * 16
    )


def _pcob(list_type: int, entries: list[bytes]) -> bytes:
    content = _U32(list_type) + _U16(0) + _U16(len(entries)) + _I32(-1) + b"".join(entries)
    return _tag("PCOB", 24, content)


def _pcp2(
    hot_cue: int,
    time_ms: int,
    *,
    cue_type: int = 1,
    loop_time: int = 0xFFFFFFFF,
    comment: str = "",
    rgb: tuple[int, int, int] = (0, 0, 0),
    color_id: int = 0,
    include_color: bool = True,
) -> bytes:
    """One PCO2 entry. Variable length; colors optional (truncated real-world form)."""
    comment_bytes = b""
    if comment:
        comment_bytes = comment.encode("utf-16-be") + b"\x00\x00"
    # Fixed fields through len_comment start at offset 40 within entry (after magic/lh/le)
    # layout from entry start:
    # 0: PCP2, 4: lh=16, 8: le, 12: hot, 16: type+pad3, 20: time, 24: loop,
    # 28: color_id + 7 pad, 36: loop num/den, 40: len_comment, 44: comment, then colors
    mid = (
        _U32(hot_cue)
        + bytes([cue_type, 0, 0x03, 0xE8])
        + _U32(time_ms)
        + _U32(loop_time)
        + bytes([color_id])
        + b"\x00" * 7
        + _U16(0)
        + _U16(0)
        + _U32(len(comment_bytes))
        + comment_bytes
    )
    if include_color:
        mid += bytes([0, rgb[0], rgb[1], rgb[2]]) + b"\x00" * 16
    # entry = magic(4) + lh(4) + le(4) + mid; le = total entry size
    # mid starts after 12-byte mini-header
    len_entry = 12 + len(mid)
    return b"PCP2" + _U32(0x10) + _U32(len_entry) + mid


def _pco2(list_type: int, entries: list[bytes]) -> bytes:
    content = _U32(list_type) + _U16(len(entries)) + _U16(0) + b"".join(entries)
    return _tag("PCO2", 20, content)


def _ppth(path: str) -> bytes:
    path_bytes = path.encode("utf-16-be") + b"\x00\x00"
    content = _U32(len(path_bytes)) + path_bytes
    return _tag("PPTH", 16, content)


def _pssi_version1() -> bytes:
    """PSSI-shaped tag with version=1 layout that breaks pyrekordbox Const(24).

    Upstream issue #199: hard-coded len_entry_bytes==24 fails when version=1.
    We emit a non-allowlisted tag so the allowlist walk never feeds it to a parser.
    """
    # After common 12: version u16=1, entry_size u16=something-not-forcing-const-path,
    # then junk. len_header claimed 32 like real PSSI.
    content = _U16(1) + _U16(20) + b"\x00" * 100
    return _tag("PSSI", 32, content)


def _unknown(fourcc: str = "ZZZZ", size: int = 40) -> bytes:
    content = b"\x00" * (size - 12)
    return _tag(fourcc, 12, content)


def _pwv6_stub() -> bytes:
    """Minimal 2EX-style waveform tag body (never parsed by allowlist)."""
    content = _U32(3) + _U32(0)  # len_entry_bytes, len_entries
    return _tag("PWV6", 20, content)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Structural: allowlist, unknown tags, PSSI survival
# ---------------------------------------------------------------------------


def test_allowlist_is_exactly_the_five_consumed_fourccs() -> None:
    """G2: only these five are parsed; everything else is structural immunization."""
    assert frozenset({"PQTZ", "PQT2", "PCOB", "PCO2", "PPTH"}) == ALLOWLIST


def test_pssi_version1_does_not_kill_file(tmp_path: Path) -> None:
    """Regression for pyrekordbox #199: version=1 PSSI must not abort the track.

    WHY: whole-file parse raises ConstError and drops the beatgrid we need mid-set.
    """
    beats = [(1, 12800, 0), (2, 12800, 469), (3, 12800, 938), (4, 12800, 1406)]
    data = _pmai([_ppth("/Contents/x.mp3"), _pqtz(beats), _pssi_version1()])
    path = _write(tmp_path / "ANLZ0000.DAT", data)

    grid, cues, warnings = read_anlz(path, None, 44100)

    assert grid is not None
    assert len(grid.beats) == 4
    assert grid.is_adjusted is False
    assert cues == []
    # PSSI must be skipped unparsed (allowlist); warn as unknown_tag if reported
    assert all("TrackSkipped" not in w for w in warnings)
    assert any(w.startswith("unknown_tag:PSSI") for w in warnings)


def test_unknown_fourcc_is_tolerated(tmp_path: Path) -> None:
    """Unknown tags (not in allowlist) must not raise — warn/count only."""
    data = _pmai([_unknown("ZZZZ"), _pqtz([(1, 12000, 0)])])
    path = _write(tmp_path / "x.DAT", data)

    grid, _cues, warnings = read_anlz(path, None, 44100)
    assert grid is not None
    assert len(grid.beats) == 1
    assert any("ZZZZ" in w or "unknown_tag" in w for w in warnings)


def test_never_calls_parse_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard rule: AnlzFile.parse_file is unsafe; our walk must not call it."""
    import pyrekordbox.anlz.file as anlz_file

    def boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("AnlzFile.parse_file must not be called")

    monkeypatch.setattr(anlz_file.AnlzFile, "parse_file", classmethod(boom))
    monkeypatch.setattr(anlz_file.AnlzFile, "parse", classmethod(boom))

    data = _pmai([_pqtz([(1, 12800, 1000)])])
    path = _write(tmp_path / "x.DAT", data)
    grid, _, _ = read_anlz(path, None, 44100)
    assert grid is not None


def test_2ex_tag_index_counted_not_parsed(tmp_path: Path) -> None:
    """`.2EX` files exist in bulk; we may index fourccs but must not parse bodies."""
    data = _pmai([_ppth("/x.mp3"), _pwv6_stub(), _unknown("PWV7", 36), _unknown("PWVC", 32)])
    path = _write(tmp_path / "ANLZ0000.2EX", data)

    index = tag_index(path)
    fourccs = [f for f, _ in index]
    assert "PWV6" in fourccs
    assert "PWV7" in fourccs
    assert "PWVC" in fourccs
    # read_anlz on a .2EX path should not try to parse waveform content
    grid, cues, warnings = read_anlz(None, path, 44100)
    assert grid is None
    assert cues == []
    assert any("2ex" in w.lower() or "PWV6" in w for w in warnings)


# ---------------------------------------------------------------------------
# Beatgrid
# ---------------------------------------------------------------------------


def test_pqtz_beats_converted_to_samples_at_given_rate(tmp_path: Path) -> None:
    """ms→samples once, at the reader boundary, with the caller's sample_rate.

    WHY: guessing 44100 when the track is 48000 shifts every cue by ~9%.
    """
    # 1000 ms @ 48000 = 48000 samples exactly
    entries = [
        (1, 12800, 0),
        (2, 12800, 469),
        (3, 12800, 938),
        (4, 12800, 1000),
    ]
    path = _write(tmp_path / "x.DAT", _pmai([_pqtz(entries)]))
    grid, _, _ = read_anlz(path, None, 48000)

    assert grid is not None
    assert grid.is_adjusted is False
    assert [b.beat_in_bar for b in grid.beats] == [1, 2, 3, 4]
    assert grid.beats[0].sample_offset == 0
    assert grid.beats[3].sample_offset == ms_to_samples(1000.0, 48000)
    assert grid.beats[3].bpm == 128.0  # tempo/100


def test_pqt2_presence_sets_is_adjusted(tmp_path: Path) -> None:
    """Keep the legacy presence flag; this does not prove a manual grid edit."""
    dat = _write(tmp_path / "x.DAT", _pmai([_pqtz([(1, 12000, 0), (2, 12000, 500)])]))
    ext = _write(tmp_path / "x.EXT", _pmai([_pqt2_minimal(0)]))

    grid, _, _ = read_anlz(dat, ext, 44100)
    assert grid is not None
    assert grid.is_adjusted is True
    assert len(grid.beats) == 2  # positions still from PQTZ


@pytest.mark.parametrize("problem", ["variant_2", "variant_0", "missing_entries"])
def test_optional_pqt2_failure_preserves_grid_and_later_cues(
    tmp_path: Path, problem: str
) -> None:
    """An unsupported optional body must not erase valid neighboring tags."""
    dat_bytes = _pmai([_pqtz([(3, 12000, 100), (4, 12000, 600), (1, 12000, 1100)])])
    pqt2 = bytearray(_pqt2_minimal())
    if problem.startswith("variant"):
        struct.pack_into(">I", pqt2, 16, 0x02000002 if problem == "variant_2" else 2)
    else:
        struct.pack_into(">I", pqt2, 40, 2)
    ext_bytes = _pmai([bytes(pqt2), _pco2(1, [_pcp2(1, 1100, comment="Downbeat")])])
    dat = _write(tmp_path / "x.DAT", dat_bytes)
    ext = _write(tmp_path / "x.EXT", ext_bytes)

    grid, cues, warnings = read_anlz(dat, ext, 48000)

    assert grid is not None
    assert [beat.sample_offset for beat in grid.beats] == [4800, 28800, 52800]
    assert [beat.beat_in_bar for beat in grid.beats] == [3, 4, 1]
    assert len(cues) == 1
    assert cues[0].start_sample == 52800
    assert cues[0].name == "Downbeat"
    assert any(warning.startswith("pqt2_ignored:x.EXT:") for warning in warnings)
    assert dat.read_bytes() == dat_bytes
    assert ext.read_bytes() == ext_bytes


def test_pqt2_broken_envelope_is_not_silently_skipped(tmp_path: Path) -> None:
    """A tag that overlaps the following cue cannot be recovered by guessing."""
    pqt2 = bytearray(_pqt2_minimal())
    struct.pack_into(">I", pqt2, 8, 10000)
    ext = _write(tmp_path / "x.EXT", _pmai([bytes(pqt2), _pco2(1, [_pcp2(1, 1100)])]))
    with pytest.raises(TrackSkipped, match="PQT2:truncated"):
        read_anlz(None, ext, 44100)


def test_no_grid_returns_none(tmp_path: Path) -> None:
    path = _write(tmp_path / "x.DAT", _pmai([_ppth("/a.mp3")]))
    grid, cues, _ = read_anlz(path, None, 44100)
    assert grid is None
    assert cues == []


# ---------------------------------------------------------------------------
# Cues and loops
# ---------------------------------------------------------------------------


def test_pcob_hot_and_memory_kinds(tmp_path: Path) -> None:
    """list_type 1 → HOT (slot from hot_cue); list_type 0 → MEMORY."""
    hot = _pcob(1, [_pcpt(3, 5000)])  # pad C
    mem = _pcob(0, [_pcpt(0, 1000)])
    path = _write(tmp_path / "x.DAT", _pmai([hot, mem]))

    _, cues, _ = read_anlz(path, None, 44100)
    assert len(cues) == 2
    hot_cues = [c for c in cues if c.kind is CueKind.HOT]
    mem_cues = [c for c in cues if c.kind is CueKind.MEMORY]
    assert len(hot_cues) == 1 and hot_cues[0].hot_slot == 3
    assert hot_cues[0].start_sample == ms_to_samples(5000.0, 44100)
    assert hot_cues[0].end_sample is None
    assert len(mem_cues) == 1 and mem_cues[0].hot_slot is None


def test_loop_out_sets_end_sample_and_is_loop(tmp_path: Path) -> None:
    """U3 hinge: loop-out present → end_sample set → is_loop True.

    WHY: mapper routes on is_loop only; a missed loop steals a pad at the gig.
    """
    loop_entry = _pcpt(1, 2000, cue_type=2, loop_time=6000)
    path = _write(tmp_path / "x.DAT", _pmai([_pcob(1, [loop_entry])]))

    _, cues, _ = read_anlz(path, None, 44100)
    assert len(cues) == 1
    c = cues[0]
    assert c.is_loop is True
    assert c.start_sample == ms_to_samples(2000.0, 44100)
    assert c.end_sample == ms_to_samples(6000.0, 44100)


def test_unset_loop_time_is_not_a_loop(tmp_path: Path) -> None:
    """0xFFFFFFFF sentinel must not become end_sample=huge."""
    path = _write(tmp_path / "x.DAT", _pmai([_pcob(1, [_pcpt(1, 100)])]))
    _, cues, _ = read_anlz(path, None, 44100)
    assert cues[0].end_sample is None
    assert cues[0].is_loop is False


def test_pco2_preferred_over_pcob_for_color_and_name(tmp_path: Path) -> None:
    """PCO2 carries RGB + comment; when present it wins over PCOB palette ids."""
    dat = _write(
        tmp_path / "x.DAT",
        _pmai([_pcob(1, [_pcpt(1, 1000)])]),
    )
    ext = _write(
        tmp_path / "x.EXT",
        _pmai(
            [
                _pco2(
                    1,
                    [
                        _pcp2(
                            1,
                            1000,
                            comment="Intro",
                            rgb=(0x4D, 0x00, 0xFF),
                        )
                    ],
                )
            ]
        ),
    )

    _, cues, _ = read_anlz(dat, ext, 44100)
    assert len(cues) == 1
    c = cues[0]
    assert c.kind is CueKind.HOT
    assert c.name == "Intro"
    assert c.color == RGB(0x4D, 0x00, 0xFF)
    assert c.start_sample == ms_to_samples(1000.0, 44100)


def test_pco2_loop_sets_end_sample(tmp_path: Path) -> None:
    ext = _write(
        tmp_path / "x.EXT",
        _pmai([_pco2(1, [_pcp2(2, 4000, cue_type=2, loop_time=8000, comment="Loop A")])]),
    )
    _, cues, _ = read_anlz(None, ext, 48000)
    assert len(cues) == 1
    assert cues[0].is_loop is True
    assert cues[0].end_sample == ms_to_samples(8000.0, 48000)
    assert cues[0].name == "Loop A"


def test_malformed_consumed_tag_raises_track_skipped(tmp_path: Path) -> None:
    """G2b: a consumed tag we cannot parse → TrackSkipped with stable reason_code.

    WHY: silent defaults would invent beat positions; better skip the track.
    """
    # Truncated PQTZ: claims 10 entries but provides none
    bad_content = b"\x00" * 4 + _U32(0x80000) + _U32(10)
    bad = _tag("PQTZ", 24, bad_content)
    path = _write(tmp_path / "x.DAT", _pmai([bad]))

    with pytest.raises(TrackSkipped) as ei:
        read_anlz(path, None, 44100)

    msg = str(ei.value)
    assert "anlz_" in msg  # machine-stable reason prefix
    reason = getattr(ei.value, "reason_code", None) or msg.split(":", maxsplit=1)[0]
    assert reason.startswith("anlz_")


def test_pcob_with_nonzero_status_still_parses(tmp_path: Path) -> None:
    """Real sticks set status/u1 outside pyrekordbox Const assumptions — we must not skip."""
    entry = _pcpt(7, 1920, status=1, u1=0)
    path = _write(tmp_path / "x.DAT", _pmai([_pcob(1, [entry])]))
    _, cues, _ = read_anlz(path, None, 44100)
    assert len(cues) == 1
    assert cues[0].hot_slot == 7
    assert cues[0].start_sample == ms_to_samples(1920.0, 44100)


def test_both_paths_none_returns_empty() -> None:
    grid, cues, warnings = read_anlz(None, None, 44100)
    assert grid is None
    assert cues == []
    assert warnings == []


# ---------------------------------------------------------------------------
# TAGS registry pin (semi-public API coupling)
# ---------------------------------------------------------------------------


def test_pyrekordbox_tags_registry_has_allowlist() -> None:
    """C-MINOR-4: pin the semi-public registry so upstream renames fail in CI."""
    from pyrekordbox.anlz.tags import TAGS

    for fourcc in ALLOWLIST:
        assert fourcc in TAGS, f"missing {fourcc} in pyrekordbox.anlz.tags.TAGS"


# ---------------------------------------------------------------------------
# Real stick (skipped when absent / RB2ENGINE_REAL_STICK unset and path missing)
# ---------------------------------------------------------------------------

_STICK = Path(os.environ.get("RB2ENGINE_REAL_STICK", "/Volumes/USB DISK"))
_USBANLZ = _STICK / "PIONEER" / "USBANLZ"


def _stick_available() -> bool:
    return _USBANLZ.is_dir()


@pytest.mark.real_stick
@pytest.mark.skipif(not _stick_available(), reason="real rekordbox stick not mounted")
def test_real_stick_sample_grids_and_cues() -> None:
    """Sample real .DAT/.EXT pairs: monotonic grids, beat cycle, loops → is_loop.

    Copies off-stick before parsing (stick is read-only reference).
    """
    pairs: list[tuple[Path, Path]] = []
    for dat in sorted(_USBANLZ.rglob("ANLZ0000.DAT"))[:25]:
        ext = dat.with_suffix(".EXT")
        if ext.is_file():
            pairs.append((dat, ext))
    assert pairs, "expected DAT/EXT pairs on stick"

    parsed = 0
    pco2_tracks = 0
    sample_rate = 44100

    with tempfile.TemporaryDirectory(prefix="rb2engine_anlz_") as tmp:
        tmp_path = Path(tmp)
        for i, (dat_src, ext_src) in enumerate(pairs):
            dat = tmp_path / f"{i}.DAT"
            ext = tmp_path / f"{i}.EXT"
            shutil.copy2(dat_src, dat)
            shutil.copy2(ext_src, ext)

            grid, cues, _warnings = read_anlz(dat, ext, sample_rate)
            parsed += 1

            # Did EXT carry PCO2, and did we surface cues (usually from PCO2)?
            idx = tag_index(ext)
            if any(f == "PCO2" for f, _ in idx) and cues:
                pco2_tracks += 1

            if grid is not None and grid.beats:
                offsets = [b.sample_offset for b in grid.beats]
                assert offsets == sorted(offsets), "beat sample offsets must be monotonic"
                # beat_in_bar cycles 1→4
                for b in grid.beats:
                    assert 1 <= b.beat_in_bar <= 4
                bpms = [b.bpm for b in grid.beats]
                assert all(40.0 <= bpm <= 300.0 for bpm in bpms), f"implausible BPM {bpms[:3]}"

            for c in cues:
                if c.end_sample is not None:
                    assert c.is_loop is True
                    assert c.end_sample >= c.start_sample

    # Surface counts for the worker report (also assert we actually did work)
    assert parsed >= 10
    # Store on the test node for the human report path
    test_real_stick_sample_grids_and_cues.last_stats = {  # type: ignore[attr-defined]
        "parsed": parsed,
        "pco2_with_cues": pco2_tracks,
        "pairs": len(pairs),
    }


@pytest.mark.real_stick
@pytest.mark.skipif(not _stick_available(), reason="real rekordbox stick not mounted")
def test_real_stick_known_loop_file() -> None:
    """The one type=2 loop found on this stick must yield is_loop=True."""
    src = _USBANLZ / "P01E" / "0002DAF6" / "ANLZ0000.EXT"
    if not src.is_file():
        pytest.skip("known loop EXT path missing on this stick")
    dat_src = src.with_suffix(".DAT")
    with tempfile.TemporaryDirectory(prefix="rb2engine_anlz_loop_") as tmp:
        tmp_path = Path(tmp)
        ext = tmp_path / "x.EXT"
        shutil.copy2(src, ext)
        dat = None
        if dat_src.is_file():
            dat = tmp_path / "x.DAT"
            shutil.copy2(dat_src, dat)
        _grid, cues, _ = read_anlz(dat, ext, 44100)
    loops = [c for c in cues if c.is_loop]
    assert loops, f"expected at least one loop cue, got {cues!r}"
    assert all(c.end_sample is not None for c in loops)
