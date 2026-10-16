"""W1 provenance: fingerprint at read time, journal at publish, attribution in verify.

WHY: a real conversion published playlist entries the settled export.pdb did not
contain (torn read, 14 s after rekordbox's last write), and a later incident saw
a "successful" convert drop its report into the source repo with the drive
unmounted. Nothing recorded which bytes any m.db was built from, so verify could
only blame the database. These tests pin the whole evidence chain: the hash is
taken over the buffer actually parsed, it travels via journal + report, and
verify uses it to name which oracle moved — without ever letting staleness
launder a source-independent defect (broken chain, undecodable blob) into
"re-run convert".
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from rb2engine.ir import SourceFingerprint, SourceLibrary
from rb2engine.report import (
    JOURNAL_FILENAME,
    JOURNAL_MAX_BYTES,
    ConversionReport,
    ProvenanceRecord,
    append_journal,
    read_last_journal_entry,
    validate_report,
)
from rb2engine.verify import Discrepancy, ProvenanceFinding, VerifyResult
from rb2engine.writer.build import build_library

# Reuse the hand-authored stick/library builders instead of re-deriving them.
from tests.unit.test_exit_codes import _write_mini_export_pdb
from tests.unit.test_verify import _build_fixture, _patch_read_library

# A fingerprint for injected IR: distinct from any real hash by construction.
_FP = SourceFingerprint(sha256="b" * 64, size=1234, mtime=1000.0)


def _record(
    *,
    pdb_sha256: str = _FP.sha256,
    m_db_sha256: str = "c" * 64,
    max_playlist_id: int = 1,
    max_playlist_entity_id: int = 3,
) -> ProvenanceRecord:
    return ProvenanceRecord(
        pdb_sha256=pdb_sha256,
        pdb_size=_FP.size,
        pdb_mtime=_FP.mtime,
        m_db_sha256=m_db_sha256,
        max_playlist_id=max_playlist_id,
        max_playlist_entity_id=max_playlist_entity_id,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Reader: the fingerprint is of the parsed buffer, not the settled file
# ---------------------------------------------------------------------------


def test_fingerprint_hashes_the_parsed_buffer_not_the_settled_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sha256 must name the bytes the parser consumed, even if the file
    on disk has since settled into something else.

    WHY: this is the torn-read incident in miniature — convert parsed bytes
    the settled export.pdb no longer contained. A fingerprint taken by
    re-reading the file would have recorded the settled hash and testified
    that nothing was wrong.
    """
    from rb2engine.reader.pdb import parse_export_pdb

    drive = tmp_path / "stick"
    drive.mkdir()
    pdb_path = _write_mini_export_pdb(drive)
    parsed_bytes = pdb_path.read_bytes()

    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path) -> bytes:
        if self == pdb_path:
            return parsed_bytes
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    # The file "settles" into different bytes underneath the parse.
    settled = bytearray(parsed_bytes)
    settled[-1] ^= 0xFF
    pdb_path.write_bytes(bytes(settled))

    lib = parse_export_pdb(pdb_path, drive)

    assert lib.fingerprint is not None
    assert lib.fingerprint.sha256 == hashlib.sha256(parsed_bytes).hexdigest()
    assert lib.fingerprint.sha256 != hashlib.sha256(bytes(settled)).hexdigest()
    assert lib.fingerprint.size == len(parsed_bytes)


def test_read_library_carries_the_fingerprint(tmp_path: Path) -> None:
    """read_library must surface the reader's fingerprint unmodified.

    WHY: mapper and writer stay pdb-ignorant; the fingerprint rides the
    SourceLibrary through dataclasses.replace. If a replace() call ever
    rebuilt the library field-by-field, provenance would silently vanish
    and every publish would record nothing.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    drive.mkdir()
    pdb_path = _write_mini_export_pdb(drive)

    lib = read_library(drive, with_anlz=False, with_artwork=False)

    assert lib.fingerprint is not None
    assert lib.fingerprint.sha256 == _sha256(pdb_path)
    assert lib.fingerprint.size == pdb_path.stat().st_size
    assert lib.fingerprint.mtime == pdb_path.stat().st_mtime


def test_to_json_obj_excludes_fingerprint(tmp_path: Path) -> None:
    """The canonical IR JSON must not change when a fingerprint is present.

    WHY: to_json_obj is load-bearing for golden byte-identity (golden_ir.json,
    inspect --json). A per-source hash in that output would break every golden
    on every re-export.
    """
    bare = SourceLibrary(
        drive_root=tmp_path, tracks={}, playlists=[], warnings=[]
    )
    fingerprinted = replace(bare, fingerprint=_FP)

    assert fingerprinted.to_json_obj() == bare.to_json_obj()
    assert "fingerprint" not in fingerprinted.to_json_obj()


# ---------------------------------------------------------------------------
# Writer: provenance recorded on the report, time-free and deterministic
# ---------------------------------------------------------------------------


def test_build_records_provenance_of_published_mdb(tmp_path: Path) -> None:
    """build_library must pair the pdb fingerprint with the published m.db hash
    and the id watermarks.

    WHY: this pairing is the attribution matrix — without both sides, a later
    verify can see that something differs but never say which oracle moved.
    """
    drive, lib, m_db = _build_fixture(tmp_path)
    lib = replace(lib, fingerprint=_FP)
    report = ConversionReport()
    m_db = build_library(lib, drive_root=drive, report=report, with_artwork=False)

    prov = report.provenance
    assert prov is not None
    assert prov.pdb_sha256 == _FP.sha256
    assert prov.pdb_size == _FP.size
    assert prov.pdb_mtime == _FP.mtime
    # The staged hash must describe the bytes that actually reached the stick.
    assert prov.m_db_sha256 == _sha256(m_db)
    # Dense allocation: 1 playlist, 3 entities in the fixture.
    assert prov.max_playlist_id == 1
    assert prov.max_playlist_entity_id == 3


def test_build_without_fingerprint_records_no_provenance(tmp_path: Path) -> None:
    """A source with no fingerprint (not parsed from a pdb) records nothing.

    WHY: an empty record must be absent, not fabricated — verify treats a
    missing record as its own finding, and a made-up hash would convert
    "unknown" into a false mismatch.
    """
    drive, lib, _m_db = _build_fixture(tmp_path)
    assert lib.fingerprint is None
    report = ConversionReport()
    build_library(lib, drive_root=drive, report=report, with_artwork=False)
    assert report.provenance is None


def test_rebuild_records_identical_mdb_hash(tmp_path: Path) -> None:
    """Two builds of the same source must record the same m.db sha256.

    WHY: the plan's storage decision hinges on it — only the journal may vary
    between rebuilds; the m.db (and therefore its recorded hash) must not,
    or the journal would report every rebuild as a database change.
    """
    drive, lib, _m_db = _build_fixture(tmp_path)
    lib = replace(lib, fingerprint=_FP)

    r1, r2 = ConversionReport(), ConversionReport()
    build_library(lib, drive_root=drive, report=r1, with_artwork=False)
    build_library(lib, drive_root=drive, report=r2, with_artwork=False)

    assert r1.provenance is not None and r2.provenance is not None
    assert r1.provenance.m_db_sha256 == r2.provenance.m_db_sha256
    assert r1.provenance.max_playlist_entity_id == r2.provenance.max_playlist_entity_id


def test_provenance_record_carries_no_timestamp() -> None:
    """The record the writer fills must be time-free.

    WHY: build_library is inside the no-wallclock determinism boundary; the
    timestamp belongs to the journal line only (report.py / cli.py). A field
    slipping into this record is how a clock would sneak back into writer/.
    """
    fields = set(ProvenanceRecord.__dataclass_fields__)
    assert fields == {
        "pdb_sha256",
        "pdb_size",
        "pdb_mtime",
        "m_db_sha256",
        "max_playlist_id",
        "max_playlist_entity_id",
        "key_analysis_sha256",
    }


def test_report_json_includes_provenance_and_validates(tmp_path: Path) -> None:
    """provenance travels in the report JSON and satisfies REPORT_SCHEMA."""
    report = ConversionReport()
    report.provenance = _record()
    obj = report.to_json_obj()
    validate_report(obj)
    assert obj["provenance"]["pdb_sha256"] == _FP.sha256


def test_report_without_provenance_still_validates() -> None:
    """A 0.4.0 report (no provenance key at all) must keep validating.

    WHY: verify degrades missing provenance to a finding; the schema rejecting
    old reports would turn that graceful path into a crash.
    """
    obj = ConversionReport().to_json_obj()
    del obj["provenance"]
    validate_report(obj)


# ---------------------------------------------------------------------------
# Journal: append-only, capped, clock injected outside writer/
# ---------------------------------------------------------------------------


def test_journal_appends_one_line_per_publish(tmp_path: Path) -> None:
    engine_lib = tmp_path / "Engine Library"
    engine_lib.mkdir()

    append_journal(engine_lib, _record(m_db_sha256="1" * 64), timestamp="t1")
    path = append_journal(engine_lib, _record(m_db_sha256="2" * 64), timestamp="t2")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, last = (json.loads(ln) for ln in lines)
    assert first["timestamp"] == "t1"
    assert last["timestamp"] == "t2"
    # verify reads the newest publish.
    entry = read_last_journal_entry(engine_lib)
    assert entry is not None and entry["m_db_sha256"] == "2" * 64


def test_journal_stamps_a_real_timestamp_by_default(tmp_path: Path) -> None:
    """Without an injected timestamp the journal line carries wall-clock UTC.

    WHY: the whole reason the journal lives outside writer/ is so a publish
    can be dated; a journal that silently recorded nothing would defeat it.
    """
    from datetime import datetime

    engine_lib = tmp_path / "Engine Library"
    engine_lib.mkdir()
    append_journal(engine_lib, _record())
    entry = read_last_journal_entry(engine_lib)
    assert entry is not None
    # Raises ValueError if the stamp is not ISO-8601.
    datetime.fromisoformat(entry["timestamp"])


def test_journal_caps_by_dropping_oldest_lines(tmp_path: Path) -> None:
    """When the cap is exceeded the OLDEST lines go; the new line always stays.

    WHY: the newest line is what verify compares against; the tail is lineage.
    Dropping newest-first (or refusing to write) would discard the one line
    that matters at the moment it matters.
    """
    engine_lib = tmp_path / "Engine Library"
    engine_lib.mkdir()
    path = engine_lib / JOURNAL_FILENAME

    filler = json.dumps({"timestamp": "old", "pad": "x" * 180}) + "\n"
    n = (JOURNAL_MAX_BYTES // len(filler)) + 50  # comfortably over the cap
    path.write_text(filler * n, encoding="utf-8")
    assert path.stat().st_size > JOURNAL_MAX_BYTES

    append_journal(engine_lib, _record(m_db_sha256="9" * 64), timestamp="new")

    assert path.stat().st_size <= JOURNAL_MAX_BYTES
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["timestamp"] == "new"
    # Oldest lines were dropped, and every survivor still parses.
    assert len(lines) < n + 1
    for ln in lines:
        json.loads(ln)


def test_journal_survives_truncated_final_line(tmp_path: Path) -> None:
    """A crash-truncated last line must not corrupt the next appended record."""
    engine_lib = tmp_path / "Engine Library"
    engine_lib.mkdir()
    (engine_lib / JOURNAL_FILENAME).write_bytes(b'{"timestamp": "half')

    append_journal(engine_lib, _record(), timestamp="after-crash")

    entry = read_last_journal_entry(engine_lib)
    assert entry is not None and entry["timestamp"] == "after-crash"


def test_read_last_journal_entry_missing_and_garbage(tmp_path: Path) -> None:
    engine_lib = tmp_path / "Engine Library"
    engine_lib.mkdir()
    assert read_last_journal_entry(engine_lib) is None

    (engine_lib / JOURNAL_FILENAME).write_text("", encoding="utf-8")
    assert read_last_journal_entry(engine_lib) is None

    (engine_lib / JOURNAL_FILENAME).write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_last_journal_entry(engine_lib)


# ---------------------------------------------------------------------------
# Verify: attribution matrix + exit-code partition
# ---------------------------------------------------------------------------


def _journal_for(
    drive: Path, m_db: Path, *, pdb_sha256: str = _FP.sha256
) -> None:
    append_journal(
        drive / "Engine Library",
        _record(pdb_sha256=pdb_sha256, m_db_sha256=_sha256(m_db)),
        timestamp="t0",
    )


def test_missing_provenance_is_a_visible_finding_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No journal + no report provenance → reported, but a clean db stays 0.

    WHY: every pre-0.5 stick is in this state. Silently OK would hide that
    staleness is unattributable; failing would tell users their good sticks
    are broken. The contract is: visible finding, unchanged exit.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert result.provenance_missing is True
    assert any(p.code == "provenance_missing" for p in result.provenance_findings)
    assert result.exit_code == 0
    assert "provenance" in result.render_text().lower()


def test_fingerprint_mismatch_alone_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded pdb hash ≠ parsed pdb hash → exit 3 with the re-run remedy.

    WHY: this is the staleness case the incident report was mislabeled as.
    It must be its own finding with its own exit, not playlist discrepancies.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _journal_for(drive, m_db, pdb_sha256="a" * 64)  # built from "different" pdb
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert result.source_changed is True
    assert result.exit_code == 3
    finding = next(
        p for p in result.provenance_findings if p.code == "source_changed"
    )
    assert "re-run" in finding.message and "convert" in finding.message
    # The irreducible residue is documented in the finding itself.
    assert "undetectable until convert re-runs" in finding.message


def test_mismatch_makes_dependent_findings_informational_exit_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fingerprint mismatch + only source-dependent findings → exit 3.

    WHY: with the source moved there is no oracle for a bpm/order comparison;
    reporting it as corruption is exactly the misattribution W1 exists to end.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _journal_for(drive, m_db, pdb_sha256="a" * 64)
    with sqlite3.connect(m_db) as conn:
        conn.execute("UPDATE Track SET bpm = 99 WHERE title = 'Alpha'")
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert result.discrepancies  # the mutation was seen…
    assert all(not d.source_independent for d in result.discrepancies)
    assert result.exit_code == 3  # …but is not attributable
    assert "informational" in result.render_text()


def test_chain_break_beats_staleness_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fingerprint mismatch + broken entity chain → exit 1, not 3.

    WHY: this is the case that decides whether exit 3 can swallow a real
    defect. A forked nextEntityId chain is wrong no matter which export.pdb
    is on the stick; staleness must never launder it into "re-run convert".
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _journal_for(drive, m_db, pdb_sha256="a" * 64)
    with sqlite3.connect(m_db) as conn:
        # Every row now claims to be the tail: the chain is forked.
        conn.execute("UPDATE PlaylistEntity SET nextEntityId = 0")
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert result.source_changed is True
    assert any(
        d.source_independent and ".chain" in d.field for d in result.discrepancies
    )
    assert result.exit_code == 1


def test_undecodable_blob_beats_staleness_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fingerprint mismatch + garbage PerformanceData blob → exit 1."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _journal_for(drive, m_db, pdb_sha256="a" * 64)
    with sqlite3.connect(m_db) as conn:
        conn.execute("UPDATE PerformanceData SET quickCues = X'DEADBEEF'")
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert any(d.source_independent for d in result.discrepancies)
    assert result.exit_code == 1


def test_matching_provenance_keeps_discrepancies_attributable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fingerprint matches → a mutation is a real finding (exit 1) and the
    changed m.db hash is classified as db_changed, not alarmed as source.

    WHY: "db changed" is expected on this stick — Engine DJ provably rewrites
    m.db (desktop-library merge). The matrix must name it without blocking
    the source comparison, which remains authoritative.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _journal_for(drive, m_db)  # recorded == what we will parse and hash
    with sqlite3.connect(m_db) as conn:
        conn.execute("UPDATE Track SET bpm = 99 WHERE title = 'Alpha'")
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert result.source_changed is False
    assert result.db_changed is True  # the mutation moved the m.db hash
    assert any(p.code == "db_changed" for p in result.provenance_findings)
    assert result.exit_code == 1


def test_clean_stick_with_matching_provenance_is_silent_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matching journal + untouched m.db → no provenance findings, exit 0."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _journal_for(drive, m_db)
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert result.provenance_findings == []
    assert result.exit_code == 0


def test_unreadable_journal_is_reported_and_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage in the journal → finding + fallback, never a traceback.

    WHY: the journal lives on removable exFAT; torn writes are this repo's
    founding incident. Backwards compatibility demands degradation.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    (drive / "Engine Library" / JOURNAL_FILENAME).write_text(
        "{{{not json\n", encoding="utf-8"
    )
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert any(p.code == "journal_unreadable" for p in result.provenance_findings)
    assert result.provenance_missing is True  # no usable witness remained


def test_report_provenance_is_the_fallback_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no journal, the report JSON's provenance block still attributes.

    WHY: decision (c) — the report is a same-publish witness; verify must say
    staleness even when only the report survived (e.g. journal deleted).
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    report = ConversionReport()
    report.provenance = _record(pdb_sha256="a" * 64, m_db_sha256=_sha256(m_db))
    report.write_json(drive / "Engine Library" / "rb2engine-report.json")
    _patch_read_library(monkeypatch, replace(lib, fingerprint=_FP))

    result = verify_library(drive, with_artwork=False)

    assert result.provenance_missing is False
    assert result.source_changed is True
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# CLI: journal written at publish; exit mapping; off-stick report warning
# ---------------------------------------------------------------------------


def test_cli_convert_appends_journal_on_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """convert must leave a journal line whose hashes match what it published."""
    from rb2engine.cli import main

    drive, lib, _m_db = _build_fixture(tmp_path)
    monkeypatch.setattr(
        "rb2engine.reader.library.read_library",
        lambda *_a, **_k: replace(lib, fingerprint=_FP),
    )

    result = CliRunner().invoke(main, ["convert", str(drive), "--no-artwork"])
    assert result.exit_code == 0, result.output

    entry = read_last_journal_entry(drive / "Engine Library")
    assert entry is not None
    assert entry["pdb_sha256"] == _FP.sha256
    m_db = drive / "Engine Library" / "Database2" / "m.db"
    assert entry["m_db_sha256"] == _sha256(m_db)
    assert entry["timestamp"]  # dated outside writer/ (no-wallclock gate)

    # And the report on the stick carries the same record.
    report_obj = json.loads(
        (drive / "Engine Library" / "rb2engine-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report_obj["provenance"]["pdb_sha256"] == _FP.sha256


def test_cli_verify_maps_partition_to_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The click app must surface VerifyResult.exit_code verbatim (incl. 3)."""
    from rb2engine.cli import main

    def _fake(result: VerifyResult):
        return lambda *_a, **_k: result

    stale = VerifyResult(
        checked=1,
        matched=1,
        mismatched=0,
        source_changed=True,
        provenance_findings=[ProvenanceFinding("source_changed", "stale")],
    )
    broken = VerifyResult(
        checked=1,
        matched=0,
        mismatched=1,
        discrepancies=[
            Discrepancy(
                track_id=None,
                field="playlist[X].chain",
                expected="chain",
                actual="fork",
                source_independent=True,
            )
        ],
        source_changed=True,
    )
    for result, expected_code in ((stale, 3), (broken, 1)):
        monkeypatch.setattr("rb2engine.verify.verify_library", _fake(result))
        out = CliRunner().invoke(main, ["verify", str(tmp_path)])
        assert out.exit_code == expected_code, out.output


def test_emit_report_warns_when_successful_report_lands_off_stick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Successful run + report falling back to cwd → loud warning.

    WHY: this really happened — convert reported success with the drive
    unmounted and wrote the report into the user's source repo, leaving the
    stick with no provenance at all. Silence made it invisible for weeks.
    """
    from rb2engine.cli import _emit_report

    monkeypatch.chdir(tmp_path)
    ghost_drive = tmp_path / "unmounted"  # no Engine Library/ → cwd fallback
    report = ConversionReport()

    _emit_report(report, ghost_drive, None)

    err = capsys.readouterr().err
    assert "NOT on the stick" in err
    assert "provenance" in err


def test_emit_report_stays_quiet_when_report_lands_on_stick(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    drive = tmp_path / "stick"
    (drive / "Engine Library").mkdir(parents=True)

    from rb2engine.cli import _emit_report

    _emit_report(ConversionReport(), drive, None)

    assert "NOT on the stick" not in capsys.readouterr().err
