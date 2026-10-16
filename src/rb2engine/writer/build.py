"""Atomic os.replace() of m.db and reconcile(); preserves sibling DBs and Music/.

Safety boundary — this is the ONLY module that writes to the user's stick:

* Write ONLY inside ``<drive>/Engine Library/``. Never touch ``PIONEER/`` or
  ``Contents/``.
* Build ``Database2/m.db.tmp`` in the same directory as ``m.db``, flush +
  fsync, close, then ``os.replace()`` over ``m.db``. Same-directory file
  replace is atomic against process failure on POSIX and Windows.
* PRESERVE everything we do not author: ``hm.db``, ``sm.db``, ``stm.db``,
  ``Music/``, ``Artwork/``, ``OverviewData/``, and any unknown file.
* Carry forward schema triple + ``Information.uuid`` from an existing ``m.db``.
* A stray ``m.db.tmp`` is deleted on startup, never adopted.
* On fatal error: remove ``m.db.tmp``, leave the previous ``m.db`` untouched.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from rb2engine.errors import FatalError
from rb2engine.ir import SourceArtwork, SourceLibrary
from rb2engine.ir_engine import EngineTrack
from rb2engine.playlist_check import compare_playlists
from rb2engine.progress import ProgressCallback, phase_callback
from rb2engine.report import ConversionReport, ProvenanceRecord

logger = logging.getLogger(__name__)

ENGINE_LIBRARY_DIRNAME = "Engine Library"
DATABASE2_DIRNAME = "Database2"
M_DB_NAME = "m.db"
M_DB_TMP_NAME = "m.db.tmp"

# Determinism pin for columns that Engine DDL triggers stamp with strftime('%s')
# (Track.lastEditTime on PerformanceData UPDATE) and for opaque Information
# fields that create_m_db may mint randomly. Fixed epoch 0 — not wall-clock.
#
# Leaving currentPlayedIndiciator at 0 is deliberate and costs nothing: Engine
# DJ populates it itself the first time it opens the library. Measured on a
# 3,673-track stick by diffing a pristine conversion against the same database
# after Engine had opened and closed it — that single cell (0 →
# 1698144667125441751) was the *only* change Engine made. No schema objects, no
# pragma changes, and Track, PerformanceData, AlbumArt, Playlist and
# PlaylistEntity all byte-identical. Writing a value here would buy nothing and
# would forfeit byte-identical rebuilds.
_DETERMINISTIC_EPOCH = 0
_DETERMINISTIC_PLAYED_INDICATOR = 0

# Fallback when we have no evidence at all: no prior m.db on the drive, no
# readable desktop library, and no explicit --target-schema.
#
# 3.0.1 rather than the newest we support, because the two directions are not
# symmetric: Engine migrates an older schema UPWARD (observed on real hardware —
# a 3.0.1 stick was migrated in place to 3.0.2 by Engine DJ 4.3.0), but there is
# no downgrade path. Writing the older version degrades gracefully for someone
# on an earlier Engine; writing the newest would hand them a database their
# build may simply refuse to open.
_DEFAULT_SCHEMA: tuple[int, int, int] = (3, 0, 1)

# Where Engine DJ keeps the desktop library on each platform. Read-only, and
# only consulted for a FRESH stick — a drive that already has an m.db always
# keeps its own triple.
_DESKTOP_LIBRARY_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("Music", "Engine Library", "Database2", "m.db"),
    ("Documents", "Engine Library", "Database2", "m.db"),
)


def detect_desktop_schema() -> tuple[int, int, int] | None:
    """Best-effort read of the user's own Engine desktop library schema.

    A fresh stick carries no signal about which Engine the user runs, so the
    next best evidence is the library Engine maintains on this machine. Strictly
    read-only and never fatal: any failure just means we fall back.

    Returns None when nothing readable is found, or when the version found is
    one we have no DDL for (writing a supported older schema and letting Engine
    migrate up beats writing a version we cannot actually produce).
    """
    from rb2engine.writer import database as database_mod
    from rb2engine.writer.schema import SUPPORTED_SCHEMAS

    home = Path.home()
    roots = [home, Path("/Users/Shared")] if os.name == "posix" else [home]
    for root in roots:
        for parts in _DESKTOP_LIBRARY_CANDIDATES:
            candidate = root.joinpath(*parts)
            if not candidate.is_file():
                continue
            try:
                triple = database_mod.detect_schema(candidate)
            except Exception as exc:  # noqa: BLE001 - diagnostics must never fail a run
                logger.debug("desktop library unreadable at %s: %s", candidate, exc)
                continue
            if triple is None:
                continue
            if triple in SUPPORTED_SCHEMAS:
                logger.info(
                    "no library on the drive; adopting schema %d.%d.%d from the "
                    "Engine desktop library at %s",
                    *triple,
                    candidate,
                )
                return triple
            logger.warning(
                "Engine desktop library at %s reports unsupported schema "
                "%d.%d.%d; falling back to %d.%d.%d (Engine migrates upward)",
                candidate,
                *triple,
                *_DEFAULT_SCHEMA,
            )
            return None
    return None


def reconcile(engine_lib_root: Path) -> None:
    """Startup cleanup of crash residue; never touches sibling DBs or Music/.

    * ``Database2/m.db.tmp`` → delete (never adopt a partial build).
    * ``Database2/m.db.tmp-journal`` → delete.
    * legacy ``Engine Library.tmp/`` or ``Engine Library.old/`` beside the
      library → delete (artifacts of the withdrawn directory-swap design).
    """
    engine_lib_root = Path(engine_lib_root)
    db2 = engine_lib_root / DATABASE2_DIRNAME
    if db2.is_dir():
        tmp = db2 / M_DB_TMP_NAME
        if tmp.exists():
            logger.warning("removing stray %s (never adopted)", tmp)
            tmp.unlink(missing_ok=True)
        journal = db2 / f"{M_DB_TMP_NAME}-journal"
        if journal.exists():
            journal.unlink(missing_ok=True)

    parent = engine_lib_root.parent
    for legacy_name in (f"{ENGINE_LIBRARY_DIRNAME}.tmp", f"{ENGINE_LIBRARY_DIRNAME}.old"):
        legacy = parent / legacy_name
        if legacy.exists():
            logger.warning("removing legacy artifact %s", legacy)
            if legacy.is_dir():
                _rmtree(legacy)
            else:
                legacy.unlink(missing_ok=True)


def build_library(
    lib: SourceLibrary,
    *,
    drive_root: Path,
    report: ConversionReport,
    path_base: str = "engine-lib",
    target_schema: tuple[int, int, int] | None = None,
    database_uuid: str | None = None,
    with_artwork: bool = True,
    on_progress: ProgressCallback | None = None,
    backup_existing: bool = False,
    allow_existing: bool = True,
) -> Path:
    """Orchestrate a full m.db write and return the final m.db path.

    See module docstring for the swap protocol and preservation rules.

    *on_progress* is an optional ``(phase, done, total)`` sink; ``total <= 0``
    marks a phase whose size is not known in advance.
    """
    from rb2engine.library_lock import library_lock

    engine_lib = Path(drive_root) / ENGINE_LIBRARY_DIRNAME
    library_existed = engine_lib.is_dir()
    with library_lock(engine_lib / DATABASE2_DIRNAME):
        if not allow_existing and (engine_lib / DATABASE2_DIRNAME / M_DB_NAME).exists():
            raise FatalError("An Engine database appeared after inspection. Inspect again.")
        if backup_existing:
            _backup_database(engine_lib / DATABASE2_DIRNAME)
        return _build_library_locked(
            lib,
            drive_root=drive_root,
            report=report,
            path_base=path_base,
            target_schema=target_schema,
            database_uuid=database_uuid,
            with_artwork=with_artwork,
            on_progress=on_progress,
            library_existed=library_existed,
        )


def _backup_database(db2: Path) -> Path | None:
    """Snapshot the current database under the writer lock before desktop rebuilds.

    SQLite backup includes committed WAL pages. This is an m.db recovery copy,
    not a backup of the user's audio or the entire Engine library.
    """
    import uuid

    source = db2 / M_DB_NAME
    if not source.exists():
        return None
    folder = db2 / "rb2engine-backups"
    if source.is_symlink() or folder.is_symlink():
        raise FatalError("Database backup paths may not be symlinks")
    folder.mkdir(exist_ok=True)
    target = folder / f"m-{uuid.uuid4().hex}.db"
    source_uri = source.resolve().as_uri() + "?mode=ro"
    try:
        with (
            contextlib.closing(sqlite3.connect(source_uri, uri=True)) as src,
            contextlib.closing(sqlite3.connect(str(target))) as dst,
        ):
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise FatalError("Database backup failed its integrity check")
        # Windows FlushFileBuffers requires a writable handle.
        with target.open("r+b") as stream:
            os.fsync(stream.fileno())
    except (OSError, sqlite3.Error) as exc:
        raise FatalError(f"Could not back up the existing Engine database: {exc}") from exc
    return target


def _build_library_locked(
    lib: SourceLibrary,
    *,
    library_existed: bool,
    drive_root: Path,
    report: ConversionReport,
    path_base: str = "engine-lib",
    target_schema: tuple[int, int, int] | None = None,
    database_uuid: str | None = None,
    with_artwork: bool = True,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Orchestrate a full m.db write and return the final m.db path.

    See module docstring for the swap protocol and preservation rules.

    *on_progress* is an optional ``(phase, done, total)`` sink; ``total <= 0``
    marks a phase whose size is not known in advance.
    """
    drive_root = Path(drive_root)
    report.path_base = path_base

    engine_lib = drive_root / ENGINE_LIBRARY_DIRNAME
    db2 = engine_lib / DATABASE2_DIRNAME
    m_db_path = db2 / M_DB_NAME
    tmp_path = db2 / M_DB_TMP_NAME

    m_db_existed = m_db_path.is_file()

    # Reconcile before any read of the prior library (stray tmp must not be
    # mistaken for state, and must not block detect_schema on m.db).
    if library_existed:
        reconcile(engine_lib)

    # Schema + uuid: prefer explicit args, else carry forward from existing m.db.
    prior_schema, prior_uuid = _read_prior_identity(m_db_path if m_db_existed else None)
    # Precedence: explicit flag > the drive's own existing library > the user's
    # desktop Engine library > conservative fallback.
    schema = (
        target_schema
        or prior_schema
        or (detect_desktop_schema() if not m_db_existed else None)
        or _DEFAULT_SCHEMA
    )
    db_uuid = database_uuid or prior_uuid  # None → create_m_db mints

    # Late imports keep this module importable while sibling writer modules
    # land (concurrent workers). Contract signatures are fixed.
    from rb2engine.writer import database as database_mod
    from rb2engine.writer import playlists as playlists_mod
    from rb2engine.writer.playlists import insert_playlists

    conn: sqlite3.Connection | None = None
    stage_dir: Path | None = None
    try:
        engine_lib.mkdir(parents=True, exist_ok=True)
        db2.mkdir(parents=True, exist_ok=True)

        # Never adopt a leftover tmp — wipe again just before create.
        if tmp_path.exists():
            logger.warning("removing stray %s before create", tmp_path)
            tmp_path.unlink()

        # Build on LOCAL storage, then copy into place.
        #
        # Building directly on the target is not viable on real DJ media: on a
        # macOS FAT32 (fskit) volume, sqlite3.executescript() of the Engine DDL
        # fails with "attempt to write a readonly database" even though the
        # volume is writable and plain CREATE TABLE + commit succeed. Staging
        # locally sidesteps the driver's journal handling entirely, and is also
        # far faster than thousands of small writes over USB.
        stage_dir = Path(tempfile.mkdtemp(prefix="rb2engine-stage-"))
        stage_path = stage_dir / M_DB_NAME

        create_m_db = database_mod.create_m_db
        conn = create_m_db(stage_path, schema=schema, uuid=db_uuid)

        report.counters.tracks_read = len(lib.tracks)
        report.counters.playlists_read = len(lib.playlists)

        # --- artwork (optional) ------------------------------------------------
        # Walk tracks in sorted rb_id order so AlbumArt AUTOINCREMENT ids (and
        # therefore every Track.albumArtId) are stable across runs regardless of
        # SourceLibrary.tracks dict insertion order.
        art_ids: dict[str, int] = {}
        arts: list[SourceArtwork] = []
        if with_artwork:
            seen: set[str] = set()
            for rb_id in sorted(lib.tracks.keys()):
                src = lib.tracks[rb_id]
                if src.artwork is None:
                    report.counters.artwork_missing += 1
                    continue
                report.counters.artwork_found += 1
                if src.artwork.content_key in seen:
                    report.counters.artwork_deduped += 1
                    continue
                seen.add(src.artwork.content_key)
                arts.append(src.artwork)
            if arts:
                from rb2engine.writer import artwork as artwork_mod

                art_ids = artwork_mod.insert_artwork(
                    conn, arts, on_progress=phase_callback(on_progress, "album art")
                )

        # --- map + insert tracks -----------------------------------------------
        from rb2engine.mapper.track import map_track
        from rb2engine.writer import tracks as tracks_mod

        engine_tracks: list[EngineTrack] = []
        rb_order: list[int] = []
        n_source = len(lib.tracks)
        for mapped, rb_id in enumerate(sorted(lib.tracks.keys()), start=1):
            if on_progress is not None:
                on_progress("mapping", mapped, n_source)
            src = lib.tracks[rb_id]
            if src.resolved_path is None:
                report.add_skip(
                    track_id=rb_id,
                    reason_code="unresolvable_path",
                    message="resolved_path is None",
                    title=src.title,
                )
                report.counters.tracks_unresolvable_paths += 1
                continue
            try:
                et = map_track(
                    src,
                    drive_root=drive_root,
                    engine_library_dir=engine_lib,
                )
            except Exception as exc:  # noqa: BLE001 - soft skip: one bad track must not kill the run
                report.add_skip(
                    track_id=rb_id,
                    reason_code="map_failed",
                    message=str(exc),
                    title=src.title,
                )
                continue
            engine_tracks.append(et)
            rb_order.append(rb_id)

        track_id_map = tracks_mod.insert_tracks(
            conn,
            engine_tracks,
            art_ids=art_ids or None,
            on_progress=phase_callback(on_progress, "writing tracks"),
        )
        # If insert_tracks returned empty but we had tracks, synthesise from order
        # only when the implementation used positional ids (defensive). Prefer
        # the returned map when non-empty.
        if not track_id_map and rb_order:
            # Trust insert_tracks; empty map with non-empty input is a writer bug
            # but must not invent ids that do not exist.
            pass
        # Align map keys to rb_ids when the tracks worker keyed by position —
        # contract says rb_id → id; use returned map as-is.
        report.counters.tracks_converted = len(track_id_map) if track_id_map else len(engine_tracks)
        # Some implementations may key by insert order 1..N; rebuild.
        if (
            track_id_map
            and rb_order
            and set(track_id_map.keys()) != set(rb_order)
            and set(track_id_map.keys()) == set(range(1, len(rb_order) + 1))
        ):
            track_id_map = {rb_order[i]: track_id_map[i + 1] for i in range(len(rb_order))}

        # --- playlists ---------------------------------------------------------
        if on_progress is not None:
            on_progress("playlists", 0, 0)
        intended_playlists: dict[int, Sequence[int]] = {}
        n_playlists = insert_playlists(
            conn,
            lib.playlists,
            track_id_map=track_id_map or {},
            intended_out=intended_playlists,
        )
        report.counters.playlists_converted = n_playlists

        # --- integrity (G4) before swap ----------------------------------------
        _finalize(conn)

        # Watermarks for the provenance record: our id allocation is dense from
        # 1, so any later row above these ceilings was not written by us.
        # Queried after _finalize so they describe the database as published.
        max_playlist_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM Playlist").fetchone()[0]
        )
        max_entity_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM PlaylistEntity").fetchone()[0]
        )

        conn.close()
        conn = None

        # Provenance: pair the fingerprint of the pdb bytes actually parsed
        # with the hash of the m.db those bytes produced, so a later verify can
        # name WHICH side moved instead of guessing. Hashed on local storage
        # before the USB copy. Deliberately no timestamp here — this module is
        # under the no-wallclock determinism gate; the journal (report.py /
        # cli.py) adds the clock outside that boundary. The hash lives in the
        # report/journal, never inside m.db: it cannot hash itself.
        if lib.fingerprint is not None:
            from rb2engine.keyscan import digest_record

            report.provenance = ProvenanceRecord(
                pdb_sha256=lib.fingerprint.sha256,
                pdb_size=lib.fingerprint.size,
                pdb_mtime=lib.fingerprint.mtime,
                m_db_sha256=_sha256_file(stage_path),
                max_playlist_id=max_playlist_id,
                max_playlist_entity_id=max_entity_id,
                key_analysis_sha256=(
                    digest_record(report.key_analysis) if report.key_analysis is not None else None
                ),
                grid_repair_sha256=(
                    digest_record(report.grid_repair) if report.grid_repair is not None else None
                ),
            )

        # journal_mode=DELETE removes m.db.tmp-journal on clean close; assert.
        # Move the finished database onto the target volume as m.db.tmp, then
        # atomically replace. shutil.copyfile writes a plain byte stream, which
        # the FAT32 driver handles fine.
        # Indeterminate: a half-gigabyte database crossing USB is the single
        # longest step of a large conversion, and copyfile reports nothing.
        if on_progress is not None:
            on_progress("publishing", 0, 0)
        shutil.copyfile(stage_path, tmp_path)
        shutil.rmtree(stage_dir, ignore_errors=True)

        journal = Path(str(tmp_path) + "-journal")
        if journal.exists():
            journal.unlink()

        _fsync_file(tmp_path)
        _fsync_dir(db2)

        # Re-check the copy that actually crossed to the target volume.
        #
        # The check inside insert_playlists runs in the writing transaction, so
        # it can only prove SQLite agreed with us at that moment — it cannot see
        # the commit, the half-gigabyte copy over USB, or this volume's driver.
        # A conversion once published playlists containing a track that was in
        # no source playlist and still exited 0, and the staged database is
        # discarded before anyone can compare it, so this is the last point
        # where that class of corruption is still catchable. Running it before
        # os.replace means a failure leaves the user's previous m.db in place.
        if intended_playlists or lib.playlists:
            check_conn = sqlite3.connect(tmp_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if intended_playlists:
                    playlists_mod.assert_entities_match_intent(check_conn, intended_playlists)

                # Independent oracle. The check above compares the database
                # against what insert_playlists *intended*, and both sides of it
                # descend from track_id_map — so a mapping fault agrees with
                # itself and passes. This one recomputes every expected track id
                # from the source through map_track and the database's own path
                # index, never touching that map, and therefore fails where the
                # intent check cannot.
                #
                # It cannot detect a misread source: both sides descend from the
                # same parse. That is the reader's job (pdb G1d).
                problems = compare_playlists(
                    lib, check_conn, drive_root=drive_root, engine_lib=engine_lib
                )
                if problems:
                    detail = "; ".join(p.describe() for p in problems[:5])
                    more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
                    raise RuntimeError(
                        "playlist-scoped recheck against the source failed, so "
                        f"nothing was published: {detail}{more}"
                    )
            finally:
                check_conn.close()

        os.replace(tmp_path, m_db_path)
        _fsync_dir(db2)

        # macOS + FAT32 can leave an AppleDouble sidecar (._m.db) beside the
        # database after the copy/replace step. Remove only our own published
        # file's sidecar — never a blanket ._ * sweep of the user's metadata.
        _remove_appledouble_sidecar_for(m_db_path)

        return m_db_path

    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception as close_exc:  # noqa: BLE001 - already unwinding
                # Closing a connection that is already broken can raise; we are
                # in the error path and about to re-raise, so log and continue.
                logger.debug("ignoring close() failure during rollback: %s", close_exc)
            conn = None
        # Leave prior m.db untouched; only discard the in-progress tmp.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as unlink_exc:
                logger.error("failed to remove %s: %s", tmp_path, unlink_exc)
        # Fresh stick: do not leave a half-created Engine Library tree behind.
        if not library_existed and engine_lib.exists() and not m_db_existed:
            try:
                # Only remove if we never successfully published an m.db.
                if not m_db_path.is_file():
                    # Keep the lock inode stable for processes that may already
                    # have opened it. Remove every incomplete product artifact.
                    for child in engine_lib.iterdir():
                        if child == db2:
                            for entry in child.iterdir():
                                if entry.name != ".rb2engine.lock":
                                    _rmtree(entry)
                        else:
                            _rmtree(child)
            except OSError as rm_exc:
                logger.error("failed to remove partial %s: %s", engine_lib, rm_exc)

        msg = f"build_library failed: {exc}"
        # A fatal run published nothing: a provenance record captured before
        # the failure (e.g. the pre-publish recheck refused) would describe an
        # m.db that never reached the stick. Clear it so the report cannot
        # claim a publish that did not happen.
        report.provenance = None
        report.mark_fatal(msg)
        if isinstance(exc, FatalError):
            raise
        raise FatalError(msg) from exc


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _read_prior_identity(
    m_db_path: Path | None,
) -> tuple[tuple[int, int, int] | None, str | None]:
    """Return (schema_triple, uuid) from an existing m.db, or (None, None)."""
    if m_db_path is None or not Path(m_db_path).is_file():
        return None, None

    from rb2engine.writer import database as database_mod

    schema = None
    detect = getattr(database_mod, "detect_schema", None)
    if callable(detect):
        try:
            schema = detect(Path(m_db_path))
        except Exception as exc:  # noqa: BLE001 - unreadable target must not abort; fall back to default
            logger.warning("detect_schema failed on %s: %s", m_db_path, exc)

    uuid_val: str | None = None
    try:
        conn = sqlite3.connect(Path(m_db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT schemaVersionMajor, schemaVersionMinor, "
                "schemaVersionPatch, uuid FROM Information LIMIT 1"
            ).fetchone()
            if row is not None:
                if schema is None:
                    schema = (int(row[0]), int(row[1]), int(row[2]))
                uuid_val = str(row[3]) if row[3] is not None else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("could not read Information from %s: %s", m_db_path, exc)

    return schema, uuid_val


def _pin_deterministic_columns(conn: sqlite3.Connection) -> None:
    """Overwrite wall-clock / random values so re-runs dump identically.

    Engine's captured DDL triggers set ``Track.lastEditTime = strftime('%s')``
    when PerformanceData blobs are updated. ``create_m_db`` may also mint a
    random ``currentPlayedIndiciator``. Neither may leak into a canonical dump:
    two conversions spanning a second boundary would otherwise diverge.
    """
    conn.execute(
        "UPDATE Track SET lastEditTime = ?",
        (_DETERMINISTIC_EPOCH,),
    )
    conn.execute(
        "UPDATE Information SET currentPlayedIndiciator = ?",
        (_DETERMINISTIC_PLAYED_INDICATOR,),
    )
    conn.commit()


def _remove_appledouble_sidecar_for(db_path: Path) -> None:
    """Remove macOS AppleDouble sidecar for the published m.db only.

    Surgical by construction: only ``._`` + the basename we just wrote
    (``._m.db``), in the same directory. No-op on non-macOS. Never deletes
    ``._hm.db``, ``._Music``, or any other user/sibling metadata.
    """
    if sys.platform != "darwin":
        return
    db_path = Path(db_path)
    if db_path.name != M_DB_NAME:
        return
    sidecar = db_path.with_name(f"._{db_path.name}")
    if not sidecar.is_file():
        return
    try:
        sidecar.unlink()
        logger.debug("removed AppleDouble sidecar %s", sidecar)
    except OSError as exc:
        logger.warning("could not remove AppleDouble sidecar %s: %s", sidecar, exc)


def _finalize(conn: sqlite3.Connection) -> None:
    """G4: pin determinism columns, then integrity_check + foreign_key_check."""
    # Pin before integrity so the published DB is the deterministic one.
    _pin_deterministic_columns(conn)

    # Prefer database.finalize if the database worker provided it.
    from rb2engine.writer import database as database_mod

    finalize = getattr(database_mod, "finalize", None)
    if callable(finalize):
        finalize(conn)
        return

    conn.commit()
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise FatalError(f"PRAGMA integrity_check failed: {row!r}")
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise FatalError(f"PRAGMA foreign_key_check reported {len(fk)} issue(s)")


def _sha256_file(path: Path) -> str:
    """Streamed sha256 — a staged m.db can run to hundreds of MB."""
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _fsync_file(path: Path) -> None:
    """Flush file contents to stable storage before the rename.

    Best-effort by design. fsync semantics vary across platforms and
    filesystems — Windows in particular can reject an fsync on a handle
    opened this way with EBADF. Durability is a nice-to-have here; the
    correctness guarantee comes from os.replace() being atomic against
    process failure. Never let a failed fsync abort a completed conversion.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        logger.debug("fsync skipped for %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("fsync unsupported for %s: %s", path, exc)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _fsync_dir(path: Path) -> None:
    """fsync the directory so the rename is durable (POSIX only, best-effort).

    Directory fsync is a POSIX concept and is not meaningful on Windows, where
    os.replace() is backed by MoveFileEx and is atomic without it. Skipping
    entirely on Windows avoids an EBADF that would otherwise fail a conversion
    that had already succeeded.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        logger.debug("dir fsync skipped for %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("dir fsync unsupported for %s: %s", path, exc)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _rmtree(path: Path) -> None:
    """Remove a directory tree (local helper; avoids shutil import side effects)."""
    if not path.exists():
        return
    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            child.unlink(missing_ok=True)
    path.rmdir()
