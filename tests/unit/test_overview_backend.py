"""Optional waveform backend: native evidence, read-only inputs and honest caches."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest

from rb2engine.overviews import _build


def test_namespace_import_does_not_load_optional_dependencies() -> None:
    """Base users must be able to import rb2engine with no NumPy installed."""
    source = Path(__file__).parents[2] / "src"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import rb2engine.overviews; "
        "assert 'numpy' not in sys.modules"
    )
    subprocess.run([sys.executable, "-S", "-c", code, str(source)], check=True)  # noqa: S603


def test_windows_cache_and_compiler_guidance(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_build.platform, "system", lambda: "Windows")
    monkeypatch.delenv("RB2ENGINE_OVERVIEWS_BUILD_CACHE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert _build.build_cache() == tmp_path / "rb2engine/overviews/build"
    with pytest.raises(RuntimeError, match="desktop waveform runtime"):
        _build.compiler_configuration()


def test_build_cache_is_user_owned(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_build.platform, "system", lambda: "Linux")
    monkeypatch.delenv("RB2ENGINE_OVERVIEWS_BUILD_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert _build.build_cache() == tmp_path / "rb2engine/overviews/build"
    monkeypatch.setenv("RB2ENGINE_OVERVIEWS_BUILD_CACHE", str(tmp_path / "explicit"))
    assert _build.build_cache() == tmp_path / "explicit"


def test_native_build_cache_rejects_corruption_and_separates_toolchains(
    monkeypatch, tmp_path
) -> None:
    """Never execute a corrupt cached binary or reuse one after compiler changes."""
    source = tmp_path / "fixture.c"
    source.write_text("int fixture(void) { return 1; }\n")
    monkeypatch.setenv("RB2ENGINE_OVERVIEWS_BUILD_CACHE", str(tmp_path / "build"))
    monkeypatch.setattr(_build.platform, "system", lambda: "Darwin")
    compiler = {"compiler": "mock-cc", "compiler_sha256": "first", "platform": "Darwin"}
    monkeypatch.setattr(_build, "compiler_configuration", lambda: ("mock-cc", [], compiler))
    commands = []

    def compile_mock(command, **kwargs):
        commands.append(command)
        Path(command[command.index("-o") + 1]).write_bytes(b"mock native artifact")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(_build.subprocess, "run", compile_mock)
    first, _ = _build.cached_build(source, "fixture", shared=True, flags=["-O2"])
    again, _ = _build.cached_build(source, "fixture", shared=True, flags=["-O2"])
    assert first == again and len(commands) == 1
    assert "-Wl,-install_name,@rpath/fixture.dylib" in commands[0]
    compiler["compiler_sha256"] = "second"
    second, _ = _build.cached_build(source, "fixture", shared=True, flags=["-O2"])
    assert second != first and len(commands) == 2
    second.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="Invalid overview build cache"):
        _build.cached_build(source, "fixture", shared=True, flags=["-O2"])
    assert len(commands) == 2


@pytest.fixture
def generator():
    pytest.importorskip("numpy")
    from rb2engine.overviews import generator

    return generator


def test_context_resolves_tool_identity_once_and_rejects_change(
    generator, monkeypatch, tmp_path
) -> None:
    binary = tmp_path / "native"
    binary.write_bytes(b"binary")
    monkeypatch.setattr(_build, "supported_platform", lambda: "Linux")
    monkeypatch.setattr(generator, "build", lambda: binary)
    monkeypatch.setattr(generator, "executable", lambda _name: str(binary))
    calls = Mock(return_value="mock version\n")
    monkeypatch.setattr(generator.subprocess, "check_output", calls)
    context = generator.create_context()
    context.validate()
    context.validate()
    assert calls.call_count == 2
    binary.write_bytes(b"updated toolchain")
    with pytest.raises(RuntimeError, match="dependency changed during batch"):
        context.validate()


def test_missing_ffmpeg_is_actionable(generator, monkeypatch) -> None:
    from rb2engine.overviews import decoder

    monkeypatch.setattr(decoder.shutil, "which", lambda _name: None)
    monkeypatch.setattr(decoder.Path, "is_file", lambda _self: False)
    with pytest.raises(RuntimeError, match="install FFmpeg"):
        decoder.executable("ffmpeg")


def test_aac_dependency_is_lazily_resolved_once(generator, monkeypatch, tmp_path) -> None:
    from rb2engine.overviews import aac_backend

    binary = tmp_path / "native"
    binary.write_bytes(b"binary")
    build = Mock(return_value=(binary, {"libraries": {}}))
    monkeypatch.setattr(aac_backend, "build", build)
    context = generator.RuntimeContext(binary, {}, "source-id")
    assert build.call_count == 0
    assert context.ensure_aac() == binary
    assert context.ensure_aac() == binary
    assert build.call_count == 1


def test_context_shares_probe_and_decode_and_cache_skips_decoder(
    generator, monkeypatch, tmp_path
) -> None:
    """Cache hits still verify content but never decode the same input again."""
    import numpy as np

    from rb2engine.overviews.decoder import DecodedAudio

    audio = tmp_path / "input.wav"
    audio.write_bytes(b"read-only fixture input")
    original = audio.read_bytes()
    metadata = {"streams": [{"codec_name": "pcm_s16le"}]}
    probe = Mock(return_value=metadata)
    monkeypatch.setattr(generator, "probe", probe)
    context = generator.RuntimeContext(
        tmp_path / "library", {"ffmpeg": {"version": "mock"}}, "core"
    )
    decode_calls = []

    def decode(path, scratch, *, metadata, aac_reader, ffmpeg):
        decode_calls.append(metadata)
        (scratch / "decoded.f32").write_bytes(b"temporary")
        return DecodedAudio(
            np.zeros((4200, 2), np.float32), 4200, 4200, 4200, 44100, 2, {"codec": "pcm_s16le"}, []
        )

    monkeypatch.setattr(generator, "decode", decode)
    monkeypatch.setattr(generator, "features", lambda _decoded, _kind, _library: (None, 420))
    monkeypatch.setattr(
        generator, "reduce_points", lambda _values, _block, _frames: np.zeros((1024, 3), np.uint8)
    )
    first = generator.generate(audio, tmp_path / "cache", context=context)
    second = generator.generate(audio, tmp_path / "cache", context=context)
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert len(decode_calls) == 1 and decode_calls[0] is metadata
    assert probe.call_count == 2  # one probe per request; none hidden in decode
    assert audio.read_bytes() == original
    assert sorted(p.name for p in Path(first["cache_path"]).iterdir()) == [
        "overview.rgb",
        "overview.tri",
        "result.json",
    ]
    (Path(first["cache_path"]) / "overview.tri").write_bytes(b"corruption")
    with pytest.raises(ValueError, match="waveform hash mismatch"):
        generator.generate(audio, tmp_path / "cache", context=context)


def test_cache_identity_covers_audio_runtime_and_code(generator) -> None:
    baseline = generator.cache_key("audio", {"tool": "v1"}, "code")
    assert baseline != generator.cache_key("edited-audio", {"tool": "v1"}, "code")
    assert baseline != generator.cache_key("audio", {"tool": "v2"}, "code")
    assert baseline != generator.cache_key("audio", {"tool": "v1"}, "fixed-code")


@pytest.mark.skipif(
    not os.environ.get("RB2ENGINE_NATIVE_RUNTIME")
    and (platform.system() != "Darwin" or platform.machine() != "arm64"),
    reason="Engine native byte-exact reference is validated on Apple Silicon macOS",
)
def test_original_frequency_sweep_matches_engine_native_reference(
    generator, monkeypatch, tmp_path
) -> None:
    """Expected hashes are Engine-authored bytes, not generator output.

    Evidence: 2026-10-07 native capture of an original stereo 20-second PCM16
    frequency sweep, 44.1 kHz. Hashes cover complete decompressed 1024-point
    header, points and footer. The original audio hash independently pins the
    reconstruction below. No commercial audio or personal metadata is used.
    """
    import numpy as np

    from rb2engine.overviews.formats import unpack

    if not os.environ.get("RB2ENGINE_NATIVE_RUNTIME") and not all(
        shutil.which(tool) for tool in ("ffmpeg", "ffprobe", "xcrun")
    ):
        pytest.skip("native integration requires FFmpeg and Xcode command-line tools")
    monkeypatch.setenv("RB2ENGINE_OVERVIEWS_BUILD_CACHE", str(tmp_path / "native-build"))
    rate = 44100
    blocks = []
    frequencies = (
        25,
        35,
        50,
        70,
        100,
        140,
        200,
        280,
        400,
        560,
        800,
        1120,
        1600,
        2240,
        3200,
        4480,
        6400,
        8960,
        12800,
        17000,
    )
    for frequency in frequencies:
        t = np.arange(rate) / rate
        envelope = np.minimum(np.minimum(t / 0.02, (0.9 - t) / 0.02), 1).clip(0, 1)
        mono = 0.5 * np.sin(2 * np.pi * frequency * t) * envelope
        blocks.append(np.stack([mono, mono], axis=1))
    audio = tmp_path / "original-frequency-sweep.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(np.rint(np.concatenate(blocks) * 32767).astype("<i2").tobytes())
    source_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    assert source_hash == "1863a6cbe6a741b901367c1d62056f9d1686579121f5f39bcf61c519b38024ce"
    result = generator.generate(audio, tmp_path / "waveforms")
    expected = {
        "tri": "1922514736ef6efff0f8447f09c72f7f7a41c8e05c68a7d5e98c9a8807a3366b",
        "rgb": "99f6cd527f77b2241f441666036407ffd4cacf4074bcbb29c310754c26c27b25",
    }
    for kind, native_sha in expected.items():
        blob = (Path(result["cache_path"]) / f"overview.{kind}").read_bytes()
        assert hashlib.sha256(unpack(blob)[-1]).hexdigest() == native_sha
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == source_hash
    assert not result["audio_written"] and not result["database_written"]
