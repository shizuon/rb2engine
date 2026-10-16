"""Mechanical fidelity check: decode written m.db and diff against source IR.

``verify_library`` re-reads the source stick, re-maps each track through the
same mapper path the writer used, decodes PerformanceData blobs with the
golden-verified codecs, and reports per-field discrepancies at sample
granularity. A verifier that cannot fail is worthless — unit tests deliberately
corrupt m.db to prove each check fires.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rb2engine.errors import FatalError
from rb2engine.ir import SourceFingerprint, SourceLibrary, SourceTrack
from rb2engine.ir_engine import artwork_content_hash
from rb2engine.mapper.track import map_track
from rb2engine.playlist_check import CHAIN, compare_playlists, db_playlist_paths
from rb2engine.playlist_naming import format_path, resolve_paths
from rb2engine.reader.library import read_library
from rb2engine.report import (
    JOURNAL_FILENAME,
    REPORT_FILENAME,
    read_last_journal_entry,
)
from rb2engine.writer.blobs import (
    decode_beat_data,
    decode_loops,
    decode_quick_cues,
    decode_track_data,
)
from rb2engine.writer.database import detect_schema
from rb2engine.writer.playlists import PINNED_LAST_EDIT_TIME

ENGINE_LIBRARY_DIRNAME = "Engine Library"
DATABASE2_DIRNAME = "Database2"
M_DB_NAME = "m.db"

# Empty-slot sentinel shared with writer/blobs and ir_engine.
_EMPTY_SAMPLE = -1.0


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One field-level mismatch between source expectation and written m.db."""

    track_id: int | None  # SourceTrack.rb_id; None for library-level checks
    field: str
    expected: Any
    actual: Any
    source_independent: bool = False
    """True when the finding needs no source oracle to be a defect.

    A broken nextEntityId chain or an undecodable blob is wrong no matter what
    export.pdb says today, so these findings survive a stale-source
    (fingerprint-mismatch) situation and keep forcing exit 1. Everything else
    is a comparison against the parsed source and becomes unattributable when
    that source is not the one the m.db was built from.
    """


@dataclass(frozen=True, slots=True)
class ProvenanceFinding:
    """One top-level provenance observation, rendered before discrepancies.

    Separate from Discrepancy on purpose: a stale source is not a corrupt
    database, and the incident this exists for was verify blaming the wrong
    oracle. ``code`` is machine-stable; ``message`` carries the remedy.
    """

    code: str  # "source_changed" | "db_changed" | "provenance_missing" | "journal_unreadable"
    message: str


@dataclass(frozen=True, slots=True)
class ExternalEdit:
    """A playlist Engine DJ added to the database after our conversion.

    Classified by ``lastEditTime``: the writer pins every playlist row it
    creates to ``PINNED_LAST_EDIT_TIME``, so a row carrying any other value was
    not written by rb2engine. Observed on real hardware (2026-07-31): opening
    Engine DJ with a populated desktop library merged 3 playlists onto a
    freshly-converted stick, each with a real edit timestamp.

    ``beyond_watermark`` is corroboration only, never the classifier: the same
    experiment showed Engine *reassigns* Playlist ids across a merge (ids ran
    to 84 against our contiguous 1..45), so an id above our allocation can also
    be one of our own rows renumbered.
    """

    label: str
    playlist_id: int
    last_edit_time: str
    beyond_watermark: bool


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of ``verify_library``.

    Counts are per-track: a track is mismatched if it contributed ≥1
    discrepancy. Library-level discrepancies (playlist count/order, artwork)
    set ``ok`` False without necessarily inflating track counts.

    ``external_edits`` is informational and deliberately excluded from ``ok``:
    an Engine desktop-library merge is a legitimate Engine feature, not a
    conversion defect, so it must not fail verification (CLI exit stays 0).
    It also stays informational because an Engine in-place migration that
    rewrote every row's lastEditTime would otherwise mass-report.
    """

    checked: int
    matched: int
    mismatched: int
    discrepancies: list[Discrepancy] = field(default_factory=list)
    provenance_findings: list[ProvenanceFinding] = field(default_factory=list)
    external_edits: list[ExternalEdit] = field(default_factory=list)
    source_changed: bool = False
    """Recorded pdb fingerprint does not match the source parsed this run."""
    db_changed: bool = False
    """m.db bytes differ from the recorded publish (Engine rewrites are legit)."""
    provenance_missing: bool = False
    """No usable provenance record (pre-0.5 m.db, or the record never landed)."""

    @property
    def ok(self) -> bool:
        return len(self.discrepancies) == 0

    @property
    def exit_code(self) -> int:
        """0 ok / 1 discrepancies / 3 not-attributable. (2 = cannot-verify,
        raised as FatalError before a result exists, so it keeps precedence.)

        Partition: source-INDEPENDENT findings (chain breaks, undecodable
        blobs) are defects regardless of which export.pdb is present, so they
        always win — a stale source must never launder a real fault into
        "re-run convert". Source-DEPENDENT comparisons under a fingerprint
        mismatch have no oracle and become informational (exit 3).
        """
        if any(d.source_independent for d in self.discrepancies):
            return 1
        if self.source_changed:
            return 3
        return 1 if self.discrepancies else 0

    def render_text(self) -> str:
        """Human-readable summary for the ``verify`` command.

        ``convert`` does not render this. It runs its own playlist-scoped
        recheck before publishing (``playlist_check.compare_playlists``) and
        refuses rather than reports; a full field-level verify stays an explicit
        second step.
        """
        code = self.exit_code
        if code == 0:
            status = "OK"
        elif code == 3:
            status = "NOT ATTRIBUTABLE (re-run convert)"
        else:
            status = "FAILED"
        lines = [
            "rb2engine verify",
            "---------------",
            f"Status:      {status}",
            f"Checked:     {self.checked}",
            f"Matched:     {self.matched}",
            f"Mismatched:  {self.mismatched}",
            f"Discrepancies: {len(self.discrepancies)}",
        ]
        if self.provenance_findings:
            lines.append("")
            lines.append("Provenance:")
            for p in self.provenance_findings:
                lines.append(f"  [{p.code}] {p.message}")
        if self.discrepancies:
            lines.append("")
            lines.append("Discrepancies:")
            if code == 3:
                lines.append(
                    "  (informational — the source is not the one this m.db "
                    "was built from, so the comparisons below are not "
                    "attributable to either side; re-run convert)"
                )
            for d in self.discrepancies:
                tid = "library" if d.track_id is None else str(d.track_id)
                lines.append(
                    f"  track {tid}: {d.field}: "
                    f"expected={d.expected!r} actual={d.actual!r}"
                )
        if self.external_edits:
            # Wording kept in step with docs/TROUBLESHOOTING.md ("`verify`
            # reports extra playlists after you opened Engine DJ") — verify
            # must tell the same story the docs already do.
            lines.append("")
            lines.append("External edits (informational, from Engine DJ):")
            for e in self.external_edits:
                extra = (
                    ", id beyond this conversion's allocation"
                    if e.beyond_watermark
                    else ""
                )
                lines.append(
                    f"  playlist {e.label!r} (Playlist.id {e.playlist_id}, "
                    f"lastEditTime {e.last_edit_time!r}{extra})"
                )
            lines.extend(
                [
                    "  rb2engine pins lastEditTime to "
                    f"{PINNED_LAST_EDIT_TIME!r} on every playlist it writes; "
                    "these rows carry other values, so Engine DJ added them",
                    "  (typically by merging its desktop library onto the "
                    "stick). Your conversion is not corrupt. Re-run convert "
                    "to make the stick match rekordbox exactly.",
                ]
            )
        return "\n".join(lines) + "\n"


def verify_library(
    drive_root: Path,
    *,
    with_artwork: bool = True,
    sample: int | None = None,
) -> VerifyResult:
    """Decode written m.db and diff against a fresh parse of the source stick.

    Parameters
    ----------
    drive_root:
        Stick mount point containing both the source (PIONEER/export.pdb,
        Contents/) and the written ``Engine Library/Database2/m.db``.
    with_artwork:
        Forwarded to ``read_library``; when True, also compare AlbumArt row
        count to unique extracted artwork keys.
    sample:
        If set, only the first N source tracks (sorted by rb_id) are checked.
        Full-library verify over USB on ~3,600 tracks is slow.
    """
    drive_root = Path(drive_root)
    m_db_path = drive_root / ENGINE_LIBRARY_DIRNAME / DATABASE2_DIRNAME / M_DB_NAME
    if not m_db_path.is_file():
        raise FatalError(f"no m.db to verify at {m_db_path}")

    # Schema probe — confirms m.db is a readable Engine library with a
    # supported schema. Reading rows is schema-agnostic, but an unknown or
    # unreadable schema must not report a false OK (CLI exit 2).
    schema = detect_schema(m_db_path)
    if schema is None:
        raise FatalError(f"unreadable or non-Engine m.db at {m_db_path}")
    from rb2engine.writer.schema import resolve_schema

    resolve_schema(schema)

    source = read_library(drive_root, with_anlz=True, with_artwork=with_artwork)
    engine_lib = drive_root / ENGINE_LIBRARY_DIRNAME
    recorded, _ = _load_recorded_provenance(engine_lib)
    # Predictions are an additional input, never an exemption from key checks.
    # Replay only a manifest paired to this source and hashed at publication.
    if (recorded and recorded.get("key_analysis_sha256")
            and source.fingerprint is not None
            and source.fingerprint.sha256 == recorded.get("pdb_sha256")):
        from rb2engine.keyscan import digest_record, replay_keys

        try:
            payload = json.loads((engine_lib / REPORT_FILENAME).read_text(encoding="utf-8"))
            analysis = payload.get("key_analysis") if isinstance(payload, dict) else None
            if (not isinstance(analysis, dict)
                    or digest_record(analysis) != recorded["key_analysis_sha256"]):
                raise FatalError("OpenKeyScan analysis record is missing or changed; cannot verify")
            source = replay_keys(source, analysis)
        except (OSError, ValueError) as exc:
            raise FatalError(f"Cannot read OpenKeyScan verification record: {exc}") from exc

    if (recorded and recorded.get("grid_repair_sha256")
            and source.fingerprint is not None
            and source.fingerprint.sha256 == recorded.get("pdb_sha256")):
        from rb2engine.gridrepair.repair import replay_repairs
        from rb2engine.keyscan import digest_record

        try:
            payload = json.loads((engine_lib / REPORT_FILENAME).read_text(encoding="utf-8"))
            repair = payload.get("grid_repair") if isinstance(payload, dict) else None
            if (not isinstance(repair, dict)
                    or digest_record(repair) != recorded["grid_repair_sha256"]):
                raise FatalError("Grid repair record is missing or changed; cannot verify")
            source = replay_repairs(source, repair)
        except (OSError, ValueError) as exc:
            raise FatalError(f"Cannot read grid repair verification record: {exc}") from exc

    # Hash the m.db BEFORE decoding it: this is the "which oracle moved"
    # question, and it must be answered against the same bytes we then verify.
    with m_db_path.open("rb") as fh:
        m_db_sha256 = hashlib.file_digest(fh, "sha256").hexdigest()

    conn = sqlite3.connect(m_db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = _verify_against_open_db(
            source,
            conn,
            drive_root=drive_root,
            engine_lib=engine_lib,
            with_artwork=with_artwork,
            sample=sample,
        )
    finally:
        conn.close()

    findings, source_changed, db_changed, missing = _assess_provenance(
        engine_lib,
        source_fingerprint=source.fingerprint,
        m_db_sha256=m_db_sha256,
    )
    return dataclasses.replace(
        result,
        provenance_findings=findings,
        source_changed=source_changed,
        db_changed=db_changed,
        provenance_missing=missing,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


# Wording shared by every not-attributable path. The residue is irreducible:
# when the source has moved, source-dependent corruption in the m.db is
# indistinguishable in principle from the source change itself until convert
# re-runs and re-pairs the oracles.
_RESIDUE = (
    "Source-dependent comparisons are not attributable while the source and "
    "database are unpaired; corruption co-occurring with a changed source is "
    "undetectable until convert re-runs."
)


def _load_recorded_provenance(
    engine_lib: Path,
) -> tuple[dict[str, Any] | None, list[ProvenanceFinding]]:
    """Last recorded publish, from the journal (authority) else the report.

    Never raises: a stick we cannot read provenance from must degrade to a
    visible finding, not a crash — a 0.4.0 m.db has no record at all.
    """
    findings: list[ProvenanceFinding] = []
    try:
        entry = read_last_journal_entry(engine_lib)
    except (OSError, ValueError) as exc:
        findings.append(
            ProvenanceFinding(
                "journal_unreadable",
                f"provenance journal exists but is unreadable ({exc}); "
                "falling back to the report",
            )
        )
        entry = None
    if entry is not None and isinstance(entry.get("pdb_sha256"), str):
        return entry, findings

    # Fallback witness: the report JSON's provenance block (same publish, but
    # overwritten by every convert — the journal exists precisely because
    # "re-run convert" is the prescribed remedy and would shred it).
    report_path = engine_lib / REPORT_FILENAME
    if report_path.is_file():
        try:
            obj = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            obj = None
        if isinstance(obj, dict):
            prov = obj.get("provenance")
            if isinstance(prov, dict) and isinstance(prov.get("pdb_sha256"), str):
                return prov, findings
    return None, findings


def _assess_provenance(
    engine_lib: Path,
    *,
    source_fingerprint: SourceFingerprint | None,
    m_db_sha256: str,
) -> tuple[list[ProvenanceFinding], bool, bool, bool]:
    """Compare the recorded publish against what this run can see.

    Returns ``(findings, source_changed, db_changed, provenance_missing)`` —
    the attribution matrix: which oracle moved, or that we cannot know.
    """
    recorded, findings = _load_recorded_provenance(engine_lib)

    if recorded is None:
        findings.append(
            ProvenanceFinding(
                "provenance_missing",
                "no provenance record on this stick (no "
                f"{JOURNAL_FILENAME} and no provenance in {REPORT_FILENAME}): "
                "the m.db predates provenance tracking or the record was "
                "written off-stick. Discrepancies below, if any, cannot be "
                "attributed to source vs database; re-run convert to record "
                "provenance.",
            )
        )
        return findings, False, False, True

    source_changed = False
    if source_fingerprint is None:
        # Only reachable when the source parse carried no fingerprint (test
        # doubles / non-pdb libraries) — without it the recorded hash has
        # nothing to be compared against, which is the missing case.
        findings.append(
            ProvenanceFinding(
                "provenance_missing",
                "the parsed source carries no fingerprint, so the recorded "
                "publish cannot be compared against it.",
            )
        )
        return findings, False, False, True

    if recorded["pdb_sha256"] != source_fingerprint.sha256:
        source_changed = True
        findings.append(
            ProvenanceFinding(
                "source_changed",
                "m.db was built from a different export.pdb than the one on "
                f"this stick (recorded sha256 {recorded['pdb_sha256'][:12]}…, "
                f"current {source_fingerprint.sha256[:12]}…). This is a stale "
                "or re-exported source, not database corruption; re-run "
                f"convert. {_RESIDUE}",
            )
        )

    db_changed = False
    recorded_db = recorded.get("m_db_sha256")
    if isinstance(recorded_db, str) and recorded_db != m_db_sha256:
        db_changed = True
        findings.append(
            ProvenanceFinding(
                "db_changed",
                "m.db has changed since rb2engine published it (recorded "
                f"sha256 {recorded_db[:12]}…, current {m_db_sha256[:12]}…). "
                "Expected when Engine DJ has opened this drive — it rewrites "
                "and merges its desktop library into m.db. The source "
                "comparison below remains authoritative for what the m.db "
                "holds now.",
            )
        )

    return findings, source_changed, db_changed, False


@dataclass
class _DbTrack:
    id: int
    path: str
    title: str
    artist: str
    album: str
    genre: str
    bpm: int | None
    bpm_analyzed: float | None
    key: int | None
    track_data: bytes | None
    beat_data: bytes | None
    quick_cues: bytes | None
    loops: bytes | None


def _verify_against_open_db(
    source: SourceLibrary,
    conn: sqlite3.Connection,
    *,
    drive_root: Path,
    engine_lib: Path,
    with_artwork: bool,
    sample: int | None,
) -> VerifyResult:
    db_tracks = _load_db_tracks(conn)
    by_path = {t.path: t for t in db_tracks}
    discrepancies: list[Discrepancy] = []
    external_edits: list[ExternalEdit] = []

    rb_ids = sorted(source.tracks.keys())
    if sample is not None:
        if sample < 0:
            raise ValueError(f"sample must be >= 0, got {sample}")
        rb_ids = rb_ids[:sample]

    checked = 0
    matched = 0
    mismatched = 0

    for rb_id in rb_ids:
        src = source.tracks[rb_id]
        checked += 1
        before = len(discrepancies)
        _compare_track(
            src,
            by_path,
            drive_root=drive_root,
            engine_lib=engine_lib,
            discrepancies=discrepancies,
        )
        if len(discrepancies) > before:
            mismatched += 1
        else:
            matched += 1

    _compare_playlists(
        source,
        conn,
        by_path=by_path,
        drive_root=drive_root,
        engine_lib=engine_lib,
        discrepancies=discrepancies,
        external_edits=external_edits,
    )

    if with_artwork:
        _compare_artwork(source, conn, discrepancies=discrepancies)

    return VerifyResult(
        checked=checked,
        matched=matched,
        mismatched=mismatched,
        discrepancies=discrepancies,
        external_edits=external_edits,
    )


def _load_db_tracks(conn: sqlite3.Connection) -> list[_DbTrack]:
    rows = conn.execute(
        """
        SELECT
            t.id, t.path, t.title, t.artist, t.album, t.genre,
            t.bpm, t.bpmAnalyzed, t.key,
            p.trackData, p.beatData, p.quickCues, p.loops
        FROM Track t
        LEFT JOIN PerformanceData p ON p.trackId = t.id
        """
    ).fetchall()
    out: list[_DbTrack] = []
    for r in rows:
        out.append(
            _DbTrack(
                id=int(r[0]),
                path=r[1] or "",
                title=r[2] or "",
                artist=r[3] or "",
                album=r[4] or "",
                genre=r[5] or "",
                bpm=None if r[6] is None else int(r[6]),
                bpm_analyzed=None if r[7] is None else float(r[7]),
                key=None if r[8] is None else int(r[8]),
                track_data=r[9],
                beat_data=r[10],
                quick_cues=r[11],
                loops=r[12],
            )
        )
    return out


def _compare_track(
    src: SourceTrack,
    by_path: dict[str, _DbTrack],
    *,
    drive_root: Path,
    engine_lib: Path,
    discrepancies: list[Discrepancy],
) -> None:
    rb_id = src.rb_id
    expected = map_track(
        src, drive_root=drive_root, engine_library_dir=engine_lib
    )
    db = by_path.get(expected.path)
    if db is None:
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="missing",
                expected="present",
                actual="absent",
            )
        )
        return

    # Path must resolve to an existing audio file relative to Engine Library/.
    resolved = _resolve_track_path(engine_lib, db.path)
    if not resolved.is_file():
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="path_exists",
                expected=True,
                actual=False,
            )
        )

    _eq(rb_id, "title", expected.title, db.title, discrepancies)
    _eq(rb_id, "artist", expected.artist, db.artist, discrepancies)
    _eq(rb_id, "album", expected.album, db.album, discrepancies)
    _eq(rb_id, "genre", expected.genre, db.genre, discrepancies)
    _eq(rb_id, "bpm", int(expected.bpm), db.bpm, discrepancies)
    _eq(
        rb_id,
        "bpm_analyzed",
        float(expected.bpm_analyzed),
        db.bpm_analyzed,
        discrepancies,
    )
    _eq(rb_id, "key", expected.key, db.key, discrepancies)

    # trackData.key ordinal (blob) — may differ from Track.key only when key
    # is None (blob stores 0); compare against mapper expectation.
    _compare_track_data_blob(rb_id, expected, db, discrepancies)
    _compare_beatgrid(rb_id, expected, db, discrepancies)
    _compare_quick_cues(rb_id, expected, db, discrepancies)
    _compare_loops(rb_id, expected, db, discrepancies)


def _compare_track_data_blob(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        td = decode_track_data(db.track_data or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001 - report decode failure as discrepancy
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="track_data",
                expected="decodable",
                actual=f"decode_error: {exc}",
                # An undecodable blob is a defect no matter what the source
                # says today — it survives the exit-3 staleness partition.
                source_independent=True,
            )
        )
        return
    exp_key = int(expected.key) if expected.key is not None else 0
    if int(td.key) != exp_key:
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="track_data.key",
                expected=exp_key,
                actual=int(td.key),
            )
        )


def _compare_beatgrid(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        bd = decode_beat_data(db.beat_data or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="beat_data",
                expected="decodable",
                actual=f"decode_error: {exc}",
                # An undecodable blob is a defect no matter what the source
                # says today — it survives the exit-3 staleness partition.
                source_independent=True,
            )
        )
        return

    exp_markers = expected.beat_grid.default_markers
    act_markers = bd.default_beat_grid.markers

    # Exact integer equality on sample offsets (no float tolerance).
    exp_offsets = [int(m.sample_offset) for m in exp_markers]
    act_offsets = [int(m.sample_offset) for m in act_markers]
    if exp_offsets != act_offsets:
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="beatgrid.sample_offsets",
                expected=exp_offsets,
                actual=act_offsets,
            )
        )

    # Verify both numbered grids, including the intentionally adjusted one.
    for name, expected_grid, actual_grid in (
        ("default", expected.beat_grid.default_markers, bd.default_beat_grid.markers),
        ("adjusted", expected.beat_grid.adjusted_markers, bd.adjusted_beat_grid.markers),
    ):
        intended = [(int(m.sample_offset), m.beat_number, m.number_of_beats) for m in expected_grid]
        actual = [(int(m.sample_offset), m.beat_number, m.number_of_beats) for m in actual_grid]
        if intended != actual:
            discrepancies.append(Discrepancy(track_id=rb_id, field=f"beatgrid.{name}_markers",
                                             expected=intended, actual=actual))


def _compare_quick_cues(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        qc = decode_quick_cues(db.quick_cues or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="quick_cues",
                expected="decodable",
                actual=f"decode_error: {exc}",
                # An undecodable blob is a defect no matter what the source
                # says today — it survives the exit-3 staleness partition.
                source_independent=True,
            )
        )
        return

    # Pad both sides to 8 so pad indices line up; None means "empty slot".
    exp_slots: list[Any] = list(expected.quick_cues)
    act_slots: list[Any] = list(qc.cues)
    # Normalize lengths to 8 for pad-index comparison.
    while len(exp_slots) < 8:
        exp_slots.append(None)
    while len(act_slots) < 8:
        act_slots.append(None)

    for pad in range(8):
        exp = exp_slots[pad]
        act = act_slots[pad]
        if exp is None and act is None:
            continue
        if exp is None:
            continue

        exp_off = int(exp.sample_offset)
        act_off = int(act.sample_offset) if act is not None else None
        if exp_off != act_off:
            # Empty expected vs empty actual is fine.
            if exp.sample_offset == _EMPTY_SAMPLE and (
                act is None or act.sample_offset == _EMPTY_SAMPLE
            ):
                pass
            else:
                discrepancies.append(
                    Discrepancy(
                        track_id=rb_id,
                        field=f"quick_cue[{pad}].sample_offset",
                        expected=exp_off,
                        actual=act_off,
                    )
                )

        exp_label = exp.label
        act_label = act.label if act is not None else None
        if exp_label != act_label:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"quick_cue[{pad}].label",
                    expected=exp_label,
                    actual=act_label,
                )
            )

        exp_color = tuple(exp.color)
        act_color = (
            (act.color.a, act.color.r, act.color.g, act.color.b)
            if act is not None
            else None
        )
        if exp_color != act_color:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"quick_cue[{pad}].color",
                    expected=exp_color,
                    actual=act_color,
                )
            )


def _compare_loops(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        lp = decode_loops(db.loops or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="loops",
                expected="decodable",
                actual=f"decode_error: {exc}",
                # An undecodable blob is a defect no matter what the source
                # says today — it survives the exit-3 staleness partition.
                source_independent=True,
            )
        )
        return

    exp_slots = list(expected.loops)
    act_slots = list(lp.loops)
    n = max(len(exp_slots), len(act_slots), 8)
    for i in range(n):
        exp = exp_slots[i] if i < len(exp_slots) else None
        act = act_slots[i] if i < len(act_slots) else None
        if exp is None:
            continue
        exp_start = int(exp.start_sample_offset)
        exp_end = int(exp.end_sample_offset)
        act_start = int(act.start_sample_offset) if act is not None else None
        act_end = int(act.end_sample_offset) if act is not None else None
        if exp_start == int(_EMPTY_SAMPLE) and (
            act is None or act.start_sample_offset == _EMPTY_SAMPLE
        ):
            continue
        if exp_start != act_start:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"loop[{i}].start",
                    expected=exp_start,
                    actual=act_start,
                )
            )
        if exp_end != act_end:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"loop[{i}].end",
                    expected=exp_end,
                    actual=act_end,
                )
            )


def _compare_playlists(
    source: SourceLibrary,
    conn: sqlite3.Connection,
    *,
    by_path: dict[str, _DbTrack],
    drive_root: Path,
    engine_lib: Path,
    discrepancies: list[Discrepancy],
    external_edits: list[ExternalEdit],
) -> None:
    db_count = int(conn.execute("SELECT COUNT(*) FROM Playlist").fetchone()[0])
    exp_count = len(source.playlists)

    # Classify database playlists the source does not describe. The writer pins
    # lastEditTime on every row it creates, so an extra playlist carrying any
    # other value was written by Engine DJ — a legitimate desktop-library merge
    # observed on real hardware (2026-07-31: 45 converted playlists became 48
    # after opening Engine DJ; the 3 additions all carried real timestamps).
    # Reporting that the same way as corruption would train users to ignore
    # verify. An extra playlist that DOES carry our pin, by contrast, claims to
    # be ours and is not — that stays a real discrepancy below.
    #
    # Deliberately NOT keyed on Playlist.id versus our contiguous allocation:
    # the same experiment showed Engine reassigns ids across a merge (ids ran
    # to 84 against our 1..45), so id position alone would misclassify our own
    # renumbered rows. max(Playlist.id) serves only as corroborating detail.
    expected_paths = set(resolve_paths(source.playlists).values())
    last_edit_of = {
        int(row[0]): "" if row[1] is None else str(row[1])
        for row in conn.execute("SELECT id, lastEditTime FROM Playlist")
    }
    for path, list_id in sorted(
        db_playlist_paths(conn).items(), key=lambda item: item[1]
    ):
        if path in expected_paths:
            continue
        last_edit = last_edit_of[list_id]
        if last_edit != PINNED_LAST_EDIT_TIME:
            external_edits.append(
                ExternalEdit(
                    label=format_path(path),
                    playlist_id=list_id,
                    last_edit_time=last_edit,
                    beyond_watermark=list_id > exp_count,
                )
            )

    # Count only rows this tool could have written: Engine's additions are
    # already named above as informational external edits, and re-counting
    # them here would push the exit code to 1 for a benign Engine feature.
    if db_count - len(external_edits) != exp_count:
        discrepancies.append(
            Discrepancy(
                track_id=None,
                field="playlist_count",
                expected=exp_count,
                actual=db_count - len(external_edits),
            )
        )

    # The comparison itself lives in playlist_check, which the writer's
    # pre-publish gate also calls. verify records findings and keeps checking;
    # the writer aborts. They must not disagree about what is wrong.
    for problem in compare_playlists(
        source,
        conn,
        drive_root=drive_root,
        engine_lib=engine_lib,
        track_id_by_path={path: t.id for path, t in by_path.items()},
    ):
        discrepancies.append(
            Discrepancy(
                track_id=None,
                field=f"playlist[{problem.label}].{problem.kind}",
                expected=problem.expected,
                actual=problem.actual,
                # A broken nextEntityId chain is internally inconsistent — no
                # source oracle is needed to call it a defect, so it must beat
                # a staleness (exit 3) classification. Membership/order kinds
                # stay source-dependent.
                source_independent=(problem.kind == CHAIN),
            )
        )



def _compare_artwork(
    source: SourceLibrary,
    conn: sqlite3.Connection,
    *,
    discrepancies: list[Discrepancy],
) -> None:
    keys: set[str] = set()
    for t in source.tracks.values():
        if t.artwork is not None and t.artwork.content_key:
            keys.add(t.artwork.content_key)
    expected = len(keys)
    actual = int(conn.execute("SELECT COUNT(*) FROM AlbumArt").fetchone()[0])
    if expected != actual:
        discrepancies.append(
            Discrepancy(
                track_id=None,
                field="album_art_count",
                expected=expected,
                actual=actual,
            )
        )

    # Row count alone is not verification: swapped, truncated or re-encoded
    # image bytes keep the count identical while the user sees wrong covers.
    # Recompute the dedup key over the stored BLOB and compare it to the keys
    # the source produced — that is the same function the writer used, so any
    # byte drift shows up as a key that should not exist.
    if not keys:
        return
    for row_id, blob in conn.execute(
        "SELECT id, albumArt FROM AlbumArt ORDER BY id"
    ).fetchall():
        if not blob:
            discrepancies.append(
                Discrepancy(
                    track_id=None,
                    field=f"album_art[{row_id}].bytes",
                    expected="non-empty image",
                    actual="empty blob",
                )
            )
            continue
        stored_key = artwork_content_hash(bytes(blob))
        if stored_key not in keys:
            discrepancies.append(
                Discrepancy(
                    track_id=None,
                    field=f"album_art[{row_id}].content_key",
                    expected=f"one of {len(keys)} source artwork keys",
                    actual=stored_key,
                )
            )


def _resolve_track_path(engine_lib: Path, track_path: str) -> Path:
    """Resolve Track.path relative to Engine Library/ (../Contents/… form)."""
    p = Path(track_path)
    if p.is_absolute():
        return p
    # Pure relative (including .. segments): resolve against Engine Library.
    return (engine_lib / track_path).resolve()


def _eq(
    track_id: int | None,
    field_name: str,
    expected: Any,
    actual: Any,
    discrepancies: list[Discrepancy],
) -> None:
    if expected != actual:
        discrepancies.append(
            Discrepancy(
                track_id=track_id,
                field=field_name,
                expected=expected,
                actual=actual,
            )
        )
