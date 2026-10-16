"""PQTZ/PQT2 → compressed Engine beat markers; -4 normalization; default vs adjusted grids.

rekordbox lists EVERY beat (~550 for a 6-min track). Engine wants SPARSE markers
with tempo implied by (Δsample_offset)/(Δbeat_number) between adjacent markers.

Algorithm (aligned with libdjinterop normalize_beatgrid + convert write path):
1. Collapse linear timing runs; retain BPM changes and timestamp corrections.
2. Shift the first marker so its beat index is -4 (Engine convention).
3. Extrapolate the last marker to the first beat past total_samples.
4. number_of_beats on each non-final marker = next.beat_number - this.beat_number.

Downbeat: preserve the first source beat's position within its bar so
beat_in_bar==1 stays on beat_number ≡ 0 (mod 4), including pickup beats.
"""

from __future__ import annotations

import math

from rb2engine.ir import SourceBeat, SourceBeatgrid
from rb2engine.ir_engine import EngineBeatGrid, EngineBeatMarker

# BPM stored as centi-bpm floats from ANLZ; equality within this is "same tempo".
_BPM_EPS = 1e-4
# PQTZ stores whole milliseconds. Compression must not add more than one
# millisecond of timing error to any source beat, even at unchanged BPM.
_MAX_TIMING_ERROR_SECONDS = 0.001


def compress_beatgrid(
    grid: SourceBeatgrid,
    *,
    sample_rate: int,
    total_samples: int | None,
) -> EngineBeatGrid:
    """Compress a dense rekordbox beat list into Engine sparse markers.

    Parameters
    ----------
    grid:
        Source beatgrid (PQTZ beats; ``is_adjusted`` from PQT2 presence).
    sample_rate:
        Track sample rate in Hz (for tempo math / fallbacks).
    total_samples:
        Exact sample count for end-of-track extrapolation; ``None`` falls back
        to the last beat offset so mapping still succeeds without mutagen.
    """
    if not grid.beats:
        return EngineBeatGrid(
            default_markers=[],
            adjusted_markers=[],
            is_beatgrid_set=False,
        )

    sparse = _compress_dense(
        grid.beats,
        tolerance_samples=max(1.0, sample_rate * _MAX_TIMING_ERROR_SECONDS),
    )
    sample_count = _resolve_sample_count(sparse, total_samples)
    normalized = _normalize_beatgrid(sparse, sample_count)
    markers = _with_number_of_beats(normalized)

    # Writer policy: adjusted grid must never be empty when a grid exists.
    # Reader only supplies one dense list (PQTZ); is_adjusted is a flag only.
    return EngineBeatGrid(
        default_markers=markers,
        adjusted_markers=list(markers),
        is_beatgrid_set=True,
    )


def _same_tempo(a: SourceBeat, b: SourceBeat) -> bool:
    return abs(a.bpm - b.bpm) <= _BPM_EPS


def _compress_dense(
    beats: list[SourceBeat], *, tolerance_samples: float
) -> list[EngineBeatMarker]:
    """Retain source timing within tolerance in one pass over the beats.

    Each beat bounds the slopes allowed from the current anchor. A new
    endpoint outside their intersection would move an earlier beat too far,
    so retain the previous endpoint and begin a new interval there. Every
    completed interval therefore has a previously checked endpoint, with no
    rescanning. BPM changes remain mandatory anchors as before.
    """
    n = len(beats)
    # PQTZ may start on beat 2, 3 or 4. Dense list index zero is not necessarily
    # a musical downbeat; preserve the source bar phase without moving audio
    # timestamps. Normalization extrapolates from these absolute beat numbers.
    first_beat_number = beats[0].beat_in_bar - 1
    if n == 1:
        b = beats[0]
        return [
            EngineBeatMarker(
                sample_offset=float(b.sample_offset),
                beat_number=first_beat_number,
                number_of_beats=0,
                unknown=0,
            )
        ]

    keep: list[int] = [0]
    minimum_slope, maximum_slope = -math.inf, math.inf
    for i in range(1, n):
        anchor = keep[-1]
        distance = i - anchor
        delta = float(beats[i].sample_offset - beats[anchor].sample_offset)
        slope = delta / distance
        if not minimum_slope <= slope <= maximum_slope:
            # The previous candidate satisfied every prior timing bound.
            keep.append(i - 1)
            distance = 1
            delta = float(beats[i].sample_offset - beats[i - 1].sample_offset)
            minimum_slope, maximum_slope = -math.inf, math.inf
        if not _same_tempo(beats[i - 1], beats[i]):
            keep.append(i)
            minimum_slope, maximum_slope = -math.inf, math.inf
        else:
            minimum_slope = max(minimum_slope, (delta - tolerance_samples) / distance)
            maximum_slope = min(maximum_slope, (delta + tolerance_samples) / distance)
    if keep[-1] != n - 1:
        keep.append(n - 1)

    return [
        EngineBeatMarker(
            sample_offset=float(beats[i].sample_offset),
            beat_number=first_beat_number + i,
            number_of_beats=0,
            unknown=0,
        )
        for i in keep
    ]


def _resolve_sample_count(
    markers: list[EngineBeatMarker], total_samples: int | None
) -> int:
    if total_samples is not None and total_samples > 0:
        return int(total_samples)
    # Fallback: treat last marker as the track end so we still extrapolate +1 beat.
    last = markers[-1].sample_offset
    return max(math.ceil(last), 1)


def _normalize_beatgrid(
    markers: list[EngineBeatMarker], sample_count: int
) -> list[EngineBeatMarker]:
    """libdjinterop ``normalize_beatgrid`` semantics (Engine -4 / past-end)."""
    if not markers:
        return []

    # Work on a mutable copy of (offset, index).
    work: list[tuple[float, int]] = [
        (m.sample_offset, int(m.beat_number)) for m in markers
    ]

    # Drop markers strictly after the first one past sample_count.
    last_past = None
    for i, (off, _) in enumerate(work):
        if off > sample_count:
            last_past = i
            break
    if last_past is not None:
        work = work[: last_past + 1]

    # Drop leading markers before the last one with sample_offset <= 0.
    after_first = None
    for i, (off, _) in enumerate(work):
        if off > 0:
            after_first = i
            break
    if after_first is not None and after_first > 0:
        work = work[after_first - 1 :]

    if len(work) < 2:
        # Single-marker (or empty after trim): synthesize a 1-beat end so Engine
        # still gets a 2-point grid rather than a misplaced single point.
        if not work:
            return []
        off0, idx0 = work[0]
        # Assume 120 BPM placeholder only if we truly lack a second point —
        # prefer extending by one index unit with zero sample delta avoided by
        # a minimal positive step derived from a default 120 BPM at 44.1k.
        # Callers with real grids always have ≥2 dense beats.
        work = [(off0, idx0), (off0 + 1.0, idx0 + 1)]

    # First marker → beat index -4, sample shifted earlier by the same number of beats.
    spb = (work[1][0] - work[0][0]) / (work[1][1] - work[0][1])
    if spb == 0:
        spb = 1.0  # degenerate guard
    new_first_off = work[0][0] - (4 + work[0][1]) * spb
    work[0] = (new_first_off, -4)

    # Last marker → first beat past usable end of track.
    last = len(work) - 1
    spb_end = (work[last][0] - work[last - 1][0]) / (
        work[last][1] - work[last - 1][1]
    )
    if spb_end == 0:
        spb_end = spb if spb != 0 else 1.0
    # Strictly past the end, including when it lies exactly on a whole beat.
    index_adjustment = math.floor((sample_count - work[last][0]) / spb_end) + 1
    work[last] = (
        work[last][0] + index_adjustment * spb_end,
        work[last][1] + index_adjustment,
    )

    return [
        EngineBeatMarker(
            sample_offset=off,
            beat_number=idx,
            number_of_beats=0,
            unknown=0,
        )
        for off, idx in work
    ]


def _with_number_of_beats(
    markers: list[EngineBeatMarker],
) -> list[EngineBeatMarker]:
    """Fill number_of_beats as (next.beat_number - this.beat_number); last = 0."""
    if not markers:
        return []
    out: list[EngineBeatMarker] = []
    for i, m in enumerate(markers):
        nbeats = int(markers[i + 1].beat_number - m.beat_number) if i + 1 < len(markers) else 0
        out.append(
            EngineBeatMarker(
                sample_offset=m.sample_offset,
                beat_number=m.beat_number,
                number_of_beats=nbeats,
                unknown=0,
            )
        )
    return out
