"""Source-side intermediate representation and JSON canonicalization boundary.

Produced by reader/; consumed by mapper/. No rekordbox type and no Engine type
crosses this module — plain pathlib.Path is explicitly permitted.

All cue/beat positions are integer sample counts. Milliseconds are converted
exactly once in the reader via units.ms_to_samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class CueKind(Enum):
    HOT = "HOT"
    MEMORY = "MEMORY"


@dataclass(frozen=True, slots=True)
class RGB:
    """0–255 per channel."""

    r: int
    g: int
    b: int


@dataclass(frozen=True, slots=True)
class SourceCue:
    kind: CueKind
    hot_slot: int | None  # 1–8 == pads A–H
    start_sample: int
    end_sample: int | None
    color: RGB | None
    name: str | None

    @property
    def is_loop(self) -> bool:
        return self.end_sample is not None


@dataclass(frozen=True, slots=True)
class SourceBeat:
    beat_in_bar: int  # 1–4
    sample_offset: int
    bpm: float


@dataclass(frozen=True, slots=True)
class SourceBeatgrid:
    beats: list[SourceBeat]
    is_adjusted: bool


@dataclass(frozen=True, slots=True)
class SourceArtwork:
    content_key: str  # sha1 hex, leading zeros stripped
    path: Path | None
    source: str  # e.g. "embedded" / "pdb"


@dataclass(frozen=True, slots=True)
class SourceTrack:
    rb_id: int
    title: str
    artist: str
    album: str
    genre: str
    label: str
    comment: str
    composer: str
    remixer: str
    year: int
    track_number: int | None
    disc_number: int | None
    bpm: float
    key_name: str | None
    rating: int
    play_count: int
    bitrate: int
    file_size: int
    file_type: str
    sample_rate: int
    duration_s: int
    total_samples: int | None
    raw_path: str
    resolved_path: Path | None
    beatgrid: SourceBeatgrid | None
    cues: list[SourceCue]
    artwork: SourceArtwork | None
    analyze_path: str | None = None
    """pdb ofs_strings[14] — locates this track's ANLZ files (grid/cues).

    Defaulted and last so existing constructions stay valid; a dataclass field
    with a default may not precede fields without one.
    """
    adjusted_beatgrid: SourceBeatgrid | None = None
    """Optional model-assisted grid; original beatgrid remains the default grid."""


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    """Identity of the export.pdb bytes a parse actually consumed.

    The sha256 is computed over the in-memory buffer the parser read, never by
    re-reading the file: a real incident put torn bytes in front of `convert`
    14 seconds after rekordbox's last write, and the settled file on disk no
    longer contained what was parsed. Re-reading would fingerprint the wrong
    thing — the whole point is to name the bytes that produced this library.

    ``size`` is the buffer length (same reasoning); ``mtime`` is corroborating
    filesystem metadata only, untrustworthy on removable exFAT.
    """

    sha256: str
    size: int
    mtime: float


@dataclass(frozen=True, slots=True)
class SourcePlaylist:
    rb_id: int
    parent_rb_id: int  # 0 = root
    name: str
    sort_order: int
    is_folder: bool
    track_rb_ids: list[int]  # order preserved


@dataclass(frozen=True, slots=True)
class SourceLibrary:
    drive_root: Path
    tracks: dict[int, SourceTrack]
    playlists: list[SourcePlaylist]
    warnings: list[str]
    fingerprint: SourceFingerprint | None = None
    """Reader-owned provenance; None for libraries not parsed from a pdb.

    Defaulted so the many construction sites (tests, filters) stay valid.
    Deliberately NOT serialized by ``to_json_obj``: that output is load-bearing
    for golden byte-identity, and a per-source hash would break every golden.
    """

    def to_json_obj(self) -> dict[str, Any]:
        """Canonical JSON structure for `rb2engine inspect --json` / golden_ir.json.

        Cross-platform rules (load-bearing for golden byte-identity):
        - drive_root → literal \"<drive_root>\"
        - paths under drive_root → drive-relative POSIX strings
        - paths outside drive_root → \"<external>\" (resolver-bug canary)
        - key order is deterministic (fixed field order, not host-dependent)
        - ``fingerprint`` is excluded: it varies per source file, so including
          it would break golden byte-identity on every re-export
        """
        root = self.drive_root.resolve()
        # Track dict keys as strings, sorted by int id for stable map order.
        tracks_obj: dict[str, Any] = {}
        for rb_id in sorted(self.tracks.keys()):
            tracks_obj[str(rb_id)] = _track_to_json(self.tracks[rb_id], root)

        return {
            "drive_root": "<drive_root>",
            "tracks": tracks_obj,
            "playlists": [_playlist_to_json(p) for p in self.playlists],
            "warnings": list(self.warnings),
        }


def _canon_path(path: Path | None, drive_root: Path) -> str | None:
    """Emit drive-relative POSIX, \"<external>\", or null — never absolute/OS-native."""
    if path is None:
        return None
    try:
        rel = path.resolve().relative_to(drive_root)
    except ValueError:
        return "<external>"
    # PurePosixPath semantics via as_posix(): always forward slashes.
    return rel.as_posix()


def _rgb_to_json(color: RGB | None) -> dict[str, int] | None:
    if color is None:
        return None
    return {"r": color.r, "g": color.g, "b": color.b}


def _cue_to_json(cue: SourceCue) -> dict[str, Any]:
    return {
        "kind": cue.kind.value,
        "hot_slot": cue.hot_slot,
        "start_sample": cue.start_sample,
        "end_sample": cue.end_sample,
        "color": _rgb_to_json(cue.color),
        "name": cue.name,
    }


def _beat_to_json(beat: SourceBeat) -> dict[str, Any]:
    return {
        "beat_in_bar": beat.beat_in_bar,
        "sample_offset": beat.sample_offset,
        "bpm": beat.bpm,
    }


def _beatgrid_to_json(grid: SourceBeatgrid | None) -> dict[str, Any] | None:
    if grid is None:
        return None
    return {
        "beats": [_beat_to_json(b) for b in grid.beats],
        "is_adjusted": grid.is_adjusted,
    }


def _artwork_to_json(art: SourceArtwork | None, drive_root: Path) -> dict[str, Any] | None:
    if art is None:
        return None
    return {
        "content_key": art.content_key,
        "path": _canon_path(art.path, drive_root),
        "source": art.source,
    }


def _track_to_json(track: SourceTrack, drive_root: Path) -> dict[str, Any]:
    # Field order is the serialization contract (deterministic key order).
    return {
        "rb_id": track.rb_id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "genre": track.genre,
        "label": track.label,
        "comment": track.comment,
        "composer": track.composer,
        "remixer": track.remixer,
        "year": track.year,
        "track_number": track.track_number,
        "disc_number": track.disc_number,
        "bpm": track.bpm,
        "key_name": track.key_name,
        "rating": track.rating,
        "play_count": track.play_count,
        "bitrate": track.bitrate,
        "file_size": track.file_size,
        "file_type": track.file_type,
        "sample_rate": track.sample_rate,
        "duration_s": track.duration_s,
        "total_samples": track.total_samples,
        "raw_path": track.raw_path,
        "resolved_path": _canon_path(track.resolved_path, drive_root),
        "analyze_path": track.analyze_path,
        "beatgrid": _beatgrid_to_json(track.beatgrid),
        "cues": [_cue_to_json(c) for c in track.cues],
        "artwork": _artwork_to_json(track.artwork, drive_root),
    }


def _playlist_to_json(pl: SourcePlaylist) -> dict[str, Any]:
    return {
        "rb_id": pl.rb_id,
        "parent_rb_id": pl.parent_rb_id,
        "name": pl.name,
        "sort_order": pl.sort_order,
        "is_folder": pl.is_folder,
        "track_rb_ids": list(pl.track_rb_ids),
    }
