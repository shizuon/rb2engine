"""Hand-rolled ANLZ tag-index walk + allowlist; gates G2/G2b.

Never call ``AnlzFile.parse_file()`` — a known tag that fails to parse
(notably ``PSSI`` with ``version=1``, pyrekordbox #199) aborts the whole file
and drops beatgrids/cues we could otherwise convert.

We walk ``{fourcc, len_header, len_tag}`` ourselves and parse only the
allowlist, using pyrekordbox's semi-public per-tag structs / TAGS registry
where safe, and lenient field layouts for cue tags whose real-world Const
assumptions fail on exported sticks.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path

from construct import (
    Array,
    Bytes,
    Const,
    Int8ub,
    Int16ub,
    Int32sb,
    Int32ub,
    Padding,
    Struct,
    this,
)  # Int8ub kept for layout parity with pyrekordbox PCPT type field
from construct.core import ConstructError

from rb2engine.errors import TrackSkipped
from rb2engine.ir import RGB, CueKind, SourceBeat, SourceBeatgrid, SourceCue
from rb2engine.units import ms_to_samples

# G2 allowlist — everything else is skipped unparsed (immunises against #199).
ALLOWLIST: frozenset[str] = frozenset({"PQTZ", "PQT2", "PCOB", "PCO2", "PPTH"})

# Unset loop-out sentinel used by rekordbox (u32 -1).
_LOOP_UNSET = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# Lenient PCOB layouts — mirror pyrekordbox.anlz.structs with Consts relaxed.
# Real USB exports set status/u1 outside upstream Const assumptions.
# PQTZ/PQT2/PPTH go through pyrekordbox.anlz.tags.TAGS instead.
# ---------------------------------------------------------------------------

_PCPT_ENTRY = Struct(
    "magic" / Const(b"PCPT"),
    "len_header" / Int32ub,
    "len_entry" / Int32ub,
    "hot_cue" / Int32ub,
    "status" / Int32ub,
    "u1" / Int32ub,
    "order_first" / Int16ub,
    "order_last" / Int16ub,
    "cue_type" / Int8ub,  # 1=point, 2=loop
    Padding(1),
    "u2" / Int16ub,
    "time" / Int32ub,
    "loop_time" / Int32ub,
    "rest" / Bytes(this.len_entry - 40),
)

_PCOB_BODY = Struct(
    "list_type" / Int32ub,  # 0=memory, 1=hotcue
    "unk" / Int16ub,
    "count" / Int16ub,
    "memory_count" / Int32sb,
    "entries" / Array(this.count, _PCPT_ENTRY),
)


def _raise_skipped(reason_code: str, detail: str = "") -> None:
    msg = f"{reason_code}:{detail}" if detail else reason_code
    exc = TrackSkipped(msg)
    exc.reason_code = reason_code  # type: ignore[attr-defined]
    raise exc


def tag_index(path: Path) -> list[tuple[str, int]]:
    """Return ``[(fourcc, len_tag), ...]`` by walking the tag envelope only.

    Does not parse tag bodies. Safe for ``.DAT`` / ``.EXT`` / ``.2EX``.
    """
    data = path.read_bytes()
    return [(fourcc, len_tag) for fourcc, _lh, len_tag, _blob in _iter_raw_tags(data)]


def read_anlz(
    dat_path: Path | None,
    ext_path: Path | None,
    sample_rate: int,
) -> tuple[SourceBeatgrid | None, list[SourceCue], list[str]]:
    """Read beatgrid + cues from ANLZ ``.DAT`` / ``.EXT`` pair.

    Parameters
    ----------
    dat_path:
        Path to ``ANLZnnnn.DAT`` (analyzed grid, basic cues), or None.
    ext_path:
        Path to ``ANLZnnnn.EXT`` (PQT2, PCO2), or None. Prefer EXT cues/grid signal.
    sample_rate:
        Track sample rate in Hz from pdb — never assumed.

    Returns
    -------
    (grid, cues, warnings)
        Positions are integer samples. Malformed required tags or tag envelopes
        raise ``TrackSkipped`` with a machine-stable ``reason_code``. An
        unsupported optional PQT2 body warns without discarding PQTZ or cues.
    """
    if sample_rate <= 0:
        _raise_skipped("anlz_invalid_sample_rate", str(sample_rate))

    warnings: list[str] = []
    pqtz_beats: list[SourceBeat] | None = None
    has_pqt2 = False
    pcob_cues: list[SourceCue] = []
    pco2_cues: list[SourceCue] = []
    saw_pco2_tag = False

    paths: list[tuple[str, Path]] = []
    if dat_path is not None:
        paths.append(("dat", Path(dat_path)))
    if ext_path is not None:
        paths.append(("ext", Path(ext_path)))

    if not paths:
        return None, [], []

    for kind, path in paths:
        suffix = path.suffix.upper()
        if suffix == ".2EX":
            # Detect and count only — never parse waveform bodies.
            index = tag_index(path)
            warnings.append(f"2ex_present:{path.name}:{len(index)}_tags")
            for fourcc, _ in index:
                if fourcc not in ALLOWLIST:
                    warnings.append(f"unknown_tag:{fourcc}")
            continue

        data = path.read_bytes()
        try:
            tags = list(_iter_raw_tags(data))
        except TrackSkipped:
            raise
        except Exception as exc:  # noqa: BLE001
            _raise_skipped("anlz_bad_header", f"{path.name}:{exc}")

        for fourcc, _len_header, _len_tag, blob in tags:
            if fourcc not in ALLOWLIST:
                warnings.append(f"unknown_tag:{fourcc}")
                continue

            if fourcc == "PQT2":
                # The envelope walk already proved where this tag ends. PQT2
                # is optional, so a parser's strict variant/layout assumptions
                # must not erase the PQTZ grid or prevent later cues being read.
                # No fractional timing or variable-tempo inference is applied.
                try:
                    _parse_pqt2(blob)
                    has_pqt2 = True
                except TrackSkipped as exc:
                    warnings.append(f"pqt2_ignored:{path.name}:{exc}")
                continue

            try:
                if fourcc == "PQTZ":
                    parsed = _parse_pqtz(blob, sample_rate)
                    # DAT analyzed grid is the full beat list; keep first unless
                    # we later decide EXT carries a replacement (it does not —
                    # PQT2 has no per-beat times). Last PQTZ wins if both files
                    # somehow carry one (DAT is the normal source).
                    if kind == "dat" or pqtz_beats is None:
                        pqtz_beats = parsed
                elif fourcc == "PCOB":
                    # Only collect PCOB when we do not yet have PCO2 cues from EXT.
                    # Always parse DAT PCOB as fallback material.
                    pcob_cues.extend(_parse_pcob(blob, sample_rate))
                elif fourcc == "PCO2":
                    saw_pco2_tag = True
                    pco2_cues.extend(_parse_pco2(blob, sample_rate))
                elif fourcc == "PPTH":
                    _parse_ppth(blob)  # validate; path not returned by this API
            except TrackSkipped:
                raise
            except ConstructError as exc:
                _raise_skipped("anlz_malformed_tag", f"{fourcc}:{exc}")
            except Exception as exc:  # noqa: BLE001
                _raise_skipped("anlz_malformed_tag", f"{fourcc}:{exc}")

    grid: SourceBeatgrid | None = None
    if pqtz_beats is not None:
        grid = SourceBeatgrid(beats=pqtz_beats, is_adjusted=has_pqt2)
    elif has_pqt2:
        # PQT2 present without PQTZ: cannot build a full grid from known fields.
        warnings.append("pqt2_without_pqtz")

    # Prefer PCO2 (extended colors/names) when any PCO2 tag was present.
    cues = pco2_cues if saw_pco2_tag else pcob_cues

    return grid, cues, warnings


# ---------------------------------------------------------------------------
# Tag index walk
# ---------------------------------------------------------------------------


def _iter_raw_tags(data: bytes) -> Iterable[tuple[str, int, int, bytes]]:
    """Yield ``(fourcc, len_header, len_tag, full_tag_blob)`` for each tag."""
    if len(data) < 12:
        _raise_skipped("anlz_bad_header", "truncated")

    magic = data[0:4]
    if magic != b"PMAI":
        _raise_skipped("anlz_bad_header", f"magic={magic!r}")

    len_header, len_file = struct.unpack_from(">II", data, 4)
    if len_header < 12 or len_header > len(data):
        _raise_skipped("anlz_bad_header", f"len_header={len_header}")

    # Tolerate len_file past EOF slightly; walk to min(len_file, len(data)).
    end = min(len_file, len(data)) if len_file else len(data)
    offset = len_header

    while offset + 12 <= end:
        fourcc_b = data[offset : offset + 4]
        try:
            fourcc = fourcc_b.decode("ascii")
        except UnicodeDecodeError:
            _raise_skipped("anlz_bad_tag_fourcc", f"offset={offset}")

        tag_len_header, len_tag = struct.unpack_from(">II", data, offset + 4)
        if len_tag < 12:
            _raise_skipped("anlz_malformed_tag", f"{fourcc}:len_tag={len_tag}")
        if offset + len_tag > len(data):
            _raise_skipped("anlz_malformed_tag", f"{fourcc}:truncated")

        blob = data[offset : offset + len_tag]
        yield fourcc, tag_len_header, len_tag, blob
        offset += len_tag


def _body(blob: bytes) -> bytes:
    """Tag content after the 12-byte envelope."""
    if len(blob) < 12:
        _raise_skipped("anlz_malformed_tag", "short_envelope")
    return blob[12:]


# ---------------------------------------------------------------------------
# Per-tag parsers
# ---------------------------------------------------------------------------


def _parse_via_tags_registry(fourcc: str, blob: bytes):
    """Parse an allowlisted tag through pyrekordbox's semi-public TAGS registry.

    Used for PQTZ / PQT2 / PPTH where upstream Const fields match real exports.
    PCOB/PCO2 use lenient local structs — upstream Const(u1=0x10000) and
    fixed-length PCO2 entries fail on this stick (status=1, truncated colors).
    """
    from pyrekordbox.anlz.tags import TAGS

    cls = TAGS.get(fourcc)
    if cls is None:
        _raise_skipped("anlz_version_mismatch", f"{fourcc}:missing_from_TAGS")
    try:
        return cls(blob)
    except Exception as exc:
        _raise_skipped("anlz_malformed_tag", f"{fourcc}:{exc}")
        raise  # unreachable


def _parse_pqtz(blob: bytes, sample_rate: int) -> list[SourceBeat]:
    tag = _parse_via_tags_registry("PQTZ", blob)
    content = tag.content

    beats: list[SourceBeat] = []
    for entry in content.entries:
        beat_in_bar = int(entry.beat)
        if not 1 <= beat_in_bar <= 4:
            _raise_skipped("anlz_version_mismatch", f"PQTZ:beat_in_bar={beat_in_bar}")
        tempo_centi = int(entry.tempo)
        time_ms = int(entry.time)
        beats.append(
            SourceBeat(
                beat_in_bar=beat_in_bar,
                sample_offset=ms_to_samples(float(time_ms), sample_rate),
                bpm=tempo_centi / 100.0,
            )
        )
    return beats


def _parse_pqt2(blob: bytes) -> None:
    """Validate PQT2 via TAGS. Full per-beat times are not available in this tag."""
    _parse_via_tags_registry("PQT2", blob)


def _parse_ppth(blob: bytes) -> str:
    tag = _parse_via_tags_registry("PPTH", blob)
    return str(tag.content.path)


def _loop_end_ms(cue_type: int, loop_time: int) -> int | None:
    """Return loop-out milliseconds if set, else None.

    rekordbox stores ``0xFFFFFFFF`` when there is no loop. Point cues (type 1)
    may still carry ``0`` or garbage in the loop field — only type 2 (loop) or
    a loop_time strictly past the unset sentinel and used as out-point counts.
    """
    if loop_time == _LOOP_UNSET:
        return None
    if cue_type == 2:
        return int(loop_time)
    # type 1 (point): ignore loop_time even if zero
    if cue_type == 1:
        return None
    if loop_time > 0:
        return int(loop_time)
    return None


def _kind_and_slot(list_type: int, hot_cue: int) -> tuple[CueKind, int | None]:
    if list_type == 1 or hot_cue >= 1:
        slot = int(hot_cue) if hot_cue >= 1 else None
        return CueKind.HOT, slot
    return CueKind.MEMORY, None


def _palette_rgb(color_id: int) -> RGB | None:
    """PCOB / memory color_id → RGB via colors.py (owned by another worker)."""
    try:
        from rb2engine.reader import colors as colors_mod
    except ImportError:  # pragma: no cover
        return None
    fn = getattr(colors_mod, "color_id_to_rgb", None)
    if fn is None:
        return None
    try:
        result = fn(color_id)
    except Exception:  # noqa: BLE001
        return None
    if result is None:
        return None
    if isinstance(result, RGB):
        return result
    if isinstance(result, tuple) and len(result) == 3:
        return RGB(int(result[0]), int(result[1]), int(result[2]))
    return None


def _parse_pcob(blob: bytes, sample_rate: int) -> list[SourceCue]:
    body = _body(blob)
    # Empty list tag (count=0) is valid — body may be empty or header-only.
    if len(body) == 0:
        return []
    try:
        parsed = _PCOB_BODY.parse(body)
    except ConstructError as exc:
        _raise_skipped("anlz_malformed_tag", f"PCOB:{exc}")
        raise

    list_type = int(parsed.list_type)
    out: list[SourceCue] = []
    for entry in parsed.entries:
        if entry.magic != b"PCPT":
            _raise_skipped("anlz_malformed_tag", "PCOB:bad_entry_magic")
        kind, slot = _kind_and_slot(list_type, int(entry.hot_cue))
        # When list says hotcue, force HOT even if hot_cue==0 (shouldn't happen).
        if list_type == 1:
            kind = CueKind.HOT
            slot = int(entry.hot_cue) if int(entry.hot_cue) >= 1 else None
        elif list_type == 0:
            kind = CueKind.MEMORY
            slot = None

        end_ms = _loop_end_ms(int(entry.cue_type), int(entry.loop_time))
        out.append(
            SourceCue(
                kind=kind,
                hot_slot=slot,
                start_sample=ms_to_samples(float(entry.time), sample_rate),
                end_sample=(
                    ms_to_samples(float(end_ms), sample_rate) if end_ms is not None else None
                ),
                color=None,  # PCOB has no RGB; palette via color_id not in PCPT
                name=None,
            )
        )
    return out


def _parse_pco2(blob: bytes, sample_rate: int) -> list[SourceCue]:
    """Walk PCO2 entries by per-entry ``len_entry`` (variable; may truncate colors)."""
    body = _body(blob)
    if len(body) < 8:
        return []

    list_type = struct.unpack_from(">I", body, 0)[0]
    count = struct.unpack_from(">H", body, 4)[0]
    # unknown u16 at 6
    offset = 8
    out: list[SourceCue] = []

    for _ in range(count):
        if offset + 12 > len(body):
            _raise_skipped("anlz_malformed_tag", "PCO2:truncated_entry")
        magic = body[offset : offset + 4]
        if magic != b"PCP2":
            _raise_skipped("anlz_malformed_tag", f"PCO2:bad_entry_magic={magic!r}")
        _eh, len_entry = struct.unpack_from(">II", body, offset + 4)
        if len_entry < 12 or offset + len_entry > len(body):
            _raise_skipped("anlz_malformed_tag", f"PCO2:len_entry={len_entry}")

        entry = body[offset : offset + len_entry]
        hot_cue = struct.unpack_from(">I", entry, 12)[0]
        cue_type = entry[16] if len(entry) > 16 else 1
        time_ms = struct.unpack_from(">I", entry, 20)[0] if len(entry) >= 24 else 0
        loop_time = (
            struct.unpack_from(">I", entry, 24)[0] if len(entry) >= 28 else _LOOP_UNSET
        )
        color_id = entry[28] if len(entry) > 28 else 0

        comment = ""
        color: RGB | None = None
        # comment starts at offset 40 within entry (after loop num/den at 36–39)
        if len(entry) >= 44:
            len_comment = struct.unpack_from(">I", entry, 40)[0]
            if len_comment < 0 or 44 + len_comment > len(entry):
                # Truncated comment region — treat as no comment rather than skip track
                len_comment = 0
            if len_comment:
                raw = entry[44 : 44 + len_comment]
                comment = raw.decode("utf-16-be", errors="replace").rstrip("\x00")
                color_off = 44 + len_comment
            else:
                color_off = 44
            if len(entry) >= color_off + 4:
                # color_code, r, g, b
                r, g, b = entry[color_off + 1], entry[color_off + 2], entry[color_off + 3]
                if r or g or b:
                    color = RGB(int(r), int(g), int(b))

        if color is None and color_id:
            color = _palette_rgb(int(color_id))

        if list_type == 1:
            kind = CueKind.HOT
            slot = int(hot_cue) if hot_cue >= 1 else None
        elif list_type == 0:
            kind = CueKind.MEMORY
            slot = None
        else:
            kind, slot = _kind_and_slot(list_type, hot_cue)

        end_ms = _loop_end_ms(int(cue_type), int(loop_time))
        out.append(
            SourceCue(
                kind=kind,
                hot_slot=slot,
                start_sample=ms_to_samples(float(time_ms), sample_rate),
                end_sample=(
                    ms_to_samples(float(end_ms), sample_rate) if end_ms is not None else None
                ),
                color=color,
                name=comment or None,
            )
        )
        offset += len_entry

    return out


# Re-export TAGS coupling for tests / discoverability.
def _tags_registry() -> dict:
    from pyrekordbox.anlz.tags import TAGS

    return TAGS


__all__ = ["ALLOWLIST", "read_anlz", "tag_index"]
