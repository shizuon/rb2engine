"""Exercise real SQLite triggers, preserved performance data and crash recovery."""

import copy
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import struct
import tempfile
import unittest
import wave
import zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("numpy")

from rb2engine.errors import FatalError
from rb2engine.overviews import publication as pub


class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="publication-tests-")
        cls.root = Path(cls.work.name).resolve()
        cls.audio = cls.root / "fixture.wav"
        # Publication only checks the source identity, not DSP quality. Build a
        # valid one-second PCM WAV and an independently hand-authored cache.
        # Native-output parity is tested separately against Engine fixtures.
        with wave.open(str(cls.audio), "wb") as f:
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(44100)
            f.writeframes(bytes(44100 * 2 * 2))
        source_sha = hashlib.sha256(cls.audio.read_bytes()).hexdigest()
        runtime = {"fixture": "hand-authored publication contract"}
        implementation = hashlib.sha256(b"publication fixture version 1").hexdigest()
        identity = hashlib.sha256(
            (
                source_sha + "\n" + json.dumps(runtime, sort_keys=True) + "\n" + implementation
            ).encode()
        ).hexdigest()
        cache = cls.root / "cache" / identity
        cache.mkdir(parents=True)
        # Engine overview framing is big-endian: two 1024-point counts, an
        # F64 samples/point clock, 1024 three-channel U8 points, and a footer.
        # Constant channels have the same maximum-channel and maximum-sum
        # footer, so this deliberately simple fixture is valid for tri and RGB.
        point = bytes((17, 29, 41))
        raw = struct.pack(">QQd", 1024, 1024, 44100 / 1024) + point * 1024 + point
        blob = struct.pack(">I", len(raw)) + zlib.compress(raw)
        output_sha = hashlib.sha256(blob).hexdigest()
        for kind in ("tri", "rgb"):
            (cache / f"overview.{kind}").write_bytes(blob)
        report = {
            "schema": 2,
            "cache_key": identity,
            "source_sha256": source_sha,
            "runtime": runtime,
            "implementation_sha256": implementation,
            "sample_rate": 44100,
            "declared_frames": 44100,
            "block_frames": 441,
            "outputs_sha256": {"tri": output_sha, "rgb": output_sha},
        }
        (cache / "result.json").write_text(json.dumps(report), encoding="utf-8")
        cls.entry = {**report, "cache_path": str(cache)}
        # These fields must be preserved as opaque bytes, not interpreted.
        cls.performance = tuple(
            label + bytes(range(256)) for label in (b"grid\x00", b"cues\x00", b"loops\x00")
        )

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def setUp(self):
        self.real_platform_guard = pub.writable_platform
        self.folder = Path(tempfile.mkdtemp(dir=self.root))
        self.db = self.folder / "Engine Library/Database2/m.db"
        self.db.parent.mkdir(parents=True)
        c = sqlite3.connect(self.db)
        c.executescript("""
            CREATE TABLE Information(id INTEGER PRIMARY KEY,uuid TEXT,
                schemaVersionMajor INTEGER,schemaVersionMinor INTEGER,schemaVersionPatch INTEGER);
            INSERT INTO Information VALUES(1,'f2581e47-ec76-4792-a3b4-6a501c249ab5',3,0,2);
            CREATE TABLE Track(id INTEGER PRIMARY KEY,path TEXT,title TEXT,bpm REAL,
                key INTEGER,lastEditTime INTEGER);
            CREATE TABLE PerformanceData(trackId INTEGER PRIMARY KEY,trackData BLOB,
                overviewWaveFormData BLOB,beatData BLOB,quickCues BLOB,loops BLOB);
            CREATE TABLE Playlist(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE PlaylistEntity(id INTEGER PRIMARY KEY,playlistId INTEGER,
                trackId INTEGER,position INTEGER);
            INSERT INTO Playlist VALUES(9,'Keep my set');
            INSERT INTO PlaylistEntity VALUES(1,9,1,3);
            CREATE TRIGGER perf_stamp AFTER UPDATE ON PerformanceData BEGIN
                UPDATE Track SET lastEditTime=999 WHERE id=NEW.trackId; END;
        """)
        raw = struct.pack(">dQi", 44100.0, 1050000, 23) + bytes(range(24))
        td = struct.pack(">I", len(raw)) + zlib.compress(raw)
        c.execute(
            "INSERT INTO Track VALUES(1,?,'Publication fixture',123.45,23,17)", (str(self.audio),)
        )
        c.execute(
            "INSERT INTO PerformanceData VALUES(1,?,?,?, ?,?)", (td, b"\0\0\0\0", *self.performance)
        )
        c.commit()
        c.row_factory = sqlite3.Row
        self.before = pub.fingerprint(c)
        c.close()
        self.batch = self.folder / "Engine Library/publication"
        pub.prepare(self.db, [{"track_id": 1, "cache_path": self.entry["cache_path"]}], self.batch)
        self.process_output = (
            '"system.exe","4","Services","0","100 K"\n'
            if platform.system() == "Windows" else "/sbin/launchd\n"
        )
        self.ps = patch.object(pub.subprocess, "check_output", return_value=self.process_output)
        self.ps.start()
        self.addCleanup(self.ps.stop)

    def test_apply_verify_restore_keeps_all_performance_and_playlist_data(self):
        result = pub.apply(self.batch)
        self.assertTrue(result["all_protected_fields_unchanged"])
        c = pub.connect(self.db)
        self.assertEqual(c.execute("SELECT lastEditTime FROM Track").fetchone()[0], 17)
        c.close()
        self.assertEqual(pub.restore(self.batch)["state"], "restored")
        c = pub.connect(self.db)
        self.assertEqual(pub.fingerprint(c), self.before)
        c.close()

    def test_concurrent_metadata_edit_prevents_any_write(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE Track SET bpm=128 WHERE id=1")
        with self.assertRaisesRegex(ValueError, "does not match|changed"):
            pub.apply(self.batch)
        self.assertFalse((self.db.parent / "OverviewData").exists())

    def test_running_engine_prevents_write(self):
        with (
            patch.object(
                pub.subprocess,
                "check_output",
                return_value=('"Engine DJ.exe","123","Console","1","100 K"\n'
                              if platform.system() == "Windows"
                              else "/Applications/Engine DJ.app/Contents/MacOS/Engine DJ\n"),
            ),
            self.assertRaisesRegex(ValueError, "Quit Engine"),
        ):
            pub.apply(self.batch)
        self.assertFalse((self.batch / "journal.json").exists())

    def test_foreign_rgb_edit_is_not_overwritten_on_restore(self):
        pub.apply(self.batch)
        plan = json.loads((self.batch / "plan.json").read_text())
        target = Path(plan["items"][0]["rgb"])
        target.write_bytes(b"another writer changed this")
        with self.assertRaisesRegex(ValueError, "concurrent change"):
            pub.restore(self.batch)
        self.assertEqual(target.read_bytes(), b"another writer changed this")

    def test_restore_preserves_later_bpm_cue_and_playlist_edits(self):
        pub.apply(self.batch)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE Track SET bpm=128 WHERE id=1")
            c.execute("UPDATE PerformanceData SET quickCues=? WHERE trackId=1", (b"new cue edit",))
            c.execute("UPDATE Playlist SET name='Edited set' WHERE id=9")
        result = pub.restore(self.batch)
        self.assertEqual(result["protected_baseline"], "immediately before restore")
        with closing(sqlite3.connect(self.db)) as c, c:
            self.assertEqual(c.execute("SELECT bpm FROM Track").fetchone()[0], 128)
            self.assertEqual(
                c.execute("SELECT quickCues FROM PerformanceData").fetchone()[0], b"new cue edit"
            )
            self.assertEqual(c.execute("SELECT name FROM Playlist").fetchone()[0], "Edited set")
            self.assertEqual(c.execute("SELECT lastEditTime FROM Track").fetchone()[0], 999)

    def test_damaged_cache_prevents_preparation(self):
        plan = json.loads((self.batch / "plan.json").read_text())
        plan["items"][0]["new"]["rgb"] = pub.encode(b"fake data")
        pub.save(self.batch / "plan.json", plan)
        with self.assertRaises(ValueError):
            pub.apply(self.batch)

    def read_plan(self):
        return json.loads((self.batch / "plan.json").read_text())

    def assert_database_unchanged(self):
        c = pub.connect(self.db)
        try:
            self.assertEqual(pub.fingerprint(c), self.before)
        finally:
            c.close()

    def prepare_another(self, entries=None):
        path = self.batch.with_name("second-publication")
        pub.prepare(
            self.db, entries or [{"track_id": 1, "cache_path": self.entry["cache_path"]}], path
        )
        return path

    def test_no_files_outside_engine_library_change(self):
        # The source WAV and a Pioneer-style sentinel are not publication outputs.
        sentinel = self.folder / "PIONEER/export.pdb"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"independent original library")
        before_audio = self.audio.read_bytes()
        pub.apply(self.batch)
        pub.restore(self.batch)
        self.assertEqual(sentinel.read_bytes(), b"independent original library")
        self.assertEqual(self.audio.read_bytes(), before_audio)
        self.assertEqual({p.name for p in self.folder.iterdir()}, {"PIONEER", "Engine Library"})

    def test_prepare_and_check_do_not_modify_database_or_verified_batch(self):
        self.assert_database_unchanged()
        pub.apply(self.batch)
        before = {p: p.read_bytes() for p in self.batch.rglob("*") if p.is_file()}
        database = self.db.read_bytes()
        self.assertEqual(pub.check(self.batch)["state"], "applied")
        self.assertEqual(database, self.db.read_bytes())
        self.assertEqual(before, {p: p.read_bytes() for p in self.batch.rglob("*") if p.is_file()})

    def test_prepare_rejects_noninteger_and_duplicate_ids(self):
        for value in [True, 1.9, "1", 0, -1]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive integer"):
                self.prepare_another([{"track_id": value, "cache_path": self.entry["cache_path"]}])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.prepare_another([{"track_id": 1, "cache_path": self.entry["cache_path"]}] * 2)

    def test_database_and_plan_must_stay_in_engine_library(self):
        wrong = self.folder / "not-m.db"
        shutil.copyfile(self.db, wrong)
        with self.assertRaisesRegex(ValueError, "Engine Library/Database2/m.db"):
            pub.prepare(
                wrong,
                [{"track_id": 1, "cache_path": self.entry["cache_path"]}],
                self.folder / "other",
            )
        with self.assertRaisesRegex(ValueError, "inside the target"):
            pub.prepare(
                self.db,
                [{"track_id": 1, "cache_path": self.entry["cache_path"]}],
                self.folder / "outside",
            )
        self.assert_database_unchanged()

    def test_symlink_at_rgb_or_any_parent_is_rejected(self):
        target = Path(self.read_plan()["items"][0]["rgb"])
        outside = self.folder / "outside"
        outside.mkdir()
        for path in [target.parent.parent, target.parent, target]:
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    path.symlink_to(outside, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"Symlink creation unavailable: {exc}")
                with self.assertRaisesRegex(ValueError, "symlink"):
                    pub.apply(self.batch)
                path.unlink()
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_database_unchanged()

    def test_database_symlink_and_lock_symlink_are_rejected(self):
        link = self.folder / "linked-root"
        try:
            link.symlink_to(self.folder / "Engine Library", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            pub.database_path(link / "Database2/m.db")
        lock = self.db.parent / ".rb2engine.lock"
        original = self.folder / "do-not-touch"
        original.write_bytes(b"keep")
        lock.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "symlink"):
            pub.apply(self.batch)
        self.assertEqual(original.read_bytes(), b"keep")

    def test_preparation_failure_does_not_leave_half_plan(self):
        other = self.batch.with_name("second-publication")
        with (
            patch.object(pub, "save", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.prepare_another()
        self.assertFalse(other.exists())
        self.assertFalse(list(other.parent.glob(".plan-*")))
        self.assert_database_unchanged()

    def test_backup_manifest_failure_prevents_any_library_mutation(self):
        original = pub.save

        def fail(path, data):
            if Path(path).name == "backup.json":
                raise OSError("disk full")
            return original(path, data)

        with patch.object(pub, "save", side_effect=fail), self.assertRaises(OSError):
            pub.apply(self.batch)
        self.assert_database_unchanged()
        self.assertFalse((self.db.parent / "OverviewData").exists())
        self.assertFalse((self.batch / "journal.json").exists())

    def test_missing_process_visibility_and_live_journal_fail_closed(self):
        with (
            patch.object(pub.subprocess, "check_output", side_effect=OSError("no ps")),
            self.assertRaisesRegex(RuntimeError, "process visibility"),
        ):
            pub.apply(self.batch)
        for suffix in ("-wal", "-journal"):
            f = Path(str(self.db) + suffix)
            f.write_bytes(b"live journal")
            try:
                with self.assertRaisesRegex(ValueError, "Active database journal"):
                    pub.apply(self.batch)
            finally:
                f.unlink()
        self.assert_database_unchanged()

    def test_engine_opened_mid_apply_rolls_back_both_outputs(self):
        with (
            patch.object(pub, "quiescent_process_only", side_effect=ValueError("Engine opened")),
            self.assertRaisesRegex(ValueError, "Engine opened"),
        ):
            pub.apply(self.batch)
        self.assertEqual(pub.check(self.batch)["state"], "rolled_back")
        self.assert_database_unchanged()
        self.assertFalse(Path(self.read_plan()["items"][0]["rgb"]).exists())

    def test_existing_rgb_is_backed_up_and_restored(self):
        target = Path(self.read_plan()["items"][0]["rgb"])
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old RGB bytes remain opaque when restoring")
        second = self.prepare_another()
        pub.apply(second)
        self.assertEqual(
            (second / "before-1.rgb").read_bytes(), b"old RGB bytes remain opaque when restoring"
        )
        pub.restore(second)
        self.assertEqual(target.read_bytes(), b"old RGB bytes remain opaque when restoring")
        self.assert_database_unchanged()

    def test_process_visibility_failure_mid_apply_rolls_back(self):
        with (
            patch.object(
                pub.subprocess,
                "check_output",
                side_effect=[self.process_output, OSError("ps failed")],
            ),
            self.assertRaisesRegex(RuntimeError, "process visibility"),
        ):
            pub.apply(self.batch)
        self.assert_database_unchanged()
        self.assertEqual(pub.verify(self.batch)["state"], "rolled_back")

    def test_process_visibility_failure_prevents_restore(self):
        pub.apply(self.batch)
        with (
            patch.object(pub.subprocess, "check_output", side_effect=OSError("ps failed")),
            self.assertRaisesRegex(RuntimeError, "process visibility"),
        ):
            pub.restore(self.batch)
        self.assertEqual(pub.verify(self.batch)["state"], "applied")

    def test_changed_plan_journal_or_backup_are_not_trusted(self):
        pub.apply(self.batch)
        original = self.read_plan()
        damaged = copy.deepcopy(original)
        damaged["items"][0]["title"] = "changed after apply"
        pub.save(self.batch / "plan.json", damaged)
        with self.assertRaisesRegex(ValueError, "plan changed|Plan changed"):
            pub.restore(self.batch)
        pub.save(self.batch / "plan.json", original)
        with (self.batch / "before.sqlite3").open("ab") as f:
            f.write(b"damage")
        with self.assertRaisesRegex(ValueError, "Backup damaged"):
            pub.restore(self.batch)

    def test_plan_cannot_change_keys_rate_or_add_fields(self):
        original = self.read_plan()
        for change in ("key", "rate", "id", "duplicate", "fields", "rgb_path"):
            plan = copy.deepcopy(original)
            item = plan["items"][0]
            if change in ("key", "rate"):
                raw = bytearray(pub.track_raw(pub.decode(item["new"]["trackData"])))
                if change == "key":
                    raw[16] ^= 1
                else:
                    struct.pack_into(">d", raw, 0, 48000.0)
                item["new"]["trackData"] = pub.encode(
                    struct.pack(">I", len(raw)) + zlib.compress(raw)
                )
            elif change == "id":
                item["track_id"] = 1.2
            elif change == "duplicate":
                plan["items"].append(copy.deepcopy(item))
            elif change == "fields":
                item["new"]["quickCues"] = "no"
            else:
                item["rgb"] = str(self.folder / "escaped.rgb")
            with self.subTest(change=change):
                pub.save(self.batch / "plan.json", plan)
                with self.assertRaises(ValueError):
                    pub.apply(self.batch)
                self.assert_database_unchanged()
        pub.save(self.batch / "plan.json", original)

    def test_plan_waveform_clock_footer_and_cache_bytes_checked(self):
        original = self.read_plan()
        for change in ("clock", "footer", "points"):
            plan = copy.deepcopy(original)
            item = plan["items"][0]
            blob = pub.decode(item["new"]["tri"])
            raw = bytearray(zlib.decompress(blob[4:]))
            if change == "clock":
                struct.pack_into(">d", raw, 16, 1.0)
            elif change == "footer":
                raw[-1] ^= 1
            else:
                raw[24] ^= 1
            item["new"]["tri"] = pub.encode(struct.pack(">I", len(raw)) + zlib.compress(raw))
            pub.save(self.batch / "plan.json", plan)
            with self.subTest(change=change), self.assertRaises(ValueError):
                pub.apply(self.batch)
        self.assert_database_unchanged()

    def test_forged_old_timing_is_rejected_by_durable_backup(self):
        pub.apply(self.batch)
        plan = self.read_plan()
        item = plan["items"][0]
        raw = bytearray(pub.track_raw(pub.decode(item["old"]["trackData"])))
        struct.pack_into(">Q", raw, 8, 555)
        item["old"]["trackData"] = pub.encode(struct.pack(">I", len(raw)) + zlib.compress(raw))
        pub.save(self.batch / "plan.json", plan)
        journal = json.loads((self.batch / "journal.json").read_text())
        journal["plan_sha256"] = pub.digest(self.batch / "plan.json")
        pub.save(self.batch / "journal.json", journal)
        with self.assertRaisesRegex(ValueError, "old bytes differ from backup"):
            pub.restore(self.batch)

    def test_restoring_twice_is_safe_and_reapplying_is_not(self):
        pub.apply(self.batch)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            pub.apply(self.batch)
        pub.restore(self.batch)
        pub.restore(self.batch)
        self.assert_database_unchanged()

    def test_competing_publication_lock_prevents_writes(self):
        with pub.publication_lock(self.db), self.assertRaisesRegex(FatalError, "library lock"):
            pub.apply(self.batch)
        self.assertFalse((self.batch / "journal.json").exists())

    def test_publication_apply_restore_reject_unsupported_platform_before_lock(self):
        with (
            patch.object(pub, "writable_platform", self.real_platform_guard),
            patch.object(platform, "system", return_value="UnsupportedOS"),
        ):
            with self.assertRaisesRegex(RuntimeError, "macOS, Linux and Windows"):
                pub.apply(self.batch)
            with self.assertRaisesRegex(RuntimeError, "macOS, Linux and Windows"):
                pub.restore(self.batch)
        self.assert_database_unchanged()

    def test_restore_failure_leaves_recoverable_old_new_states(self):
        pub.apply(self.batch)
        with (
            patch.object(
                pub, "quiescent_process_only", side_effect=[None, ValueError("late Engine")]
            ),
            self.assertRaisesRegex(ValueError, "late Engine"),
        ):
            pub.restore(self.batch)
        self.assertEqual(pub.restore(self.batch)["state"], "restored")
        self.assert_database_unchanged()

    def test_trigger_changing_key_rolls_back_everything(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "CREATE TRIGGER bad AFTER UPDATE ON PerformanceData BEGIN "
                "UPDATE Track SET key=9 WHERE id=NEW.trackId; END"
            )
        second = self.prepare_another()
        with self.assertRaisesRegex(ValueError, "protected field"):
            pub.apply(second)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT key FROM Track").fetchone()[0], 23)
            self.assertEqual(
                c.execute("SELECT overviewWaveFormData FROM PerformanceData").fetchone()[0],
                b"\0\0\0\0",
            )

    def test_unknown_schema_and_missing_row_stop_prepare(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE Information SET schemaVersionPatch=99")
        with self.assertRaisesRegex(ValueError, "schema"):
            self.prepare_another()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE Information SET schemaVersionPatch=2")
        with self.assertRaisesRegex(ValueError, "Missing track"):
            self.prepare_another([{"track_id": 2, "cache_path": self.entry["cache_path"]}])

    def test_forged_old_rgb_rejected_by_backup_even_if_journal_rehashed(self):
        pub.apply(self.batch)
        plan = self.read_plan()
        plan["items"][0]["old"]["rgb"] = pub.encode(b"invented original RGB")
        pub.save(self.batch / "plan.json", plan)
        journal = json.loads((self.batch / "journal.json").read_text())
        journal["plan_sha256"] = pub.digest(self.batch / "plan.json")
        pub.save(self.batch / "journal.json", journal)
        with self.assertRaisesRegex(ValueError, "old RGB differs from backup"):
            pub.restore(self.batch)

    def test_incomplete_journal_requires_recovery(self):
        pub.apply(self.batch)
        journal = json.loads((self.batch / "journal.json").read_text())
        journal["state"] = "applying"
        pub.save(self.batch / "journal.json", journal)
        with self.assertRaisesRegex(ValueError, "Incomplete publication"):
            pub.verify(self.batch)
        self.assertEqual(pub.restore(self.batch)["state"], "restored")

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process crash simulation")
    def test_crash_before_sqlite_commit_recovers_rgb_and_database(self):
        pid = os.fork()
        if pid == 0:
            original = pub.mutate

            def crash(c, plan, side):
                original(c, plan, side)
                os._exit(77)

            pub.mutate = crash
            pub.apply(self.batch)
            os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), 77)
        self.assertEqual(pub.restore(self.batch)["state"], "restored")
        c = pub.connect(self.db)
        self.assertEqual(pub.fingerprint(c), self.before)
        c.close()


if __name__ == "__main__":
    unittest.main()


class PlatformTests(unittest.TestCase):
    def test_unsupported_apply_and_restore_fail_before_filesystem_or_posix_import(self):
        with patch.object(platform, "system", return_value="UnsupportedOS"):
            for method in (pub.apply, pub.restore):
                with (
                    self.subTest(method=method.__name__),
                    self.assertRaisesRegex(RuntimeError, "macOS, Linux and Windows"),
                ):
                    method(Path("does-not-exist"))
