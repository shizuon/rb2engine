"""ConversionReport: counters, per-track skips, dropped cues and loops; text + JSON sinks.

Exit-code semantics (applied by cli.py):
  0 — clean (nothing skipped at track level)
  1 — converted with track skips
  2 — fatal: nothing usable written

Default JSON path: ``<drive>/Engine Library/rb2engine-report.json``.
Never the drive root. Cwd fallback when the library is unready/unwritable.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_FILENAME = "rb2engine-report.json"
ENGINE_LIBRARY_DIRNAME = "Engine Library"

# Append-only provenance journal, beside the report under Engine Library/.
# The report is overwritten by every convert — and "re-run convert" is exactly
# the remedy verify prescribes on a staleness finding, so a fixed-name report
# alone would destroy the fingerprint of the suspect bytes at the very moment
# it matters (this already happened once; the incident evidence is gone).
# One JSONL line per publish survives any number of re-runs.
JOURNAL_FILENAME = "rb2engine-journal.jsonl"

# Cap so the journal cannot grow without bound on the user's stick. Oldest
# lines are dropped at publish; ~200 bytes/line leaves room for ~300 publishes.
JOURNAL_MAX_BYTES = 64 * 1024

# JSON Schema for the conversion report contract (also the source of
# tests/fixtures/report.schema.json once the lead copies it there).
REPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://rb2engine.local/report.schema.json",
    "title": "rb2engine ConversionReport",
    "type": "object",
    "required": [
        "counters",
        "skipped_tracks",
        "dropped_cues",
        "dropped_loops",
    ],
    "additionalProperties": True,
    "properties": {
        "counters": {
            "type": "object",
            "required": [
                "tracks_read",
                "tracks_converted",
                "tracks_skipped",
                "playlists_read",
                "playlists_converted",
                "cues_dropped",
                "loops_dropped",
                "tracks_unresolvable_paths",
                "tracks_no_anlz",
                "artwork_found",
                "artwork_missing",
                "artwork_deduped",
                "unknown_tag_encounters",
                "unknown_page_type_encounters",
                "export_ext_present",
            ],
            "properties": {
                "tracks_read": {"type": "integer", "minimum": 0},
                "tracks_converted": {"type": "integer", "minimum": 0},
                "tracks_skipped": {"type": "integer", "minimum": 0},
                "playlists_read": {"type": "integer", "minimum": 0},
                "playlists_converted": {"type": "integer", "minimum": 0},
                "cues_dropped": {"type": "integer", "minimum": 0},
                "loops_dropped": {"type": "integer", "minimum": 0},
                "tracks_unresolvable_paths": {"type": "integer", "minimum": 0},
                "tracks_no_anlz": {"type": "integer", "minimum": 0},
                "artwork_found": {"type": "integer", "minimum": 0},
                "artwork_missing": {"type": "integer", "minimum": 0},
                "artwork_deduped": {"type": "integer", "minimum": 0},
                "unknown_tag_encounters": {"type": "integer", "minimum": 0},
                "unknown_page_type_encounters": {"type": "integer", "minimum": 0},
                "export_ext_present": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        "skipped_tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["track_id", "reason_code", "message"],
                "properties": {
                    "track_id": {"type": "integer"},
                    "reason_code": {"type": "string", "minLength": 1},
                    "message": {"type": "string"},
                    "title": {"type": ["string", "null"]},
                },
                "additionalProperties": True,
            },
        },
        "dropped_cues": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["reason_code", "start_sample"],
                    "properties": {
                        "reason_code": {"type": "string", "minLength": 1},
                        "start_sample": {"type": "integer"},
                        "end_sample": {"type": ["integer", "null"]},
                        "name": {"type": ["string", "null"]},
                        "hot_slot": {"type": ["integer", "null"]},
                    },
                    "additionalProperties": True,
                },
            },
        },
        "dropped_loops": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["reason_code", "start_sample", "end_sample"],
                    "properties": {
                        "reason_code": {"type": "string", "minLength": 1},
                        "start_sample": {"type": "integer"},
                        "end_sample": {"type": ["integer", "null"]},
                        "name": {"type": ["string", "null"]},
                    },
                    "additionalProperties": True,
                },
            },
        },
        "path_base": {"type": ["string", "null"]},
        "fatal": {"type": "boolean"},
        "fatal_message": {"type": ["string", "null"]},
        # Optional (not in "required"): reports from < 0.5 have no provenance,
        # and they must keep validating — verify degrades that to a finding.
        "provenance": {
            "type": ["object", "null"],
            "required": [
                "pdb_sha256",
                "pdb_size",
                "pdb_mtime",
                "m_db_sha256",
                "max_playlist_id",
                "max_playlist_entity_id",
            ],
            "properties": {
                "pdb_sha256": {"type": "string", "minLength": 1},
                "pdb_size": {"type": "integer", "minimum": 0},
                "pdb_mtime": {"type": "number"},
                "m_db_sha256": {"type": "string", "minLength": 1},
                "max_playlist_id": {"type": "integer", "minimum": 0},
                "max_playlist_entity_id": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": True,
        },
    },
}


@dataclass
class ProvenanceRecord:
    """What this publish was built from, and what it produced.

    Captured by the writer at publish time (evidence at write time beats
    forensics after the fact) but deliberately time-free: build_library must
    stay deterministic, so the wall-clock timestamp is added only where the
    journal line is written — in this module, outside the writer/mapper
    determinism boundary that test_no_wallclock.py enforces.

    The watermarks are the writer's dense 1..N id allocation ceilings; a later
    row above them was not written by us (external-edit classification, W3).
    """

    pdb_sha256: str
    pdb_size: int
    pdb_mtime: float
    m_db_sha256: str  # staged m.db, hashed before it crossed to the stick
    max_playlist_id: int
    max_playlist_entity_id: int
    key_analysis_sha256: str | None = None


@dataclass
class ReportCounters:
    tracks_read: int = 0
    tracks_converted: int = 0
    tracks_skipped: int = 0
    playlists_read: int = 0
    playlists_converted: int = 0
    cues_dropped: int = 0
    loops_dropped: int = 0
    tracks_unresolvable_paths: int = 0
    tracks_no_anlz: int = 0
    artwork_found: int = 0
    artwork_missing: int = 0
    artwork_deduped: int = 0
    unknown_tag_encounters: int = 0
    unknown_page_type_encounters: int = 0
    export_ext_present: bool = False


@dataclass
class SkipRecord:
    track_id: int
    reason_code: str
    message: str
    title: str | None = None


@dataclass
class DroppedCueRecord:
    reason_code: str
    start_sample: int
    end_sample: int | None = None
    name: str | None = None
    hot_slot: int | None = None


@dataclass
class DroppedLoopRecord:
    reason_code: str
    start_sample: int
    end_sample: int | None = None
    name: str | None = None


@dataclass
class ConversionReport:
    """Accumulates conversion outcomes for human text + machine JSON sinks."""

    counters: ReportCounters = field(default_factory=ReportCounters)
    skipped_tracks: list[SkipRecord] = field(default_factory=list)
    # track_id (str keys in JSON) → list of dropped items
    dropped_cues: dict[int, list[DroppedCueRecord]] = field(default_factory=dict)
    dropped_loops: dict[int, list[DroppedLoopRecord]] = field(default_factory=dict)
    path_base: str | None = None
    fatal: bool = False
    fatal_message: str | None = None
    # Set by build_library on a successful publish (None when the source had no
    # fingerprint, e.g. libraries not parsed from a pdb, or on fatal runs).
    provenance: ProvenanceRecord | None = None
    key_analysis: dict[str, Any] | None = None

    def add_skip(
        self,
        *,
        track_id: int,
        reason_code: str,
        message: str,
        title: str | None = None,
    ) -> None:
        self.skipped_tracks.append(
            SkipRecord(
                track_id=track_id,
                reason_code=reason_code,
                message=message,
                title=title,
            )
        )
        self.counters.tracks_skipped = len(self.skipped_tracks)

    def add_dropped_cue(
        self,
        *,
        track_id: int,
        reason_code: str,
        start_sample: int,
        end_sample: int | None = None,
        name: str | None = None,
        hot_slot: int | None = None,
    ) -> None:
        rec = DroppedCueRecord(
            reason_code=reason_code,
            start_sample=start_sample,
            end_sample=end_sample,
            name=name,
            hot_slot=hot_slot,
        )
        self.dropped_cues.setdefault(track_id, []).append(rec)
        self.counters.cues_dropped = sum(len(v) for v in self.dropped_cues.values())

    def add_dropped_loop(
        self,
        *,
        track_id: int,
        reason_code: str,
        start_sample: int,
        end_sample: int | None = None,
        name: str | None = None,
    ) -> None:
        rec = DroppedLoopRecord(
            reason_code=reason_code,
            start_sample=start_sample,
            end_sample=end_sample,
            name=name,
        )
        self.dropped_loops.setdefault(track_id, []).append(rec)
        self.counters.loops_dropped = sum(len(v) for v in self.dropped_loops.values())

    def mark_fatal(self, message: str) -> None:
        self.fatal = True
        self.fatal_message = message

    def to_json_obj(self) -> dict[str, Any]:
        """Machine-readable report matching REPORT_SCHEMA."""
        return {
            "counters": asdict(self.counters),
            "skipped_tracks": [asdict(s) for s in self.skipped_tracks],
            "dropped_cues": {
                str(tid): [asdict(c) for c in cues]
                for tid, cues in sorted(self.dropped_cues.items())
            },
            "dropped_loops": {
                str(tid): [asdict(loop) for loop in loops]
                for tid, loops in sorted(self.dropped_loops.items())
            },
            "path_base": self.path_base,
            "key_analysis": self.key_analysis,
            "fatal": self.fatal,
            "fatal_message": self.fatal_message,
            "provenance": (
                None if self.provenance is None else asdict(self.provenance)
            ),
        }

    def write_json(self, path: Path) -> Path:
        """Write schema-shaped JSON to *path*. Creates parent dirs as needed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_json_obj(), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
        return path

    def render_text(self) -> str:
        """Plain-text human summary (stdout sink; rich may style this further)."""
        c = self.counters
        lines = [
            "rb2engine conversion report",
            "---------------------------",
            f"Tracks read:       {c.tracks_read}",
            f"Tracks converted:  {c.tracks_converted}",
            f"Tracks skipped:    {c.tracks_skipped}",
            f"Playlists read:    {c.playlists_read}",
            f"Playlists converted: {c.playlists_converted}",
            f"Cues dropped:      {c.cues_dropped}",
            f"Loops dropped:     {c.loops_dropped}",
            f"Unresolvable paths:{c.tracks_unresolvable_paths}",
            f"Tracks with no ANLZ: {c.tracks_no_anlz}",
            f"Artwork found:     {c.artwork_found}",
            f"Artwork missing:   {c.artwork_missing}",
            f"Artwork deduped:   {c.artwork_deduped}",
            f"Unknown tags:      {c.unknown_tag_encounters}",
            f"Unknown page types:{c.unknown_page_type_encounters}",
            f"exportExt.pdb:     {'yes' if c.export_ext_present else 'no'}",
        ]
        if self.path_base is not None:
            lines.append(f"path_base:         {self.path_base}")
        if self.fatal:
            lines.append(f"FATAL: {self.fatal_message}")
        if self.skipped_tracks:
            lines.append("")
            lines.append("Skipped tracks:")
            for s in self.skipped_tracks:
                title = f" ({s.title})" if s.title else ""
                lines.append(
                    f"  track {s.track_id}{title}: [{s.reason_code}] {s.message}"
                )
        if self.dropped_cues:
            lines.append("")
            lines.append("Dropped cues:")
            for tid, cues in sorted(self.dropped_cues.items()):
                for cue in cues:
                    lines.append(
                        f"  track {tid}: [{cue.reason_code}] "
                        f"start={cue.start_sample} name={cue.name!r}"
                    )
        if self.dropped_loops:
            lines.append("")
            lines.append("Dropped loops:")
            for tid, loops in sorted(self.dropped_loops.items()):
                for loop in loops:
                    lines.append(
                        f"  track {tid}: [{loop.reason_code}] "
                        f"start={loop.start_sample} end={loop.end_sample} "
                        f"name={loop.name!r}"
                    )
        return "\n".join(lines) + "\n"

    def print_human(self) -> None:
        """Emit the human report to stdout via rich when available."""
        text = self.render_text()
        try:
            from rich.console import Console

            Console().print(text, end="")
        except (OSError, TypeError, ValueError):
            print(text, end="")


def exit_code_for(report: ConversionReport) -> int:
    """Map report state to process exit code: 0 clean, 1 skips, 2 fatal."""
    if report.fatal:
        return 2
    if report.skipped_tracks or report.counters.tracks_skipped > 0:
        return 1
    return 0


def resolve_report_path(
    drive: Path | None,
    *,
    override: Path | None = None,
    library_ready: bool = True,
) -> Path:
    """Choose where to write the machine JSON report.

    Prefer ``<drive>/Engine Library/rb2engine-report.json`` when the library
    exists and is writable. Never write to the drive root. Fall back to
    ``./rb2engine-report.json`` in the cwd when the library is not ready,
    missing, or unwritable (fatal runs, early failure).
    """
    if override is not None:
        return Path(override)

    if drive is not None and library_ready:
        eng = Path(drive) / ENGINE_LIBRARY_DIRNAME
        if eng.is_dir() and os.access(eng, os.W_OK):
            return eng / REPORT_FILENAME

    return Path.cwd() / REPORT_FILENAME


def append_journal(
    engine_lib: Path,
    record: ProvenanceRecord,
    *,
    timestamp: str | None = None,
) -> Path:
    """Append one publish line to ``Engine Library/rb2engine-journal.jsonl``.

    Called from the CLI layer after a successful publish — never from writer/,
    because the line carries a wall-clock timestamp and writer/ is under the
    no-wallclock determinism gate. The m.db itself stays byte-identical across
    rebuilds; only this journal varies.

    Capped at JOURNAL_MAX_BYTES by dropping the OLDEST lines: the newest line
    is the one verify needs, the tail is lineage. The new line is always kept
    even in the pathological case where it alone exceeds the cap.

    *timestamp* is injectable for tests; production callers omit it.
    """
    engine_lib = Path(engine_lib)
    path = engine_lib / JOURNAL_FILENAME
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = (
        json.dumps(
            {"timestamp": timestamp, **asdict(record)}, ensure_ascii=False
        )
        + "\n"
    )

    existing = b""
    if path.exists():
        existing = path.read_bytes()
        if existing and not existing.endswith(b"\n"):
            # A truncated final line (interrupted write) must not be glued to
            # the front of the new record and corrupt it too.
            existing += b"\n"
    content = existing + line.encode("utf-8")
    if len(content) > JOURNAL_MAX_BYTES:
        kept = content.split(b"\n")
        # Drop oldest complete lines until the remainder fits, but never the
        # line we just appended (kept[-2]; kept[-1] is the trailing empty str).
        while len(kept) > 2 and len(b"\n".join(kept)) > JOURNAL_MAX_BYTES:
            kept.pop(0)
        content = b"\n".join(kept)
    path.write_bytes(content)
    return path


def read_last_journal_entry(engine_lib: Path) -> dict[str, Any] | None:
    """Most recent publish record from the journal, or None when absent/empty.

    Raises ValueError on a journal that exists but holds no parseable final
    line — verify turns that into a visible finding rather than a silent OK.
    """
    path = Path(engine_lib) / JOURNAL_FILENAME
    if not path.is_file():
        return None
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        obj = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"unparseable last line in {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"last line in {path} is not a JSON object")
    return obj


def validate_report(obj: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
    """Validate *obj* against REPORT_SCHEMA (minimal subset — no jsonschema dep).

    Raises ValueError with a path message on the first structural failure.
    """
    schema = schema if schema is not None else REPORT_SCHEMA
    _validate(obj, schema, path="$")


def _validate(instance: Any, schema: dict[str, Any], *, path: str) -> None:
    expected_type = schema.get("type")
    if expected_type is not None:
        _check_type(instance, expected_type, path=path)

    if isinstance(instance, dict) and "properties" in schema:
        props = schema["properties"]
        for key in schema.get("required", []):
            if key not in instance:
                raise ValueError(f"missing required property {key!r} at {path}")
        for key, value in instance.items():
            if key in props:
                _validate(value, props[key], path=f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"unexpected property {key!r} at {path}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate(
                    value,
                    schema["additionalProperties"],
                    path=f"{path}.{key}",
                )

    if isinstance(instance, list) and "items" in schema:
        item_schema = schema["items"]
        for i, item in enumerate(instance):
            _validate(item, item_schema, path=f"{path}[{i}]")

    if (
        "minimum" in schema
        and isinstance(instance, (int, float))
        and instance < schema["minimum"]
    ):
        raise ValueError(f"{path} = {instance} below minimum {schema['minimum']}")

    if (
        "minLength" in schema
        and isinstance(instance, str)
        and len(instance) < schema["minLength"]
    ):
        raise ValueError(f"{path} shorter than minLength {schema['minLength']}")


def _check_type(instance: Any, expected: Any, *, path: str) -> None:
    if isinstance(expected, list):
        for alt in expected:
            try:
                _check_type(instance, alt, path=path)
                return
            except ValueError:
                continue
        raise ValueError(f"{path}: expected one of {expected}, got {type(instance).__name__}")
    if expected == "object":
        if not isinstance(instance, dict):
            raise ValueError(f"{path}: expected object, got {type(instance).__name__}")
    elif expected == "array":
        if not isinstance(instance, list):
            raise ValueError(f"{path}: expected array, got {type(instance).__name__}")
    elif expected == "integer":
        # bool is a subclass of int in Python — reject it
        if isinstance(instance, bool) or not isinstance(instance, int):
            raise ValueError(f"{path}: expected integer, got {type(instance).__name__}")
    elif expected == "boolean":
        if not isinstance(instance, bool):
            raise ValueError(f"{path}: expected boolean, got {type(instance).__name__}")
    elif expected == "string":
        if not isinstance(instance, str):
            raise ValueError(f"{path}: expected string, got {type(instance).__name__}")
    elif expected == "null" and instance is not None:
        # Safe to collapse: the dispatch chain has no trailing else, so a
        # non-match here cannot fall through into another type's check.
        raise ValueError(f"{path}: expected null, got {type(instance).__name__}")
    elif expected == "number" and (
        isinstance(instance, bool) or not isinstance(instance, (int, float))
    ):
        raise ValueError(f"{path}: expected number, got {type(instance).__name__}")
