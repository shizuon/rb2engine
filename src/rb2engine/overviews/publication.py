"""Reviewable waveform-only publication with durable backup and recovery.

SQLite and an RGB file cannot share an atomic commit. A durable journal records
both old and new bytes before mutation. Recovery accepts only those two states
and refuses to overwrite concurrent edits. Audio is always read-only.
"""

import base64
import datetime
import hashlib
import json
import math
import os
import platform
import sqlite3
import struct
import subprocess
import tempfile
import uuid
import zlib
from contextlib import closing, contextmanager
from pathlib import Path

from .formats import digest, unpack
from .generator import verify_entry


def reject_symlinks(path):
    """Check lexical paths before resolving, including every existing parent."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Publication path may not traverse a symlink")
    return path


def database_path(path):
    path = reject_symlinks(path).resolve(strict=True)
    if (path.name, path.parent.name, path.parent.parent.name) != (
        "m.db",
        "Database2",
        "Engine Library",
    ):
        raise ValueError("Target must be Engine Library/Database2/m.db")
    if not path.is_file():
        raise ValueError("Database must be a regular file")
    return path


def positive_integer(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def writable_platform():
    if platform.system() not in ("Darwin", "Linux", "Windows"):
        raise RuntimeError("Overview apply/restore support macOS, Linux and Windows only")


def plan_folder(path, database):
    path = reject_symlinks(path).resolve()
    if not path.is_relative_to(database.parent.parent) or path == database.parent.parent:
        raise ValueError("Publication directory must be inside the target Engine Library")
    return path


def now():
    return datetime.datetime.now(datetime.UTC).isoformat()


def blob_hash(value):
    return None if value is None else hashlib.sha256(value).hexdigest()


def encode(value):
    return None if value is None else base64.b64encode(value).decode("ascii")


def decode(value):
    return None if value is None else base64.b64decode(value, validate=True)


def save(path, data):
    path = reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as file:
            temp = Path(file.name)
            file.write(json.dumps(data, ensure_ascii=False, indent=2).encode())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def sync_dir(path):
    if platform.system() == "Windows":
        return  # Windows has no POSIX directory fsync; file handles are flushed above.
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def connect(path, writable=False):
    c = sqlite3.connect(
        Path(path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"),
        uri=True,
        timeout=2,
        isolation_level=None,
    )
    c.row_factory = sqlite3.Row
    if not writable:
        c.execute("PRAGMA query_only=ON")
    return c


def schema(c):
    row = c.execute("SELECT * FROM Information").fetchall()
    if len(row) != 1:
        raise ValueError("Unknown Engine database identity")
    info = dict(row[0])
    uid = str(uuid.UUID(info["uuid"]))
    version = tuple(
        info[k] for k in ("schemaVersionMajor", "schemaVersionMinor", "schemaVersionPatch")
    )
    if version != (3, 0, 2):
        raise ValueError(f"Database schema {version} has not been validated")
    if uid != info["uuid"]:
        raise ValueError("Unexpected database UUID")
    return uid


def fingerprint(c, protected=False, ids=()):
    """Hash every table and schema; optionally mask only permitted payloads."""
    result = {}
    names = [
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    result["schema"] = hashlib.sha256(
        repr(
            [
                tuple(r)
                for r in c.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                )
            ]
        ).encode()
    ).hexdigest()
    for name in names:
        h = hashlib.sha256()
        try:
            rows = c.execute(f"SELECT * FROM {quote(name)} ORDER BY rowid")  # noqa: S608 — quoted SQLite schema name
        except sqlite3.OperationalError:
            rows = c.execute(f"SELECT * FROM {quote(name)} ORDER BY 1")  # noqa: S608 — quoted SQLite schema name
        count = 0
        for row in rows:
            data = dict(row)
            if protected and name == "PerformanceData" and data["trackId"] in ids:
                data["overviewWaveFormData"] = "<permitted waveform>"
                raw = track_raw(data["trackData"])
                data["trackData"] = raw[16:]
            normalized = [
                (k, ("blob", len(v), blob_hash(v)) if isinstance(v, bytes) else v)
                for k, v in data.items()
            ]
            h.update(repr(normalized).encode("utf-8"))
            h.update(b"\n")
            count += 1
        result[name] = {"rows": count, "sha256": h.hexdigest()}
    return result


def track_raw(blob):
    if not isinstance(blob, bytes) or len(blob) < 5:
        raise ValueError("Track has no known timing header; analyze/import timing first")
    size = struct.unpack(">I", blob[:4])[0]
    if size not in (44, 68):
        raise ValueError("Unknown trackData layout")
    dec = zlib.decompressobj()
    raw = dec.decompress(blob[4:], size + 1)
    if len(raw) != size or not dec.eof or dec.unused_data or dec.unconsumed_tail:
        raise ValueError("Invalid trackData")
    return raw


def timing_header(blob, rate, frames):
    positive_integer(frames, "frame count")
    if rate not in (44100, 48000) or isinstance(rate, bool):
        raise ValueError("Unsupported sample rate")
    raw = track_raw(blob)
    if struct.unpack_from(">d", raw)[0] != rate:
        raise ValueError(
            "Track sample rate differs from generated audio; no cue/grid rescaling allowed"
        )
    expected = struct.pack(">dQ", rate, frames) + raw[16:]
    if expected == raw:
        return blob
    return struct.pack(">I", len(expected)) + zlib.compress(expected)


def audio_path(db, stored):
    if not isinstance(stored, str) or not stored or "://" in stored:
        raise ValueError("Only local audio paths are supported")
    return (db.parent.parent / stored).resolve(strict=True)


def rgb_path(db, uid, tid):
    positive_integer(tid, "track ID")
    if str(uuid.UUID(uid)) != uid:
        raise ValueError("Noncanonical database UUID")
    return reject_symlinks(db.parent / "OverviewData" / uid / f"{tid}.rgb")


def validate_waveform(blob, kind, frames):
    points, clock, footer, _ = unpack(blob)
    if not math.isclose(clock * 1024, frames, rel_tol=0, abs_tol=1e-6):
        raise ValueError("Waveform and track clock disagree")
    maximum = points.max(0) if kind == "tri" else points[points.astype("uint16").sum(1).argmax()]
    if footer != maximum.tobytes():
        raise ValueError("Waveform footer disagrees with channel data")


def prepare(database, entries, destination, *, expected_fingerprint=None, empty_only=False,
            replace_orphan_rgb=False):
    database = database_path(database)
    destination = plan_folder(destination, database)
    if destination.exists():
        raise ValueError("Publication directory already exists; choose a new batch")
    if not entries or any(set(x) != {"track_id", "cache_path"} for x in entries):
        raise ValueError("Publication manifest must contain track_id/cache_path entries")
    ids = [positive_integer(x["track_id"], "track ID") for x in entries]
    if len(ids) != len(set(ids)) or any(t <= 0 for t in ids):
        raise ValueError("Duplicate/invalid track IDs")
    c = connect(database)
    try:
        c.execute("BEGIN")
        uid = schema(c)
        full = fingerprint(c)
        if expected_fingerprint is not None and full != expected_fingerprint:
            raise ValueError("Engine database changed during waveform generation; retry")
        protected = fingerprint(c, True, ids)
        items = []
        for entry, tid in zip(entries, ids, strict=True):
            cache = Path(entry["cache_path"]).resolve(strict=True)
            report = verify_entry(cache)
            track = c.execute("SELECT * FROM Track WHERE id=?", (tid,)).fetchone()
            perf = c.execute("SELECT * FROM PerformanceData WHERE trackId=?", (tid,)).fetchone()
            if track is None or perf is None:
                raise ValueError(f"Missing track/performance row: {tid}")
            audio = audio_path(database, track["path"])
            if digest(audio) != report["source_sha256"]:
                raise ValueError(f"Track {tid} does not match generated audio content")
            original_td = perf["trackData"]
            td = timing_header(original_td, report["sample_rate"], report["declared_frames"])
            tri, rgb = [(cache / f"overview.{kind}").read_bytes() for kind in ("tri", "rgb")]
            for kind, blob in (("tri", tri), ("rgb", rgb)):
                validate_waveform(blob, kind, report["declared_frames"])
            target = rgb_path(database, uid, tid)
            old_rgb = target.read_bytes() if target.exists() else None
            if empty_only and (perf["overviewWaveFormData"] or
                               (old_rgb is not None and not replace_orphan_rgb)):
                raise ValueError(f"Track {tid} acquired a waveform during generation; retry")
            items.append(
                {
                    "track_id": tid,
                    "title": track["title"],
                    "stored_path": track["path"],
                    "audio": str(audio),
                    "audio_sha256": report["source_sha256"],
                    "cache_path": str(cache),
                    "cache_key": report["cache_key"],
                    "rgb": str(target),
                    "lastEditTime": track["lastEditTime"],
                    "old": {
                        "tri": encode(perf["overviewWaveFormData"]),
                        "trackData": encode(original_td),
                        "rgb": encode(old_rgb),
                    },
                    "new": {"tri": encode(tri), "trackData": encode(td), "rgb": encode(rgb)},
                    "clock_change": {
                        "before": struct.unpack_from(">dQ", track_raw(original_td)),
                        "after": [report["sample_rate"], report["declared_frames"]],
                    },
                }
            )
        plan = {
            "schema": 1,
            "created_utc": now(),
            "database": str(database),
            "uuid": uid,
            "database_fingerprint": full,
            "protected_fingerprint": protected,
            "allowed_changes": [
                "PerformanceData.overviewWaveFormData",
                "trackData first 16 decoded bytes",
                "OverviewData/<database UUID>/<track ID>.rgb",
            ],
            "items": items,
            "audio_copied": False,
            "hardware_validated": False,
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".plan-") as pending:
            staging = Path(pending)
            save(staging / "plan.json", plan)
            staging.rename(destination)
            sync_dir(destination.parent)
        return {
            "plan": str(destination / "plan.json"),
            "tracks": len(items),
            "clock_changes": sum(
                x["clock_change"]["before"] != tuple(x["clock_change"]["after"]) for x in items
            ),
            "database_written": False,
            "audio_written": False,
        }
    finally:
        c.close()


def process_commands():
    try:
        if platform.system() == "Windows":
            import csv

            from .runtime import subprocess_options

            tasklist = Path(os.environ["SYSTEMROOT"]) / "System32" / "tasklist.exe"
            result = subprocess.check_output(  # noqa: S603 - Windows system tool
                [str(tasklist), "/FO", "CSV", "/NH"], stderr=subprocess.PIPE,
                text=True, **subprocess_options(),
            )
            rows = list(csv.reader(result.splitlines()))
            if not rows or any(len(row) < 2 for row in rows):
                raise OSError("Cannot parse process list")
            return "\n".join(row[0] for row in rows)
        return subprocess.check_output(
            ["/bin/ps", "-axo", "comm="], text=True, stderr=subprocess.PIPE
        )
    except (OSError, KeyError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Cannot check running Engine process; publication requires process visibility"
        ) from exc


def is_engine_process(command):
    name = command.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name.removesuffix(".exe") in ("engine dj", "engine prime", "engineprime")


def quiescent(database):
    for line in process_commands().splitlines():
        if is_engine_process(line):
            raise ValueError("Quit Engine DJ before applying or restoring waveforms")
    for suffix in ("-wal", "-journal"):
        file = Path(str(database) + suffix)
        if file.exists() and file.stat().st_size:
            raise ValueError(
                "Active database journal; open Engine and quit normally before publication"
            )


@contextmanager
def publication_lock(database):
    """Use the same lock as base conversion; never import POSIX locks at import time."""
    writable_platform()
    from rb2engine.library_lock import library_lock

    reject_symlinks(database.parent / ".rb2engine.lock")
    with library_lock(database.parent):
        yield


def load_plan(folder):
    folder = reject_symlinks(folder).resolve(strict=True)
    plan = json.loads(reject_symlinks(folder / "plan.json").read_text(encoding="utf-8"))
    if plan.get("schema") != 1:
        raise ValueError("Unknown publication plan")
    db = database_path(plan["database"])
    plan_folder(folder, db)
    if str(db) != plan["database"]:
        raise ValueError("Publication database mapping changed")
    uid = str(uuid.UUID(plan["uuid"]))
    if uid != plan["uuid"] or not isinstance(plan["items"], list) or not plan["items"]:
        raise ValueError("Invalid publication identity or empty items")
    ids = [positive_integer(item["track_id"], "track ID") for item in plan["items"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate track IDs in plan")
    for item in plan["items"]:
        if Path(item["rgb"]) != rgb_path(db, uid, item["track_id"]):
            raise ValueError("Publication RGB mapping was changed")
        for side in ("old", "new"):
            if set(item[side]) != {"trackData", "tri", "rgb"}:
                raise ValueError("Unexpected publication fields")
        old = track_raw(decode(item["old"]["trackData"]))
        new = track_raw(decode(item["new"]["trackData"]))
        if old[16:] != new[16:]:
            raise ValueError("Plan would alter embedded key or opaque track data")
        rate, frames = struct.unpack_from(">dQ", new)
        if rate != struct.unpack_from(">d", old)[0] or rate not in (44100, 48000):
            raise ValueError("Plan cannot rescale track sample rate")
        positive_integer(frames, "frame count")
        report = verify_entry(item["cache_path"], item["cache_key"])
        if report["source_sha256"] != item["audio_sha256"]:
            raise ValueError("Plan/cache audio identity mismatch")
        for kind in ("tri", "rgb"):
            blob = decode(item["new"][kind])
            validate_waveform(blob, kind, frames)
            if (Path(item["cache_path"]) / f"overview.{kind}").read_bytes() != blob:
                raise ValueError("Plan waveform differs from verified generation")
        if (rate, frames) != (report["sample_rate"], report["declared_frames"]):
            raise ValueError("Plan track clock differs from generated audio")
    return folder, plan, db


def check_inputs(c, plan, expected_side, protected_fingerprint=None, check_protected=True):
    if schema(c) != plan["uuid"]:
        raise ValueError("Database UUID changed")
    ids = [x["track_id"] for x in plan["items"]]
    expected = (
        protected_fingerprint
        if protected_fingerprint is not None
        else plan["protected_fingerprint"]
    )
    if check_protected and fingerprint(c, True, ids) != expected:
        raise ValueError("Protected library data changed; prepare a new publication batch")
    for item in plan["items"]:
        track = c.execute("SELECT path FROM Track WHERE id=?", (item["track_id"],)).fetchone()
        if track is None:
            raise ValueError("Publication track was removed")
        audio = audio_path(Path(plan["database"]), track["path"])
        if str(audio) != item["audio"] or digest(audio) != item["audio_sha256"]:
            raise ValueError("Audio identity/path changed")
        perf = c.execute(
            "SELECT trackData,overviewWaveFormData FROM PerformanceData WHERE trackId=?",
            (item["track_id"],),
        ).fetchone()
        if perf is None:
            raise ValueError("Publication performance row was removed")
        rgb_path(Path(plan["database"]), plan["uuid"], item["track_id"])
        states = ("old", "new") if expected_side == "either" else (expected_side,)
        for field, actual in (
            ("trackData", perf["trackData"]),
            ("tri", perf["overviewWaveFormData"]),
            ("rgb", Path(item["rgb"]).read_bytes() if Path(item["rgb"]).exists() else None),
        ):
            if not any(actual == decode(item[side][field]) for side in states):
                raise ValueError(f"Unexpected concurrent change: track {item['track_id']} {field}")


def replace_rgb(path, blob):
    path = reject_symlinks(path)
    if blob is None:
        if path.exists():
            path.unlink()
            sync_dir(path.parent)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".rgb-", delete=False) as file:
            temp = Path(file.name)
            file.write(blob)
            file.flush()
            os.fsync(file.fileno())
        reject_symlinks(path)
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def mutate(c, plan, side, expected_protected=None):
    for item in plan["items"]:
        stamp = c.execute(
            "SELECT lastEditTime FROM Track WHERE id=?", (item["track_id"],)
        ).fetchone()[0]
        c.execute(
            "UPDATE PerformanceData SET overviewWaveFormData=?,trackData=? WHERE trackId=?",
            (decode(item[side]["tri"]), decode(item[side]["trackData"]), item["track_id"]),
        )
        # Engine's own trigger stamps this value. Restore it in the transaction
        # so all Track metadata, including this field, remains byte-for-byte.
        c.execute("UPDATE Track SET lastEditTime=? WHERE id=?", (stamp, item["track_id"]))
        replace_rgb(item["rgb"], decode(item[side]["rgb"]))
    expected = (
        expected_protected if expected_protected is not None else plan["protected_fingerprint"]
    )
    if fingerprint(c, True, [x["track_id"] for x in plan["items"]]) != expected:
        raise ValueError("Publication changed a protected field; transaction rolled back")
    if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Database integrity check failed")


def apply(folder):
    writable_platform()
    folder, plan, db = load_plan(folder)
    if (folder / "journal.json").exists():
        raise ValueError("Batch already attempted; verify or recover it instead of reapplying")
    with publication_lock(db):
        quiescent(db)
        c = connect(db, True)
        try:
            # Backup precedes the write transaction; recheck the complete
            # logical state after acquiring SQLite's exclusive writer lock.
            backup_file = reject_symlinks(folder / "before.sqlite3")
            if backup_file.exists():
                raise ValueError("Backup already exists; preserve it and prepare a new batch")
            with closing(sqlite3.connect(backup_file)) as backup:
                c.backup(backup)
                backup.row_factory = sqlite3.Row
                if fingerprint(backup) != plan["database_fingerprint"]:
                    raise ValueError("Backup does not match prepared database state")
            with backup_file.open("r+b") as backup_handle:
                os.fsync(backup_handle.fileno())
            sync_dir(folder)
            rgb_backups = {}
            for item in plan["items"]:
                blob = decode(item["old"]["rgb"])
                name = f"before-{item['track_id']}.rgb"
                backup_rgb = reject_symlinks(folder / name)
                if backup_rgb.exists():
                    raise ValueError("RGB backup already exists")
                if blob is not None:
                    replace_rgb(backup_rgb, blob)
                rgb_backups[str(item["track_id"])] = blob_hash(blob)
            save(
                folder / "backup.json",
                {"sha256": digest(backup_file), "rgb": rgb_backups, "created_utc": now()},
            )
            c.execute("PRAGMA synchronous=FULL")
            c.execute("BEGIN EXCLUSIVE")
            if fingerprint(c) != plan["database_fingerprint"]:
                raise ValueError("Database changed after preparation")
            check_inputs(c, plan, "old")
            journal = {
                "state": "applying",
                "plan_sha256": digest(folder / "plan.json"),
                "started_utc": now(),
            }
            save(folder / "journal.json", journal)
            try:
                mutate(c, plan, "new")
                quiescent_process_only()
                c.commit()
            except Exception:
                c.rollback()
                for item in plan["items"]:
                    replace_rgb(item["rgb"], decode(item["old"]["rgb"]))
                journal["state"] = "rolled_back"
                save(folder / "journal.json", journal)
                raise
            journal.update(state="applied", completed_utc=now())
            save(folder / "journal.json", journal)
        finally:
            c.close()
    return verify(folder)


def quiescent_process_only():
    # While our transaction is open its own rollback journal is expected.
    if any(
        is_engine_process(x)
        for x in process_commands().splitlines()
    ):
        raise ValueError("Engine opened during publication; rollback required")


def verify(folder):
    folder, plan, db = load_plan(folder)
    journal = json.loads(reject_symlinks(folder / "journal.json").read_text(encoding="utf-8"))
    if digest(folder / "plan.json") != journal["plan_sha256"]:
        raise ValueError("Publication plan changed after mutation")
    side = (
        "new"
        if journal["state"] == "applied"
        else "old"
        if journal["state"] in ("restored", "rolled_back")
        else None
    )
    if side is None:
        raise ValueError("Incomplete publication; run restore to recover")
    c = connect(db)
    try:
        c.execute("BEGIN")
        check_inputs(c, plan, side, journal.get("restore_protected_fingerprint"))
        result = {
            "state": journal["state"],
            "tracks": len(plan["items"]),
            "all_protected_fields_unchanged": True,
            "audio_hashes_match": True,
            "protected_baseline": "immediately before restore"
            if journal.get("restore_protected_fingerprint")
            else "publication preparation",
            "tri_rgb_readback_matches": True,
            "hardware_validated": False,
            "verified_utc": now(),
        }
    finally:
        c.close()
    return result


def restore(folder):
    writable_platform()
    folder, plan, db = load_plan(folder)
    journal = json.loads(reject_symlinks(folder / "journal.json").read_text(encoding="utf-8"))
    if digest(folder / "plan.json") != journal["plan_sha256"]:
        raise ValueError("Plan changed; cannot safely restore")
    backup = json.loads(reject_symlinks(folder / "backup.json").read_text(encoding="utf-8"))
    backup_file = reject_symlinks(folder / "before.sqlite3")
    if digest(backup_file) != backup["sha256"]:
        raise ValueError("Backup damaged")
    # A modified plan cannot manufacture old values to restore. The durable
    # original SQLite snapshot is the authority for the previous timing and tri.
    before = connect(backup_file)
    try:
        if schema(before) != plan["uuid"] or fingerprint(before) != plan["database_fingerprint"]:
            raise ValueError("Backup identity differs from plan")
        for item in plan["items"]:
            backup_rgb = reject_symlinks(folder / f"before-{item['track_id']}.rgb")
            blob = backup_rgb.read_bytes() if backup_rgb.exists() else None
            if blob_hash(blob) != backup["rgb"].get(str(item["track_id"])) or blob != decode(
                item["old"]["rgb"]
            ):
                raise ValueError("Plan old RGB differs from backup")
            row = before.execute(
                "SELECT trackData,overviewWaveFormData FROM PerformanceData WHERE trackId=?",
                (item["track_id"],),
            ).fetchone()
            if row is None or (row["trackData"], row["overviewWaveFormData"]) != (
                decode(item["old"]["trackData"]),
                decode(item["old"]["tri"]),
            ):
                raise ValueError("Plan old bytes differ from backup")
    finally:
        before.close()
    with publication_lock(db):
        # Opening SQLite below recovers a hot rollback journal left by a crash.
        # A normal apply still requires a cleanly closed library.
        quiescent_process_only()
        c = connect(db, True)
        try:
            c.execute("PRAGMA synchronous=FULL")
            c.execute("BEGIN EXCLUSIVE")
            check_inputs(c, plan, "either", check_protected=False)
            protected = fingerprint(c, True, [x["track_id"] for x in plan["items"]])
            journal["state"] = "restoring"
            journal["restore_protected_fingerprint"] = protected
            save(folder / "journal.json", journal)
            mutate(c, plan, "old", protected)
            quiescent_process_only()
            c.commit()
            journal.update(state="restored", restored_utc=now())
            save(folder / "journal.json", journal)
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    return verify(folder)


def check(folder):
    """Read-only alias used by the overview CLI."""
    return verify(folder)
