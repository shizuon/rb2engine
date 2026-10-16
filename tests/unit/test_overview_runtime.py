"""Exercise the shipped decoder/helper/DSP with real audio and Unicode paths."""
from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from rb2engine.overviews import generator
from rb2engine.overviews.decoder import decode, executable
from rb2engine.overviews.formats import digest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RB2ENGINE_NATIVE_RUNTIME"), reason="bundled runtime required"
)


@pytest.mark.parametrize("extension", ["wav", "mp3", "m4a", "flac"])
def test_bundled_decoder_tone_clock_source_and_cache(tmp_path, extension):
    root = tmp_path / "音频 #1 100%"
    root.mkdir()
    audio = root / ("测试." + extension)
    rate = 44100
    time = np.arange(rate * 3) / rate
    tone = np.rint(4096 * np.sin(2 * np.pi * 440 * time)).astype("<i2")
    source = root / "source.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(np.column_stack((tone, tone)).tobytes())
    if extension == "wav":
        shutil.copy2(source, audio)
    elif extension == "mp3":
        shutil.copy2(Path(__file__).parents[1] / "fixtures/audio/authored-440hz.mp3", audio)
    else:
        codec = "aac" if extension == "m4a" else "flac"
        subprocess.run(  # noqa: S603 - packaged tool, synthetic fixture
            [executable("ffmpeg"), "-v", "error", "-i", str(source), "-c:a", codec, str(audio)],
            check=True, capture_output=True,
        )
    before = digest(audio)
    scratch = root / "scratch"
    scratch.mkdir()
    decoded = decode(audio, scratch)
    assert decoded.rate == rate
    # The MP3 fixture contains 116 MPEG frames; Engine's overview clock keeps
    # that encoded length while gapless decoding yields exactly three seconds.
    expected_frames = 116 * 1152 if extension == "mp3" else rate * 3
    assert decoded.frames == expected_frames
    assert decoded.decoded_frames >= rate * 3
    samples = np.asarray(decoded.pcm[rate:rate * 2, 0])
    peak = np.fft.rfftfreq(rate, 1 / rate)[np.abs(np.fft.rfft(samples)).argmax()]
    assert peak == pytest.approx(440, abs=1)
    del samples, decoded
    context = generator.create_context()
    first = generator.generate(audio, root / "cache", context=context)
    assert generator.verify_entry(first["cache_path"])["declared_frames"] == expected_frames
    second = generator.generate(audio, root / "cache", context=context)
    assert second["cache_hit"] and first["outputs_sha256"] == second["outputs_sha256"]
    assert digest(audio) == before
