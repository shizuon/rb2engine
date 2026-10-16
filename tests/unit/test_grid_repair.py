"""Agreement, conservative abstention, source preservation and verification."""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import sqlite3
import sys

import numpy as np
import pytest
from click.testing import CliRunner

from rb2engine.cli import main
from rb2engine.desktop.service import DesktopService
from rb2engine.errors import FatalError
from rb2engine.gridrepair import repair, runtime
from rb2engine.ir import SourceBeatgrid
from rb2engine.reader.library import read_library
from rb2engine.report import REPORT_FILENAME
from rb2engine.verify import verify_library
from rb2engine.writer.blobs import decode_beat_data, encode_beat_data
from tests.desktop_fixture import make_drive
from tests.grid_fixture import make_grid_drive
from tests.unit.test_desktop import manifest, wait


@pytest.fixture
def library(tmp_path):
    return read_library(make_drive(tmp_path / "Grid USB 中文 #1 %", analysis=True, duration=96))


def predictions(track, offsets=(-0.08, -0.08, -0.08), phases=(3, 3, 3)):
    times = np.array([b.sample_offset / track.sample_rate for b in track.beatgrid.beats])
    labels = np.array([b.beat_in_bar for b in track.beatgrid.beats])
    passages = []
    for start, offset, phase in zip((0, 32, 64), offsets, phases, strict=True):
        selected = (times > start + 3) & (times < start + 29)
        passages.append(
            {
                "start_seconds": start,
                "duration_seconds": 32,
                "beats": (times[selected] + offset).tolist(),
                "downbeats": (times[selected & (labels == phase)] + offset).tolist(),
            }
        )
    return passages


def mock_detector(monkeypatch, passages, error=None):
    class Detector:
        def __init__(self):
            self.identity = {"provider": "BeatThis", "model": "test-double"}

        def analyze(self, path, sample_rate):
            if error:
                raise error
            return copy.deepcopy(passages), {"sample_rate": sample_rate}

    monkeypatch.setattr(runtime, "BeatDetector", Detector)


@pytest.mark.parametrize("mode", ["strict", "majority"])
@pytest.mark.parametrize("offset,phase", [(-0.08, 3), (0.08, 1), (0, 3), (0, 1), (0.01, 1)])
def test_uniform_shift_and_phase_preserve_tempo_and_original(library, mode, offset, phase):
    track = library.tracks[1]
    result = repair.assess(track, predictions(track, (offset,) * 3, (phase,) * 3), mode=mode)
    assert result["sample_shift"] == (round(offset * 44100) if abs(offset) > 0.02 else 0)
    assert result["bar_phase_shift"] == (1 - phase) % 4
    assert result["supporting_windows"] == [0, 1, 2]
    shifted = repair.shifted_grid(track, result["sample_shift"], result["bar_phase_shift"])
    assert [b.bpm for b in shifted.beats] == [b.bpm for b in track.beatgrid.beats]
    assert np.array_equal(
        np.diff([b.sample_offset for b in shifted.beats]),
        np.diff([b.sample_offset for b in track.beatgrid.beats]),
    )
    assert track.adjusted_beatgrid is None


@pytest.mark.parametrize("bad_index", [0, 1, 2])
@pytest.mark.parametrize("problem", ["phase", "offset", "empty", "half_tempo", "double_tempo"])
def test_only_majority_accepts_two_matching_passages(library, bad_index, problem):
    track = library.tracks[1]
    p = predictions(track)
    if problem == "phase":
        p[bad_index] = predictions(track, phases=(1, 1, 1))[bad_index]
    elif problem == "offset":
        p[bad_index] = predictions(track, offsets=(0.06, 0.06, 0.06))[bad_index]
    elif problem == "empty":
        p[bad_index]["downbeats"] = []
    elif problem == "half_tempo":
        p[bad_index]["beats"] = p[bad_index]["beats"][::2]
    else:
        beats = np.array(p[bad_index]["beats"])
        p[bad_index]["beats"] = np.sort(np.concatenate((beats, beats + 0.25))).tolist()
    assert repair.assess(track, p)["status"] == "skipped"
    majority = repair.assess(track, p, mode="majority")
    assert majority["status"] == "corrected"
    assert majority["sample_shift"] == -3528
    assert majority["supporting_windows"] == [i for i in range(3) if i != bad_index]


@pytest.mark.parametrize("mode", ["strict", "majority"])
def test_weak_or_conflicting_evidence_never_repairs(library, mode):
    track = library.tracks[1]
    assert (
        repair.assess(track, predictions(track, phases=(1, 2, 3)), mode=mode)["status"] == "skipped"
    )
    assert (
        repair.assess(track, predictions(track, offsets=(-0.1, 0, 0.1)), mode=mode)["status"]
        == "skipped"
    )
    p = predictions(track)
    for w in p:
        w["beats"] = [b + (0.1 if i % 2 else -0.1) for i, b in enumerate(w["beats"])]
    assert repair.assess(track, p, mode=mode)["status"] == "skipped"
    assert (
        repair.assess(track, predictions(track, offsets=(0.24,) * 3), mode=mode)["status"]
        == "skipped"
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing", "missing_or_short_grid"),
        ("short", "missing_or_short_grid"),
        ("rate", "unsupported_sample_rate"),
        ("path", "audio_missing"),
        ("nan", "invalid_grid"),
        ("reverse", "invalid_grid"),
        ("variable", "variable_tempo_grid"),
        ("slow", "tempo_outside_supported_range"),
        ("nonlinear", "nonlinear_grid"),
        ("phase", "bar_phase_changes"),
    ],
)
def test_eligibility_abstains(library, mutation, reason):
    track = library.tracks[1]
    beats = list(track.beatgrid.beats)
    if mutation == "missing":
        track = dataclasses.replace(track, beatgrid=None)
    elif mutation == "rate":
        track = dataclasses.replace(track, sample_rate=32000)
    elif mutation == "path":
        track = dataclasses.replace(track, resolved_path=None)
    else:
        if mutation == "short":
            beats = beats[:10]
        elif mutation == "nan":
            beats[1] = dataclasses.replace(beats[1], bpm=float("nan"))
        elif mutation == "reverse":
            beats.reverse()
        elif mutation == "variable":
            beats[1] = dataclasses.replace(beats[1], bpm=130)
        elif mutation == "slow":
            beats = [dataclasses.replace(b, sample_offset=b.sample_offset * 4) for b in beats]
        elif mutation == "nonlinear":
            beats[50] = dataclasses.replace(beats[50], sample_offset=beats[50].sample_offset + 500)
        elif mutation == "phase":
            beats[10] = dataclasses.replace(beats[10], beat_in_bar=0)
        track = dataclasses.replace(track, beatgrid=SourceBeatgrid(beats, False))
    assert repair.eligibility(track) == reason
    assert repair.assess(track, [])["reason"] == reason


def test_passage_geometry_and_values(library):
    track = library.tracks[1]
    assert repair.assess(track, [])["reason"] == "three_passages_required"
    p = predictions(track)
    p[1]["start_seconds"] = 1
    assert repair.assess(track, p)["reason"] == "overlapping_or_short_passages"
    p = predictions(track)
    p[0]["beats"][2] = float("nan")
    assert repair.assess(track, p)["status"] == "skipped"
    with pytest.raises(FatalError, match="mode"):
        repair.assess(track, p, mode="invalid")


def test_conversion_verify_mutation_and_restoration(library, monkeypatch):
    root = library.drive_root
    before = manifest(root)
    mock_detector(monkeypatch, predictions(library.tracks[1]))
    service = DesktopService()
    service.inspect_drive(str(root))
    wait(service)
    service.convert_drive(False, True, False, False, False, True, "majority")
    state = wait(service)
    assert state["status"] in ("complete", "warning"), state
    assert state["result"]["grid_repair"]["corrected"] == 1
    assert "Grid repair (majority)" in state["details"]
    assert verify_library(root).exit_code == 0
    database = root / "Engine Library/Database2/m.db"
    with sqlite3.connect(database) as conn:
        original_blob, cues = conn.execute(
            "SELECT beatData, quickCues FROM PerformanceData"
        ).fetchone()
        assert conn.execute("SELECT bpmAnalyzed FROM Track").fetchone() == (120.0,)
        data = decode_beat_data(original_blob)
        assert data.default_beat_grid != data.adjusted_beat_grid
        first, last = data.adjusted_beat_grid.markers
        period = (last.sample_offset - first.sample_offset) / (last.beat_number - first.beat_number)
        assert period == 22050
        assert abs(first.sample_offset - first.beat_number * period - .17 * 44100) < 1e-6
        # A mutation only in the adjusted grid must fail verification.
        modified = dataclasses.replace(data, adjusted_beat_grid=data.default_beat_grid)
        conn.execute("UPDATE PerformanceData SET beatData=?", (encode_beat_data(modified),))
    verified = verify_library(root)
    assert verified.exit_code == 1
    assert any(d.field == "beatgrid.adjusted_markers" for d in verified.discrepancies)
    assert manifest(root) == before
    service.convert_drive(False, True, True)
    assert wait(service)["status"] in ("complete", "warning")
    assert verify_library(root).exit_code == 0
    with sqlite3.connect(database) as conn:
        blob, restored_cues = conn.execute(
            "SELECT beatData, quickCues FROM PerformanceData"
        ).fetchone()
        restored = decode_beat_data(blob)
        assert restored.default_beat_grid == data.default_beat_grid == restored.adjusted_beat_grid
        assert restored_cues == cues


def test_replay_and_tamper_guards(library, monkeypatch):
    mock_detector(monkeypatch, predictions(library.tracks[1]))
    adjusted, record = repair.repair_library(library)
    assert repair.replay_repairs(library, record) == adjusted
    repair.validate_inputs(adjusted, record)
    for key, value in [("schema", 0), ("mode", "invalid"), ("tracks", [])]:
        with pytest.raises(FatalError):
            repair.replay_repairs(library, {**record, key: value})
    for key, value in [
        ("sample_shift", 50000),
        ("bar_phase_shift", 4),
        ("track_id", 99),
        ("sample_rate", 48000),
        ("status", "bad"),
        ("source_grid_sha256", "changed"),
        ("audio_sha256", "changed"),
    ]:
        altered = copy.deepcopy(record)
        altered["tracks"][0][key] = value
        with pytest.raises(FatalError):
            repair.replay_repairs(library, altered)
    with pytest.raises(FatalError, match="duplicate"):
        repair.replay_repairs(library, {**record, "tracks": record["tracks"] * 2})
    audio = library.tracks[1].resolved_path
    audio.write_bytes(audio.read_bytes() + b"changed")
    with pytest.raises(FatalError, match="Audio changed"):
        repair.validate_inputs(adjusted, record)


def test_record_hash_tamper_fails_verification(library, monkeypatch):
    mock_detector(monkeypatch, predictions(library.tracks[1]))
    result = CliRunner().invoke(main, ["convert", str(library.drive_root), "--repair-grid"])
    assert result.exit_code == 0, result.output
    path = library.drive_root / "Engine Library" / REPORT_FILENAME
    report = json.loads(path.read_text())
    report["grid_repair"]["mode"] = "majority"
    path.write_text(json.dumps(report))
    with pytest.raises(FatalError, match="record is missing or changed"):
        verify_library(library.drive_root)


def test_off_and_dry_run_never_load_model(library, monkeypatch):
    def forbidden():
        raise AssertionError("Must not load model when off or dry run")

    monkeypatch.setattr(runtime, "BeatDetector", forbidden)
    runner = CliRunner()
    for flags in (["--repair-grid", "--dry-run"], []):
        result = runner.invoke(main, ["convert", str(library.drive_root), *flags])
        assert result.exit_code == 0, result.output


def test_analysis_failure_retains_original_and_runtime_missing_is_fatal(library, monkeypatch):
    mock_detector(monkeypatch, [], ValueError("bad PCM"))
    adjusted, report = repair.repair_library(library)
    assert adjusted == library
    assert report["failed"] == 1
    with pytest.raises(FatalError, match="mode"):
        repair.repair_library(library, mode="invalid")
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    # Import guard is tested independently of the detector double.
    from importlib import reload

    reload(runtime)
    with pytest.raises(FatalError, match="extra"):
        runtime.BeatDetector()


def test_runtime_assets_spectrogram_peaks_and_silence(monkeypatch, tmp_path):
    detector = runtime.BeatDetector()
    assert detector.identity["provider"] == "BeatThis"
    assert np.array_equal(
        runtime.peaks(np.array([-1, 1, 1, -1, -1, -1, -1, 2.0])), np.array([0.03, 0.14])
    )
    for signal in (np.zeros(5), np.ones((1024, 2)), np.full(1024, np.nan)):
        with pytest.raises(ValueError):
            detector.spectrogram(signal)
    beat, downbeat = detector.predict(np.zeros(22050 * 32, dtype=np.float32))
    assert len(beat) == len(downbeat) == 0
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "manifest.json").write_text(json.dumps({"files": {"small0.onnx": "bad"}}))
    (assets / "small0.onnx").write_bytes(b"changed")
    monkeypatch.setattr(runtime, "ASSETS", assets)
    with pytest.raises(FatalError, match="changed"):
        runtime.BeatDetector()


def test_actual_audio_model_repairs_authored_grid(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Native decoder unavailable; frozen desktop smoke covers this on both OSes")
    root = make_grid_drive(tmp_path / "Authored Grid 中文 %")
    library = read_library(root)
    before = manifest(root)
    corrected, record = repair.repair_library(library)
    assert record["corrected"] == 1 and record["failed"] == 0, record
    row = record["tracks"][0]
    assert -100 <= row["time_shift_ms"] <= -50
    assert row["bar_phase_shift"] == 2
    assert row["supporting_windows"] == [0, 1, 2]
    assert corrected.tracks[1].beatgrid == library.tracks[1].beatgrid
    assert corrected.tracks[1].cues == library.tracks[1].cues
    assert manifest(root) == before
    repair.validate_inputs(corrected, record)
