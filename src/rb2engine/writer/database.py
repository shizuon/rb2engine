"""m.db creation, Information row, transaction boundaries, integrity checks (G4).

detect_schema() reads the target database — the only correct way to choose a
schema triple. Engine DJ 4.3.0 runs 3.0.1 on the desktop library and 3.0.2 on
the stick; the app version does not determine the schema.

create_m_db() replays captured DDL and writes Information. uuid reuse is a
hard requirement: hm.db play history and OverviewData/<uuid>/ directories key
on Information.uuid.
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid as uuid_mod
from pathlib import Path

from rb2engine.errors import UnsupportedFormatError
from rb2engine.writer.schema import resolve_schema


def detect_schema(m_db_path: Path) -> tuple[int, int, int] | None:
    """Read Information schema triple from an existing m.db.

    Returns None if the file is absent, unreadable, or not an Engine library.
    Does not gate on SUPPORTED_SCHEMAS — callers use resolve_schema / create_m_db
    for G3. Always read the target; never infer from Engine app version.
    """
    path = Path(m_db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            """
            SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch
            FROM Information
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    if row is None:
        return None
    major, minor, patch = row
    if not all(isinstance(v, int) for v in (major, minor, patch)):
        return None
    return (int(major), int(minor), int(patch))


def _random_int64() -> int:
    """Opaque signed int64 for Information.currentPlayedIndiciator.

    Real Engine values are large negatives; any signed int64 is accepted.
    """
    # secrets.randbits(64) → [0, 2**64); map to signed int64 range.
    u = secrets.randbits(64)
    if u >= 2**63:
        return u - 2**64
    return u


def create_m_db(
    path: Path, *, schema: tuple[int, int, int], uuid: str | None = None
) -> sqlite3.Connection:
    """Create an Engine m.db at *path* for *schema*.

    Applies the captured DDL via executescript (tables, indexes, triggers,
    views — never reimplemented in Python), sets PRAGMA journal_mode=DELETE
    (not WAL; matches Engine on FAT32), and inserts the single Information row.

    *uuid*: REUSE the previous database's uuid when given. Engine keys play
    history (hm.db) and OverviewData/<uuid>/ directories on it; minting a new
    one orphans both. Random only on first run (when uuid is None).

    Returns an open connection; the caller owns close/commit after further
    writes. The Information insert is committed so a crash after return still
    leaves a valid empty Engine DB.
    """
    ddl_path = resolve_schema(schema)
    ddl = ddl_path.read_text(encoding="utf-8")

    # Parameter name `uuid` matches the binding contract; module is uuid_mod.
    db_uuid = uuid if uuid is not None else str(uuid_mod.uuid4())
    maj, minor, patch = schema
    played = _random_int64()

    path = Path(path)
    if path.parent and str(path) not in {":memory:", ""}:
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    try:
        # Must be DELETE, not WAL — removable FAT32 volumes lack reliable shm.
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            conn.close()
            raise UnsupportedFormatError(
                f"Could not set PRAGMA journal_mode=DELETE (got {mode!r}). "
                "WAL is not safe on typical removable Engine library volumes."
            )

        conn.executescript(ddl)

        # Column name currentPlayedIndiciator is Engine's spelling — do not "fix".
        conn.execute(
            """
            INSERT INTO Information (
                uuid,
                schemaVersionMajor,
                schemaVersionMinor,
                schemaVersionPatch,
                currentPlayedIndiciator,
                lastRekordBoxLibraryImportReadCounter
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (db_uuid, maj, minor, patch, played, None),
        )
        conn.commit()
    except Exception:
        conn.close()
        raise

    return conn
