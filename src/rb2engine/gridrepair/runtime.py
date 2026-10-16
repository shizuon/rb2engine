"""Offline BeatThis small0 inference using ONNX Runtime and NumPy.

Pre/postprocessing follows CPJKU/beat_this 1.1.0 (MIT); see assets/LICENSE.
The model is exported at build-development time, never on the user's machine.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from rb2engine.errors import FatalError

ASSETS = Path(__file__).parent / "assets"
RATE = 22050
FPS = 50


def peaks(logits: np.ndarray) -> np.ndarray:
    """Upstream minimal peak selection, including adjacent-peak averaging."""
    local = np.lib.stride_tricks.sliding_window_view(
        np.pad(logits, 3, constant_values=-np.inf),
        7,
    ).max(axis=1)
    indices = np.flatnonzero((logits == local) & (logits > 0))
    result: list[float] = []
    count = 0
    mean = 0.0
    for index in indices:
        if count and index - mean <= 1:
            count += 1
            mean += (index - mean) / count
        else:
            if count:
                result.append(mean)
            mean, count = float(index), 1
    if count:
        result.append(mean)
    return np.asarray(result, dtype=np.float64) / FPS


class BeatDetector:
    def __init__(self) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise FatalError(
                "Grid repair needs the grid-repair extra or the desktop release."
            ) from exc
        try:
            manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
            for name in ("small0.onnx", "frontend.npz"):
                expected = manifest["files"][name]
                if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != expected:
                    raise FatalError("Grid repair model files are missing or changed; reinstall.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FatalError("Cannot read grid repair model files; reinstall.") from exc
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(ASSETS / "small0.onnx"),
            options,
            providers=["CPUExecutionProvider"],
        )
        with np.load(ASSETS / "frontend.npz", allow_pickle=False) as frontend:
            self.window = frontend["window"]
            self.mel = frontend["mel"]
        self.identity = {
            "provider": "BeatThis",
            "model": "small0",
            "source_version": "1.1.0",
            "runtime": "onnxruntime",
            "runtime_version": ort.__version__,
            "model_sha256": manifest["files"]["small0.onnx"],
            "frontend_sha256": manifest["files"]["frontend.npz"],
        }

    def spectrogram(self, signal: np.ndarray) -> np.ndarray:
        signal = np.asarray(signal, dtype=np.float32)
        if signal.ndim != 1 or len(signal) < 1024 or not np.isfinite(signal).all():
            raise ValueError("Invalid mono PCM for beat detection")
        frames = np.lib.stride_tricks.sliding_window_view(
            np.pad(signal, 512, mode="reflect"),
            1024,
        )[::441]
        spectrum = np.abs(np.fft.rfft(frames * self.window, axis=1)).astype(np.float32)
        return np.log1p((spectrum / 32) @ self.mel * 1000).astype(np.float32)

    def logits(self, signal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        spect = self.spectrogram(signal)
        starts = np.arange(-6, len(spect) - 6, 1488)
        if len(spect) > 1488:
            starts[-1] = len(spect) - 1494
        outputs = [np.full(len(spect), -1000, dtype=np.float32) for _ in range(2)]
        for start in reversed(starts):
            chunk = spect[max(start, 0) : min(start + 1500, len(spect))]
            chunk = np.pad(
                chunk, ((max(0, -start), max(0, min(6, start + 1500 - len(spect)))), (0, 0))
            )
            predicted = self.session.run(None, {"spectrogram": chunk[None]})
            for output, prediction in zip(outputs, predicted, strict=True):
                output[start + 6 : start + 1494] = prediction[0, 6:-6]
        return outputs[0], outputs[1]

    def predict(self, signal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        beat, downbeat = (peaks(logit) for logit in self.logits(signal))
        if len(beat):
            downbeat = np.unique([beat[np.argmin(abs(beat - time))] for time in downbeat])
        return beat, downbeat

    def analyze(self, path: Path, sample_rate: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Use the same decoded sample clock as Engine overview generation."""
        from rb2engine.overviews.decoder import decode, executable
        from rb2engine.overviews.runtime import subprocess_options

        with tempfile.TemporaryDirectory(prefix="rb2engine-grid-") as scratch:
            audio = decode(path, Path(scratch))
            try:
                if audio.rate != sample_rate:
                    raise ValueError("Source and decoded sample rates disagree")
                duration = min(audio.decoded_frames, audio.processed_frames) / audio.rate
                if not 90 <= duration <= 5400:
                    raise ValueError("Grid repair requires audio between 90 seconds and 90 minutes")
                length = min(45.0, duration / 3)
                starts = [0.0, (duration - length) / 2, duration - length]
                passages = []
                for start in starts:
                    first = round(start * audio.rate)
                    last = min(round((start + length) * audio.rate), len(audio.pcm))
                    mono = audio.pcm[first:last].mean(axis=1).astype("<f4")
                    command = [
                        executable("ffmpeg"),
                        "-v",
                        "error",
                        "-nostdin",
                        "-f",
                        "f32le",
                        "-ar",
                        str(audio.rate),
                        "-ac",
                        "1",
                        "-i",
                        "pipe:0",
                        "-ar",
                        str(RATE),
                        "-f",
                        "f32le",
                        "pipe:1",
                    ]
                    result = subprocess.run(  # noqa: S603 - fixed args, no shell
                        command,
                        input=mono.tobytes(),
                        capture_output=True,
                        timeout=60,
                        check=True,
                        **{
                            k: v
                            for k, v in subprocess_options().items()
                            if k not in ("encoding", "errors")
                        },
                    )
                    signal = np.frombuffer(result.stdout, dtype="<f4")
                    beats, downbeats = self.predict(signal)
                    # Discard context boundaries before judging agreement.
                    beats = beats[(beats >= 3) & (beats < length - 3)] + first / audio.rate
                    downbeats = downbeats[(downbeats >= 3) & (downbeats < length - 3)]
                    downbeats += first / audio.rate
                    passages.append(
                        {
                            "start_seconds": first / audio.rate,
                            "duration_seconds": length,
                            "beats": beats.tolist(),
                            "downbeats": downbeats.tolist(),
                        }
                    )
                clock = {
                    "sample_rate": audio.rate,
                    "codec": audio.plan["codec"],
                    "clock_policy": audio.plan["clock_policy"],
                    "reader_start_offset": audio.plan["reader_start_offset"],
                }
                return passages, clock
            finally:
                # Windows cannot delete a temporary file while its mmap is open.
                audio.pcm._mmap.close()
