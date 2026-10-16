"""SourceTrack → EngineTrack field mapping.

Composes existing pieces — does not reimplement them:
- mapper.keys.key_name_to_ordinal
- mapper.cues.merge_cues (U3: end point → loop slot, frees pad)
- mapper.beatgrid.compress_beatgrid
- writer.paths.engine_track_path

Note: EngineTrack (ir_engine) currently carries the writer-facing subset of
columns. Source fields such as remixer / play_count / bitrate / file_size /
file_type / length are not on EngineTrack yet; map only what the IR holds.
"""

from __future__ import annotations

from pathlib import Path

from rb2engine.ir import RGB, SourceCue, SourceTrack
from rb2engine.ir import CueKind as IrCueKind
from rb2engine.ir_engine import (
    EMPTY_LOOP,
    EMPTY_QUICK_CUE,
    EngineBeatGrid,
    EngineTrack,
    LoopSlot,
    QuickCueSlot,
)
from rb2engine.mapper.beatgrid import compress_beatgrid
from rb2engine.mapper.cues import (
    CueKind as CuesCueKind,
)
from rb2engine.mapper.cues import (
    SourceCue as CuesSourceCue,
)
from rb2engine.mapper.cues import (
    merge_cues,
)
from rb2engine.mapper.keys import key_name_to_ordinal


def map_track(
    src: SourceTrack,
    *,
    drive_root: Path,
    engine_library_dir: Path,
) -> EngineTrack:
    """Map one source track to the Engine-side IR the writer consumes."""
    path = _map_path(src, drive_root=drive_root, engine_library_dir=engine_library_dir)
    key = _map_key(src.key_name)
    samples = _map_samples(src)
    beat_grid = _map_beatgrid(src, samples=samples)
    quick_cues, loops = _map_cues(src.cues)

    bpm_analyzed = float(src.bpm)
    bpm_int = round(bpm_analyzed)

    album_art_hash: str | None = None
    if src.artwork is not None:
        album_art_hash = src.artwork.content_key or None

    return EngineTrack(
        path=path,
        title=src.title or "",
        artist=src.artist or "",
        album=src.album or "",
        genre=src.genre or "",
        label=src.label or "",
        comment=src.comment or "",
        composer=src.composer or "",
        year=int(src.year) if src.year is not None else 0,
        track_number=src.track_number,
        disc_number=src.disc_number,
        bpm=bpm_int,
        bpm_analyzed=bpm_analyzed,
        key=key,
        rating=int(src.rating) if src.rating is not None else 0,
        sample_rate=float(src.sample_rate) if src.sample_rate else 0.0,
        samples=samples,
        date_added=None,
        date_created=None,
        last_edit_time=None,
        album_art_hash=album_art_hash,
        beat_grid=beat_grid,
        quick_cues=quick_cues,
        loops=loops,
    )


def _map_path(
    src: SourceTrack,
    *,
    drive_root: Path,
    engine_library_dir: Path,
) -> str:
    if src.resolved_path is None:
        return src.raw_path or ""
    try:
        # Lazy-capable import: paths is owned by another worker; prefer direct call.
        from rb2engine.writer.paths import engine_track_path
    except ImportError:  # pragma: no cover — present in this tree
        return src.raw_path or str(src.resolved_path)

    try:
        return engine_track_path(
            Path(src.resolved_path),
            drive_root=Path(drive_root),
            engine_library_dir=Path(engine_library_dir),
            source_path=src.raw_path,
        )
    except ValueError:
        # Outside drive root or geometry mismatch — degrade to raw_path.
        return src.raw_path or str(src.resolved_path)


def _map_key(key_name: str | None) -> int | None:
    if key_name is None:
        return None
    if not str(key_name).strip():
        return None
    return key_name_to_ordinal(key_name)


def _map_samples(src: SourceTrack) -> int:
    if src.total_samples is not None and src.total_samples > 0:
        return int(src.total_samples)
    if src.sample_rate and src.duration_s is not None:
        return int(src.duration_s) * int(src.sample_rate)
    return 0


def _map_beatgrid(src: SourceTrack, *, samples: int) -> EngineBeatGrid:
    if src.beatgrid is None:
        return EngineBeatGrid(
            default_markers=[],
            adjusted_markers=[],
            is_beatgrid_set=False,
        )
    total = samples if samples > 0 else None
    original = compress_beatgrid(
        src.beatgrid,
        sample_rate=int(src.sample_rate) if src.sample_rate else 0,
        total_samples=total,
    )
    if src.adjusted_beatgrid is None:
        return original
    adjusted = compress_beatgrid(
        src.adjusted_beatgrid, sample_rate=src.sample_rate, total_samples=total,
    )
    return EngineBeatGrid(default_markers=original.default_markers,
                          adjusted_markers=adjusted.adjusted_markers,
                          is_beatgrid_set=original.is_beatgrid_set)


def _map_cues(cues: list[SourceCue]) -> tuple[list[QuickCueSlot], list[LoopSlot]]:
    if not cues:
        return (
            [EMPTY_QUICK_CUE for _ in range(8)],
            [EMPTY_LOOP for _ in range(8)],
        )

    local = [_to_cues_source(c) for c in cues]
    result = merge_cues(local)

    quick = [
        QuickCueSlot(
            label=s.label,
            sample_offset=s.sample_offset,
            color=s.argb,
        )
        for s in result.quick_cues
    ]
    loops = [
        LoopSlot(
            label=s.label,
            start_sample_offset=s.start,
            end_sample_offset=s.end,
            is_start_set=s.is_start_set,
            is_end_set=s.is_end_set,
            color=s.argb,
        )
        for s in result.loops
    ]
    # Defensive pad to 8 if merge_cues shape ever drifts.
    while len(quick) < 8:
        quick.append(EMPTY_QUICK_CUE)
    while len(loops) < 8:
        loops.append(EMPTY_LOOP)
    return quick[:8], loops[:8]


def _to_cues_source(c: SourceCue) -> CuesSourceCue:
    """Adapt ir.SourceCue → mapper.cues local stand-in (color RGB vs tuple)."""
    color: tuple[int, int, int] | None = None
    if isinstance(c.color, RGB):
        color = (c.color.r, c.color.g, c.color.b)
    elif isinstance(c.color, tuple) and len(c.color) == 3:
        color = (int(c.color[0]), int(c.color[1]), int(c.color[2]))

    kind = (
        CuesCueKind.HOT
        if c.kind is IrCueKind.HOT
        else CuesCueKind.MEMORY
    )
    return CuesSourceCue(
        kind=kind,
        start_sample=float(c.start_sample),
        end_sample=None if c.end_sample is None else float(c.end_sample),
        hot_slot=c.hot_slot,
        color=color,
        name=c.name,
    )
