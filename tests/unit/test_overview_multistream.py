"""AAC decoding must select the same first audio stream as probing and FFmpeg."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import wave
from pathlib import Path

import pytest


def _native_dependencies() -> tuple[str, str]:
    if os.environ.get("RB2ENGINE_NATIVE_RUNTIME"):
        from rb2engine.overviews.decoder import executable

        return executable("ffmpeg"), executable("ffprobe")
    system = platform.system()
    if system not in ("Darwin", "Linux"):
        pytest.skip("native waveform generation supports macOS/Linux")
    required = ["ffmpeg", "ffprobe", "pkg-config", "xcrun" if system == "Darwin" else "cc"]
    binaries = {name: shutil.which(name) for name in required}
    if not all(binaries.values()):
        pytest.skip("AAC integration needs FFmpeg, pkg-config and a C compiler")
    pkg = binaries["pkg-config"]
    for name in ("libavformat", "libavcodec", "libavutil"):
        available = subprocess.run(  # noqa: S603 - discovered executable, fixed arguments
            [pkg, "--exists", name], capture_output=True, check=False
        )
        if available.returncode:
            pytest.skip("AAC integration needs FFmpeg development headers/libraries")
        folder = Path(
            subprocess.check_output(  # noqa: S603 - discovered executable
                [pkg, "--variable=libdir", name], text=True
            ).strip()
        )
        if not any((folder / (name + suffix)).is_file() for suffix in (".dylib", ".so")):
            pytest.skip("AAC integration needs dynamic FFmpeg libraries")
    if system == "Darwin":
        available = subprocess.run(  # noqa: S603 - Xcode toolchain discovery
            [binaries["xcrun"], "--find", "clang"], capture_output=True, check=False
        )
        if available.returncode:
            pytest.skip("AAC integration needs Xcode Command Line Tools")
    return binaries["ffmpeg"], binaries["ffprobe"]


@pytest.mark.parametrize("first_rate,default_rate", [(44100, 48000), (48000, 44100)])
def test_aac_first_stream_wins_over_later_default_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_rate: int, default_rate: int
) -> None:
    """The known first tone is 440 Hz for 3 s; the default track is 880 Hz.

    Different rates and durations expose selecting one stream's PCM while
    applying another stream's clock. Expected values are the declared source
    signal, not cached decoder or waveform output.
    """
    np = pytest.importorskip("numpy")
    ffmpeg, ffprobe = _native_dependencies()
    from rb2engine.overviews.decoder import decode, probe

    monkeypatch.setenv("RB2ENGINE_OVERVIEWS_BUILD_CACHE", str(tmp_path / "native-build"))
    audio = tmp_path / "two-audio-streams.m4a"
    sources = []
    for index, (rate, frequency, seconds) in enumerate(
        ((first_rate, 440, 3), (default_rate, 880, 4))
    ):
        source = tmp_path / f"tone-{index}.wav"
        time = np.arange(rate * seconds) / rate
        pcm = np.rint(4096 * np.sin(2 * np.pi * frequency * time)).astype("<i2")
        with wave.open(str(source), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm.tobytes())
        sources.append(source)
    subprocess.run(  # noqa: S603 - fixed synthetic signals; no shell
        [
            ffmpeg,
            "-v",
            "error",
            "-i", str(sources[0]),
            "-i", str(sources[1]),
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:a",
            "aac",
            "-disposition:a:0",
            "0",
            "-disposition:a:1",
            "default",
            str(audio),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    streams = json.loads(
        subprocess.check_output(  # noqa: S603 - inspect the synthetic fixture
            [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(audio)], text=True
        )
    )["streams"]
    assert [int(s["sample_rate"]) for s in streams] == [first_rate, default_rate]
    assert [s["disposition"]["default"] for s in streams] == [0, 1]
    original = audio.read_bytes()
    metadata = probe(audio, ffprobe=ffprobe)
    assert len(metadata["streams"]) == 1
    assert int(metadata["streams"][0]["sample_rate"]) == first_rate
    scratch = tmp_path / "decode"
    scratch.mkdir()
    decoded = decode(audio, scratch, metadata=metadata)
    assert decoded.rate == first_rate
    assert decoded.frames == 3 * first_rate
    # AAC may pad the final 1024-sample packet, but not add a fourth second.
    assert 3 * first_rate <= decoded.decoded_frames < 3 * first_rate + 1024
    sample = np.asarray(decoded.pcm[first_rate : 2 * first_rate, 0])
    spectrum = np.abs(np.fft.rfft(sample))
    frequencies = np.fft.rfftfreq(len(sample), 1 / first_rate)
    peak = frequencies[spectrum.argmax()]
    assert peak == pytest.approx(440, abs=1)
    assert audio.read_bytes() == original
