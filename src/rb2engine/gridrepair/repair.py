"""Conservative, opt-in constant-tempo shifts with source-bound provenance."""

from __future__ import annotations

import dataclasses
import math
import subprocess
from itertools import combinations
from typing import Any

from rb2engine.errors import FatalError
from rb2engine.ir import SourceBeatgrid, SourceLibrary, SourceTrack
from rb2engine.keyscan import digest_record, file_hash
from rb2engine.progress import ProgressCallback

try:
    import numpy as np
except ImportError as exc:
    raise FatalError("Grid repair needs the grid-repair extra or the desktop release.") from exc

SCHEMA = 1
POLICY = "separated-passages-fixed-tempo-v1"
MAX_SHIFT_SECONDS = 0.2
MIN_SHIFT_SECONDS = 0.02


def grid_digest(grid: SourceBeatgrid) -> str:
    return digest_record(
        {"beats": [dataclasses.asdict(b) for b in grid.beats], "is_adjusted": grid.is_adjusted}
    )


def eligibility(track: SourceTrack) -> str | None:
    grid = track.beatgrid
    if grid is None or len(grid.beats) < 64:
        return "missing_or_short_grid"
    if track.sample_rate not in (44100, 48000):
        return "unsupported_sample_rate"
    if track.resolved_path is None:
        return "audio_missing"
    times = np.asarray([b.sample_offset for b in grid.beats], dtype=np.float64)
    bpms = np.asarray([b.bpm for b in grid.beats])
    if (
        not np.isfinite(times).all()
        or not np.isfinite(bpms).all()
        or np.any(np.diff(times) <= 0)
        or np.any(bpms <= 0)
    ):
        return "invalid_grid"
    if np.ptp(bpms) > 0.0001:
        return "variable_tempo_grid"
    period = (times[-1] - times[0]) / (len(times) - 1)
    if not 55 <= 60 * track.sample_rate / period <= 215:
        return "tempo_outside_supported_range"
    if np.max(abs(times - (times[0] + np.arange(len(times)) * period))) > track.sample_rate * 0.002:
        return "nonlinear_grid"
    first = grid.beats[0].beat_in_bar
    if first not in (1, 2, 3, 4) or any(
        b.beat_in_bar != (first - 1 + i) % 4 + 1 for i, b in enumerate(grid.beats)
    ):
        return "bar_phase_changes"
    return None


def _nearest(source: np.ndarray, values: np.ndarray) -> np.ndarray:
    right = np.searchsorted(source, values).clip(0, len(source) - 1)
    left = (right - 1).clip(0, len(source) - 1)
    return np.where(abs(values - source[left]) < abs(values - source[right]), left, right)


def assess(
    track: SourceTrack,
    passages: list[dict[str, Any]],
    *,
    mode: str = "strict",
) -> dict[str, Any]:
    """Keep tempo; strict needs all three passages, majority needs any two."""
    if mode not in ("strict", "majority"):
        raise FatalError("Unknown grid repair mode")
    reason = eligibility(track)
    result: dict[str, Any] = {"status": "skipped", "reason": reason, "windows": []}
    if reason:
        return result
    assert track.beatgrid is not None
    source = np.asarray([b.sample_offset / track.sample_rate for b in track.beatgrid.beats])
    labels = np.asarray([b.beat_in_bar for b in track.beatgrid.beats])
    period = (source[-1] - source[0]) / (len(source) - 1)
    if len(passages) != 3:
        return {**result, "reason": "three_passages_required"}
    ends: list[float] = []
    candidates: list[tuple[int, float, int]] = []
    for passage in passages:
        beats = np.asarray(passage["beats"], dtype=np.float64)
        downbeats = np.asarray(passage["downbeats"], dtype=np.float64)
        start, duration = float(passage["start_seconds"]), float(passage["duration_seconds"])
        if (
            not math.isfinite(start + duration)
            or duration < 30
            or start < 0
            or (ends and start < ends[-1] - 0.001)
        ):
            return {**result, "reason": "overlapping_or_short_passages"}
        ends.append(start + duration)
        valid = (
            beats.ndim == downbeats.ndim == 1
            and np.isfinite(beats).all()
            and np.isfinite(downbeats).all()
            and np.all(np.diff(beats) > 0)
            and np.all(np.diff(downbeats) > 0)
        )
        if (
            not valid
            or len(beats) < 24
            or len(downbeats) < 8
            or np.any(beats < max(source[0], start))
            or np.any(beats > min(source[-1], start + duration))
            or np.any(downbeats < start)
            or np.any(downbeats > start + duration)
        ):
            result["windows"].append(
                {
                    "start_seconds": start,
                    "duration_seconds": duration,
                    "reason": "insufficient_or_invalid_predictions",
                }
            )
            continue
        residual = beats - source[_nearest(source, beats)]
        offset = float(np.median(residual))
        if abs(offset) > min(MAX_SHIFT_SECONDS, period * 0.4):
            result["windows"].append(
                {
                    "start_seconds": start,
                    "duration_seconds": duration,
                    "reason": "offset_too_large_or_half_beat_ambiguous",
                }
            )
            continue
        indices = _nearest(source, beats - offset)
        aligned = beats - offset - source[indices]
        match = float(np.mean(abs(aligned) <= 0.04))
        consecutive = float(np.mean(np.diff(indices) == 1))
        db_indices = _nearest(source, downbeats - offset)
        db_close = abs(downbeats - offset - source[db_indices]) <= 0.04
        counts = np.bincount(labels[db_indices[db_close]], minlength=5)
        phase = int(np.argmax(counts))
        support = float(counts[phase] / len(downbeats))
        window: dict[str, Any] = {
            "start_seconds": round(start, 6),
            "duration_seconds": round(duration, 6),
            "beats": len(beats),
            "downbeats": len(downbeats),
            "offset_ms": round(offset * 1000, 3),
            "beat_match_fraction": round(match, 6),
            "consecutive_beat_fraction": round(consecutive, 6),
            "downbeat_source_bar_beat": phase,
            "downbeat_support_fraction": round(support, 6),
        }
        result["windows"].append(window)
        if match < 0.95 or consecutive < 0.9 or support < 0.9:
            window["reason"] = "inconsistent_beats_or_downbeats"
            continue
        candidates.append((len(result["windows"]) - 1, offset, phase))
    required = 3 if mode == "strict" else 2
    # Prefer all three if possible, otherwise the closest agreeing pair.
    # Tie breaks by passage order, so identical input has a stable result.
    agreements = [
        group
        for size in range(3, required - 1, -1)
        for group in combinations(candidates, size)
        if len({c[2] for c in group}) == 1
        and max(c[1] for c in group) - min(c[1] for c in group) <= 0.02 + 1e-9
    ]
    if not agreements:
        reason = (
            "insufficient_reliable_passages" if len(candidates) < required else "passages_disagree"
        )
        return {**result, "reason": reason}
    accepted = min(agreements, key=lambda g: (-len(g), max(c[1] for c in g) - min(c[1] for c in g)))
    shift = float(np.median([c[1] for c in accepted]))
    samples = round(shift * track.sample_rate) if abs(shift) > MIN_SHIFT_SECONDS else 0
    phase_shift = (1 - accepted[0][2]) % 4
    return {
        **result,
        "status": "corrected" if samples or phase_shift else "unchanged",
        "reason": "consistent_passages" if samples or phase_shift else "already_aligned",
        "supporting_windows": [c[0] for c in accepted],
        "sample_shift": samples,
        "time_shift_ms": samples / track.sample_rate * 1000,
        "bar_phase_shift": phase_shift,
    }


def shifted_grid(track: SourceTrack, samples: int, phase: int) -> SourceBeatgrid:
    assert track.beatgrid is not None
    return SourceBeatgrid(
        beats=[
            dataclasses.replace(
                b,
                sample_offset=b.sample_offset + samples,
                beat_in_bar=(b.beat_in_bar - 1 + phase) % 4 + 1,
            )
            for b in track.beatgrid.beats
        ],
        is_adjusted=True,
    )


def repair_library(
    library: SourceLibrary,
    *,
    mode: str = "strict",
    on_progress: ProgressCallback | None = None,
) -> tuple[SourceLibrary, dict[str, Any]]:
    from rb2engine.gridrepair.runtime import BeatDetector

    if mode not in ("strict", "majority"):
        raise FatalError("Unknown grid repair mode")
    if on_progress:
        on_progress("preparing grid repair", 0, 0)
    detector = BeatDetector()  # Missing/corrupt runtime is fatal before database publication.
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "policy": POLICY,
        "mode": mode,
        **detector.identity,
        "tracks": [],
        "corrected": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
    }
    tracks = dict(library.tracks)
    for done, track in enumerate(library.tracks.values(), 1):
        if on_progress:
            on_progress("checking grid alignment", done - 1, len(tracks))
        reason = eligibility(track)
        row: dict[str, Any] = {"track_id": track.rb_id, "status": "skipped", "reason": reason}
        if reason is None:
            assert track.resolved_path is not None and track.beatgrid is not None
            try:
                before = file_hash(track.resolved_path)
                passages, clock = detector.analyze(track.resolved_path, track.sample_rate)
                if file_hash(track.resolved_path) != before:
                    raise FatalError("Audio changed during grid analysis; conversion stopped")
                row.update(assess(track, passages, mode=mode))
                row.update(
                    audio_sha256=before,
                    source_grid_sha256=grid_digest(track.beatgrid),
                    sample_rate=track.sample_rate,
                    clock=clock,
                )
                if row["status"] == "corrected":
                    tracks[track.rb_id] = dataclasses.replace(
                        track,
                        adjusted_beatgrid=shifted_grid(
                            track,
                            row["sample_shift"],
                            row["bar_phase_shift"],
                        ),
                    )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                row.update(status="failed", reason=str(exc))
        record["tracks"].append(row)
        record[row["status"]] += 1
        if on_progress:
            on_progress("checking grid alignment", done, len(tracks))
    return dataclasses.replace(library, tracks=tracks), record


def replay_repairs(library: SourceLibrary, record: dict[str, Any]) -> SourceLibrary:
    """Replay only after the caller validates the publication record's digest."""
    if (
        record.get("schema") != SCHEMA
        or record.get("policy") != POLICY
        or record.get("provider") != "BeatThis"
        or record.get("mode") not in ("strict", "majority")
        or not isinstance(record.get("tracks"), list)
    ):
        raise FatalError("Invalid grid repair record")
    tracks = dict(library.tracks)
    seen = set()
    for row in record["tracks"]:
        if not isinstance(row, dict):
            raise FatalError("Invalid grid repair track record")
        tid = row.get("track_id")
        if type(tid) is not int or tid not in tracks or tid in seen:
            raise FatalError("Grid repair record has an unknown or duplicate track")
        seen.add(tid)
        if row.get("status") not in ("corrected", "unchanged", "skipped", "failed"):
            raise FatalError("Invalid grid repair status")
        if row.get("status") != "corrected":
            continue
        track = tracks[tid]
        samples, phase = row.get("sample_shift"), row.get("bar_phase_shift")
        if (
            type(samples) is not int
            or abs(samples) > track.sample_rate * MAX_SHIFT_SECONDS
            or type(phase) is not int
            or not 0 <= phase <= 3
            or row.get("sample_rate") != track.sample_rate
            or eligibility(track)
            or track.beatgrid is None
            or grid_digest(track.beatgrid) != row.get("source_grid_sha256")
        ):
            raise FatalError(f"Grid repair source or shift for track {tid} changed; reconvert")
        if track.resolved_path is None or file_hash(track.resolved_path) != row.get("audio_sha256"):
            raise FatalError(f"Audio for grid repair track {tid} changed or is missing; reconvert")
        tracks[tid] = dataclasses.replace(
            track,
            adjusted_beatgrid=shifted_grid(track, samples, phase),
        )
    if seen != set(tracks):
        raise FatalError("Grid repair record does not cover the source library")
    return dataclasses.replace(library, tracks=tracks)


def validate_inputs(library: SourceLibrary, record: dict[str, Any]) -> None:
    """Re-read the source after long analysis, before publishing any database."""
    from rb2engine.reader.library import read_library

    fresh = read_library(library.drive_root, with_anlz=True, with_artwork=False)
    if (
        fresh.fingerprint is None
        or library.fingerprint is None
        or fresh.fingerprint.sha256 != library.fingerprint.sha256
    ):
        raise FatalError("The rekordbox export changed during grid analysis; reconvert")
    for row in record["tracks"]:
        track = fresh.tracks.get(row["track_id"])
        if not row.get("source_grid_sha256"):
            continue
        if (
            track is None
            or track.beatgrid is None
            or grid_digest(track.beatgrid) != row["source_grid_sha256"]
            or track.sample_rate != row["sample_rate"]
        ):
            raise FatalError("The source grid changed during analysis; reconvert")
        if track.resolved_path is None or file_hash(track.resolved_path) != row["audio_sha256"]:
            raise FatalError("Audio changed during grid analysis; reconvert")
