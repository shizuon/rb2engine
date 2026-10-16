"""Portable codec-boundary tests using hand-authored bytes and fake decoders.

No FFmpeg, compiler, audio hardware or commercial recording is needed here.
Expected values follow MPEG headers, Qt framing and the documented timing rules.
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

np = pytest.importorskip("numpy")
from rb2engine.overviews import _build, aac_backend, decoder, doctor, formats, generator


def metadata(codec="pcm_s16le", rate=44100, channels=1, **extra):
    return {
        "streams": [{"codec_name": codec, "sample_rate": str(rate), "channels": channels, **extra}]
    }


def compressed(raw):
    return struct.pack(">I", len(raw)) + zlib.compress(raw)


def overview_raw(*, count=1024, requested=1024, clock=42.0, points=None, footer=b"\0\0\0"):
    return (
        struct.pack(">QQd", count, requested, clock)
        + (bytes(3072) if points is None else points)
        + footer
    )


@pytest.mark.parametrize("word", [0, 0xFFFB0000, 0xFFFBF000, 0xFFFB9C00, 0xFFEB9000, 0xFFFD9000])
def test_mpeg_invalid_or_reserved_headers_rejected(word):
    assert decoder.mpeg_header(word) is None


@pytest.mark.parametrize(
    "word,rate,frames,frame_bytes,side",
    [
        (0xFFFB9000, 44100, 1152, 417, 32),  # MPEG1 Layer3, stereo, 128kbps
        (0xFFFB92C0, 44100, 1152, 418, 17),  # padding byte and mono side information
        (0xFFF390C0, 22050, 576, 261, 9),  # MPEG2 Layer3 mono, 80kbps
    ],
)
def test_mpeg_header_fields_follow_bit_allocation(word, rate, frames, frame_bytes, side):
    assert decoder.mpeg_header(word) == {
        "rate": rate,
        "samples_per_frame": frames,
        "frame_bytes": frame_bytes,
        "side_info_bytes": side,
    }


def xing_frame(*, flags=15, extra_bytes=0):
    """417-byte MPEG1 frame: Xing at 4+32, all optional fields, LAME gapless pads."""
    data = bytearray(417 + extra_bytes)
    struct.pack_into(">I", data, 0, 0xFFFB9000)
    data[36:40] = b"Xing"
    struct.pack_into(">I", data, 40, flags)
    at = 44
    if flags & 1:
        struct.pack_into(">I", data, at, 100)
        at += 4
    if flags & 2:
        struct.pack_into(">I", data, at, 413)
        at += 4
    if flags & 4:
        at += 100
    if flags & 8:
        at += 4
    data[at : at + 9] = b"LAME3.100"
    data[at + 21 : at + 24] = ((576 << 12) | 1152).to_bytes(3, "big")
    return data


def test_xing_location_fields_gapless_and_concatenated_bytes(tmp_path):
    audio = tmp_path / "constructed.mp3"
    audio.write_bytes(xing_frame())
    result = decoder.mp3_info(audio, {"pos": "417"})
    assert (result["frames"], result["tag_bytes"], result["start_pad"], result["end_pad"]) == (
        100,
        413,
        576,
        1152,
    )
    assert result["encoder"] == "LAME3.100" and not result["concatenated_size_mismatch"]
    audio.write_bytes(xing_frame(extra_bytes=100))
    assert decoder.mp3_info(audio, {"pos": 417})["concatenated_size_mismatch"]
    assert decoder.mp3_info(audio, {}) is None
    assert decoder.mp3_info(audio, {"pos": 416}) is None  # not a whole preceding frame
    data = xing_frame()
    data[36:40] = b"JUNK"
    audio.write_bytes(data)
    assert decoder.mp3_info(audio, {"pos": 417}) is None


def test_unknown_xing_flags_fail_instead_of_guessing(tmp_path):
    audio = tmp_path / "unknown.mp3"
    audio.write_bytes(xing_frame(flags=16))
    with pytest.raises(decoder.UnsupportedAudio, match="Unknown Xing flags"):
        decoder.mp3_info(audio, {"pos": 417})


@pytest.mark.parametrize(
    "codec,rate,channels,match",
    [
        ("aac", 96000, 2, "rate/channel"),
        ("flac", 44100, 6, "rate/channel"),
        ("vorbis", 44100, 2, "Codec not yet verified"),
    ],
)
def test_unsupported_audio_rejected_before_decode(codec, rate, channels, match):
    with pytest.raises(decoder.UnsupportedAudio, match=match):
        decoder.timing_plan("unused", metadata(codec, rate, channels))


@pytest.mark.parametrize(
    "extra,frames,policy",
    [
        ({"time_base": "1/44100", "duration_ts": 44100}, 44100, "container_float32_duration"),
        ({"duration": "1.5"}, 66150, "stream_decimal_duration"),
        ({}, None, "decoded_frame_count"),
    ],
)
def test_time_source_selection_is_explicit(extra, frames, policy):
    plan = decoder.timing_plan("unused", metadata(**extra))
    assert plan["declared_frames"] == frames and plan["clock_policy"] == policy


def test_nonpositive_declared_length_fails():
    with pytest.raises(decoder.UnsupportedAudio, match="Invalid declared length"):
        decoder.timing_plan("unused", metadata(duration="0"))


def test_mp3_gapless_compensation_requires_stream_encoder_tag(tmp_path):
    audio = tmp_path / "gapless.mp3"
    audio.write_bytes(xing_frame())
    value = metadata("mp3", time_base="1/44100", start_pts=576)
    value["packets"] = [
        {"pos": 417, "side_data_list": [{"side_data_type": "Skip Samples", "skip_samples": 576}]}
    ]
    # A LAME string inside Xing alone does not cancel the stream start timestamp.
    assert decoder.timing_plan(audio, value)["declared_frames"] == 115200 - 576
    value["streams"][0]["tags"] = {"encoder": "LAME3.100"}
    plan = decoder.timing_plan(audio, value)
    assert plan["declared_frames"] == 115200 and plan["reader_start_offset"] == 0
    assert plan["clock_policy"] == "mpeg_xing_declared_frames"


def test_aac_negative_origin_is_preserved_and_fractional_origin_rejected():
    value = metadata("aac", time_base="1/44100", duration_ts=44100)
    value["packets"] = [{"pts": -1024}]
    assert decoder.timing_plan("unused", value)["rgb_timestamp_offset"] == -1024
    value["streams"][0]["time_base"] = "1/48000"
    value["packets"] = [{"pts": -1}]
    with pytest.raises(decoder.UnsupportedAudio, match="Fractional AAC start"):
        decoder.timing_plan("unused", value)


@pytest.mark.parametrize(
    "returncode,stdout,error",
    [
        (1, "", "ffprobe could not read"),
        (0, '{"streams": []}', "No audio stream"),
    ],
)
def test_probe_errors_are_actionable(monkeypatch, returncode, stdout, error):
    monkeypatch.setattr(
        decoder.subprocess,
        "run",
        lambda *a, **_k: subprocess.CompletedProcess(a, returncode, stdout, "bad audio"),
    )
    with pytest.raises(ValueError, match=error):
        decoder.probe("audio", ffprobe="mock-probe")


def test_probe_uses_selected_tool_and_one_audio_packet(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata()), ""))
    monkeypatch.setattr(decoder.subprocess, "run", run)
    assert decoder.probe("source.wav", ffprobe="specific-probe") == metadata()
    command = run.call_args.args[0]
    assert (
        command[0] == "specific-probe" and command[command.index("-read_intervals") + 1] == "%+#1"
    )
    assert command[-1] == "source.wav" and run.call_args.kwargs["timeout"] == 45


def fake_decode(monkeypatch, pcm, *, code=0, report=None, stderr="mock warning\n"):
    def run(command, **kwargs):
        output = Path(command[-2] if report else command[-1])
        output.write_bytes(pcm)
        if report:
            Path(command[-1]).write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, code, "", stderr)

    monkeypatch.setattr(decoder.subprocess, "run", run)


@pytest.mark.parametrize(
    "pcm,code,message",
    [
        (b"", 0, "Empty or incomplete"),
        (b"abc", 0, "Empty or incomplete"),
        (b"", 1, "Audio decode failed"),
    ],
)
def test_decode_rejects_failure_and_partial_float_frame(monkeypatch, tmp_path, pcm, code, message):
    fake_decode(monkeypatch, pcm, code=code)
    with pytest.raises(ValueError, match=message):
        decoder.decode(tmp_path / "source.wav", tmp_path, metadata=metadata(), ffmpeg="fake-ffmpeg")


@pytest.mark.parametrize("physical,declared", [(2, 3), (16384, 16387), (4, 2)])
def test_pcm_eof_buffer_rule_and_truncation_without_external_decoder(
    monkeypatch, tmp_path, physical, declared
):
    pcm = np.arange(1, physical + 1, dtype="<f4")
    fake_decode(monkeypatch, pcm.tobytes())
    result = decoder.decode(
        tmp_path / "source.wav",
        tmp_path,
        metadata=metadata(time_base="1/44100", duration_ts=declared),
        ffmpeg="fake",
    )
    # A short initial buffer pads zeros; a full final buffer reuses its prefix once.
    expected = (
        [1, 2, 0] if physical == 2 else [*range(1, 16385), 1, 2, 3] if physical == 16384 else [1, 2]
    )
    assert result.pcm[:, 0].tolist() == expected
    assert result.frames == declared and result.decoded_frames == physical
    assert result.warnings[0] == "mock warning"
    result.pcm._mmap.close()


def test_implausible_declared_shortage_rejected(monkeypatch, tmp_path):
    fake_decode(monkeypatch, np.zeros(1, dtype="<f4").tobytes())
    with pytest.raises(decoder.UnsupportedAudio, match="repair file before publication"):
        decoder.decode(
            tmp_path / "source", tmp_path, metadata=metadata(duration="2"), ffmpeg="fake"
        )


ESTIMATED_MP3_WARNING = "[mp3 @ fixture] Estimating duration from bitrate, this may be inaccurate\n"


def unindexed_vbr_bytes(count=64):
    """64 MPEG1 mono frames = 73,728 samples, alternating 128/192 kbps.

    Frame sizes are floor(144000 * bitrate / 44100): 417 and 626 bytes.
    Payload contents need no codec in these structural/fake-PCM tests.
    """
    return b"".join(
        struct.pack(">I", word) + bytes(size - 4)
        for word, size in [(0xFFFB90C0, 417), (0xFFFBB0C0, 626)] * (count // 2)
    )


@pytest.mark.parametrize("estimate", [147456, 1])
def test_unindexed_vbr_duration_recovers_both_overestimate_and_underestimate(
    monkeypatch, tmp_path, estimate
):
    """A bitrate estimate must neither reject a long set nor cut its audio tail."""
    audio = tmp_path / "vbr.mp3"
    audio.write_bytes(unindexed_vbr_bytes())
    original = audio.read_bytes()
    value = metadata("mp3", time_base="1/44100", duration_ts=estimate)
    value["packets"] = [{"pos": 0}]
    fake_decode(monkeypatch, bytes(73728 * 4), stderr=ESTIMATED_MP3_WARNING)
    result = decoder.decode(audio, tmp_path, metadata=value, ffmpeg="fake")
    assert result.frames == result.decoded_frames == result.processed_frames == 73728
    assert result.plan["clock_policy"] == "mpeg_unindexed_vbr_verified_frame_count"
    assert result.plan["estimated_frames_before_recovery"] == estimate
    assert result.plan["mpeg_frame_scan"]["mpeg_frames"] == 64
    assert audio.read_bytes() == original
    result.pcm._mmap.close()


def test_unindexed_vbr_scan_excludes_only_recognized_id3_tags(tmp_path):
    audio = tmp_path / "tags.mp3"
    # ID3v2.3 has a synchsafe three-byte body; ID3v1 is exactly 128 bytes.
    prefix = b"ID3\x03\0\0\0\0\0\x03abc"
    audio.write_bytes(prefix + unindexed_vbr_bytes() + b"TAG" + bytes(125))
    assert decoder.unindexed_mp3_frames(audio, {"pos": 13}, 44100, 1) == {
        "mpeg_frames": 64,
        "samples_per_frame": 1152,
        "sample_frames": 73728,
    }


@pytest.mark.parametrize(
    "data,pos,match",
    [
        (unindexed_vbr_bytes()[:-1], 0, "incomplete MP3 frame"),
        (unindexed_vbr_bytes() + b"junk", 0, "incomplete MP3 frame"),
        (b"junk" + unindexed_vbr_bytes(), 4, "Unrecognized bytes before"),
        (b"ID3\x03\0\0\0\0\0\x01" + unindexed_vbr_bytes(), 10, "ID3 length"),
        (bytes(417) + unindexed_vbr_bytes(), 0, "incomplete MP3 frame"),
        (struct.pack(">I", 0xFFFB94C0) + bytes(380), 0, "rate/channel changes"),
        ((struct.pack(">I", 0xFFFB90C0) + bytes(413)) * 64, 0, "variable-bitrate"),
    ],
    ids=[
        "partial-frame",
        "trailing-junk",
        "leading-junk",
        "bad-id3",
        "bad-header",
        "rate-change",
        "cbr",
    ],
)
def test_unindexed_vbr_recovery_never_resyncs_or_accepts_partial_frames(tmp_path, data, pos, match):
    audio = tmp_path / "bad.mp3"
    audio.write_bytes(data)
    with pytest.raises(decoder.UnsupportedAudio, match=match):
        decoder.unindexed_mp3_frames(audio, {"pos": pos}, 44100, 1)


@pytest.mark.parametrize(
    "decoded,stderr,start,match",
    [
        (73728 - 1152, ESTIMATED_MP3_WARNING, 0, "decoded PCM count disagree"),
        (73728, ESTIMATED_MP3_WARNING + "Error while decoding MPEG frame\n", 0, "decode warnings"),
        (73728, ESTIMATED_MP3_WARNING, 1, "nonzero start offset"),
    ],
)
def test_unindexed_vbr_recovery_requires_complete_clean_decode(
    monkeypatch, tmp_path, decoded, stderr, start, match
):
    audio = tmp_path / "vbr.mp3"
    audio.write_bytes(unindexed_vbr_bytes())
    value = metadata("mp3", time_base="1/44100", duration_ts=147456, start_pts=start)
    value["packets"] = [{"pos": 0}]
    fake_decode(monkeypatch, bytes(decoded * 4), stderr=stderr)
    with pytest.raises(decoder.UnsupportedAudio, match=match):
        decoder.decode(audio, tmp_path, metadata=value, ffmpeg="fake")


def test_accurate_unindexed_mp3_retains_native_clock(monkeypatch, tmp_path):
    audio = tmp_path / "vbr.mp3"
    audio.write_bytes(unindexed_vbr_bytes())
    value = metadata("mp3", time_base="1/44100", duration_ts=73727)
    value["packets"] = [{"pos": 0}]
    fake_decode(monkeypatch, bytes(73728 * 4), stderr=ESTIMATED_MP3_WARNING)
    result = decoder.decode(audio, tmp_path, metadata=value, ffmpeg="fake")
    assert result.frames == 73727 and result.decoded_frames == 73728
    assert result.plan["clock_policy"] == "container_float32_duration"
    result.pcm._mmap.close()


def test_indexed_mp3_shortage_still_rejected(monkeypatch, tmp_path):
    audio = tmp_path / "xing.mp3"
    audio.write_bytes(xing_frame())  # Declares 100 * 1152 samples.
    value = metadata("mp3", time_base="1/44100")
    value["packets"] = [{"pos": 417}]
    fake_decode(monkeypatch, bytes(1152 * 4), stderr=ESTIMATED_MP3_WARNING)
    with pytest.raises(decoder.UnsupportedAudio, match="repair file before publication"):
        decoder.decode(audio, tmp_path, metadata=value, ffmpeg="fake")


def test_aac_decode_reports_start_and_trims_declared_length(monkeypatch, tmp_path):
    report = {"measurement_frame_dts": 1, "frames": [{"pkt_dts": 1, "nb_samples": 6}]}
    fake_decode(monkeypatch, np.arange(6, dtype="<f4").tobytes(), report=report)
    result = decoder.decode(
        tmp_path / "source.m4a",
        tmp_path,
        metadata=metadata("aac", time_base="1/44100", duration_ts=6),
        aac_reader="fake",
    )
    assert result.frames == 5 and result.plan["reader_start_offset"] == 1
    assert result.pcm[:, 0].tolist() == [0, 1, 2, 3, 4]
    assert not (tmp_path / "aac-bootstrap.json").exists()
    result.pcm._mmap.close()


@pytest.mark.parametrize(
    "frames,message",
    [
        ([], "count disagree"),
        ([{"nb_samples": 5}], "count disagree"),
        ([{"nb_samples": 6}], "no timestamp"),
    ],
)
def test_aac_frame_metadata_must_cover_exact_pcm(monkeypatch, tmp_path, frames, message):
    pcmfile = tmp_path / "raw.f32"
    pcmfile.write_bytes(np.arange(6, dtype="<f4").tobytes())
    with pytest.raises(decoder.UnsupportedAudio, match=message):
        decoder.align_aac_reads(
            metadata("aac", time_base="1/44100"), {"channels": 1}, pcmfile, frames
        )


@pytest.mark.parametrize(
    "count,frames,expected,trimmed",
    [
        (6, [{"nb_samples": 4, "pkt_dts": 0}, {"nb_samples": 2, "pkt_dts": 3}], [0, 1, 2, 3, 5], 1),
        (20000, [{"nb_samples": 20000, "pkt_dts": 0}], list(range(20000)), 0),
    ],
)
def test_aac_overlap_and_large_retained_frame_are_not_resampled(
    tmp_path, count, frames, expected, trimmed
):
    pcmfile = tmp_path / "raw.f32"
    pcmfile.write_bytes(np.arange(count, dtype="<f4").tobytes())
    report = decoder.align_aac_reads(
        metadata("aac", time_base="1/44100"), {"channels": 1}, pcmfile, frames
    )
    assert np.fromfile(pcmfile, dtype="<f4").tolist() == expected
    assert report["timestamp_trimmed_frames"] == trimmed


@pytest.mark.parametrize(
    "blob,match",
    [
        (b"", "Empty"),
        (struct.pack(">I", 1) + b"x", "Invalid waveform size"),
        (compressed(overview_raw())[:-1], "Truncated"),
        (compressed(overview_raw()) + b"extra", "Truncated"),
        (compressed(overview_raw(count=1023)), "Unknown waveform layout"),
        (compressed(overview_raw(requested=1023)), "Unknown waveform layout"),
        (compressed(overview_raw(clock=0)), "Invalid time axis"),
        (compressed(overview_raw(clock=float("nan"))), "Invalid time axis"),
    ],
)
def test_qt_container_rejects_malformed_or_ambiguous_bytes(blob, match):
    with pytest.raises(ValueError, match=match):
        formats.unpack(blob)


def test_silence_container_and_different_footer_definitions():
    # Hand-built silence has 1024 zero RGB triplets and a zero footer at clock 42.
    raw = overview_raw()
    points, clock, tail, actual = formats.unpack(compressed(raw))
    assert actual == raw and clock == 42 and tail == bytes(3)
    assert formats.pack(points, 43008, 420, "tri") == compressed(raw)
    points[0] = [100, 0, 0]
    points[1] = [0, 60, 60]
    assert formats.unpack(formats.pack(points, 43008, 420, "tri"))[2] == bytes([100, 60, 60])
    assert formats.unpack(formats.pack(points, 43008, 420, "rgb"))[2] == bytes([0, 60, 60])


@pytest.mark.parametrize(
    "points,frames,block,kind,match",
    [
        (np.zeros((1024, 3), np.uint8), 0, 420, "tri", "Invalid overview"),
        (np.zeros((1024, 3), np.uint8), 1, 0, "tri", "Invalid overview"),
        (np.zeros((1024, 3), np.uint8), 1, 420, "other", "Invalid overview"),
        (np.zeros((1023, 3), np.uint8), 1, 420, "tri", "Expected 1024"),
        (np.zeros((1024, 3), np.float32), 1, 420, "tri", "Expected 1024"),
    ],
)
def test_packing_rejects_invalid_point_shape_or_clock(points, frames, block, kind, match):
    with pytest.raises(ValueError, match=match):
        formats.pack(points, frames, block, kind)


def cache_fixture(folder):
    folder.mkdir()
    blobs = {kind: compressed(overview_raw()) for kind in ("tri", "rgb")}
    report = {
        "schema": 2,
        "declared_frames": 43008,
        "sample_rate": 44100,
        "block_frames": 420,
        "source_sha256": "constructed",
        "runtime": {},
        "implementation_sha256": "spec",
        "outputs_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in blobs.items()},
    }
    report["cache_key"] = generator.cache_key("constructed", {}, "spec")
    for kind, blob in blobs.items():
        (folder / f"overview.{kind}").write_bytes(blob)
    (folder / "result.json").write_text(json.dumps(report))
    return report


@pytest.mark.parametrize(
    "change,message",
    [
        ({"schema": 99}, "Unknown/invalid"),
        ({"sample_rate": 96000}, "Unknown/invalid"),
        ({"declared_frames": 0}, "Unknown/invalid"),
        ({"cache_key": "changed"}, "report identity"),
        ({"declared_frames": 43009}, "clock mismatch"),
    ],
)
def test_cache_report_rejects_identity_and_timing_corruption(tmp_path, change, message):
    folder = tmp_path / "cache"
    report = cache_fixture(folder)
    report.update(change)
    (folder / "result.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match=message):
        generator.verify_entry(folder)


def test_cache_rejects_wrong_expected_key_and_inconsistent_footer(tmp_path):
    folder = tmp_path / "cache"
    report = cache_fixture(folder)
    with pytest.raises(ValueError, match="Cache identity mismatch"):
        generator.verify_entry(folder, "unrelated")
    blob = compressed(overview_raw(footer=b"\1\0\0"))
    (folder / "overview.tri").write_bytes(blob)
    report["outputs_sha256"]["tri"] = hashlib.sha256(blob).hexdigest()
    (folder / "result.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="footer mismatch"):
        generator.verify_entry(folder)


@pytest.mark.parametrize("include_aac", [False, True])
def test_doctor_only_requests_aac_dependencies_when_selected(monkeypatch, include_aac):
    context = SimpleNamespace(
        runtime={"dsp_sha256": "verified"},
        library=Path("dsp"),
        aac_identity={"version": "mock"},
        ensure_aac=Mock(return_value=Path("aac")),
    )
    monkeypatch.setattr(doctor, "create_context", lambda: context)
    result = doctor.doctor(include_aac=include_aac)
    assert context.ensure_aac.call_count == int(include_aac)
    assert ("aac_runtime" in result) is include_aac
    assert result["supported_rates"] == [44100, 48000] and not result["hardware_validated"]


def test_aac_missing_pkg_config_is_actionable(monkeypatch):
    monkeypatch.setattr(_build, "supported_platform", lambda: "Linux")
    monkeypatch.setattr(aac_backend.shutil, "which", lambda _name: None)
    monkeypatch.setattr(aac_backend.Path, "is_file", lambda _path: False)
    with pytest.raises(RuntimeError, match="AAC needs pkg-config"):
        aac_backend.build()


@pytest.mark.parametrize(
    "failed_query,missing_library", [(True, False), (False, True), (False, False)]
)
def test_aac_dependency_discovery_hashes_actual_libraries(
    monkeypatch, tmp_path, failed_query, missing_library
):
    monkeypatch.setattr(_build, "supported_platform", lambda: "Linux")
    monkeypatch.setattr(aac_backend.shutil, "which", lambda _name: "mock-pkg-config")
    for name in aac_backend.LIBRARIES:
        if not missing_library:
            (tmp_path / f"{name}.so").write_bytes(name.encode())
    helper = tmp_path / "helper"
    helper.write_bytes(b"independent client")

    def query(command, **kwargs):
        answer = (
            str(tmp_path)
            if "--variable=libdir" in command
            else "1.2.3"
            if "--modversion" in command
            else '-I"folder with spaces" -lavcodec'
        )
        return subprocess.CompletedProcess(command, int(failed_query), answer, "missing headers")

    monkeypatch.setattr(aac_backend.subprocess, "run", query)
    build = Mock(return_value=(helper, {}))
    monkeypatch.setattr(_build, "cached_build", build)
    if failed_query or missing_library:
        with pytest.raises(RuntimeError, match=r"development headers|Cannot identify dynamic"):
            aac_backend.build()
        assert build.call_count == 0
    else:
        path, identity = aac_backend.build()
        assert path == helper and len(identity["libraries"]) == 3
        assert identity["flags"] == ["-Ifolder with spaces", "-lavcodec"]
        for name, library in identity["libraries"].items():
            assert library["sha256"] == hashlib.sha256(name.encode()).hexdigest()


def test_dsp_build_preserves_explicit_float_flags(monkeypatch, tmp_path):
    from rb2engine.overviews import dsp

    build = Mock(return_value=(tmp_path / "library", {}))
    monkeypatch.setattr(_build, "cached_build", build)
    assert dsp.build() == tmp_path / "library"
    assert build.call_args.kwargs["flags"] == ["-O2", "-ffp-contract=off"]
    assert dsp.block_size(44100) == 420 and dsp.block_size(48000) == 456


@pytest.mark.parametrize("kind,expected_offset", [("tri", 0), ("rgb", -1024)])
def test_dsp_callback_gets_exact_frame_clock_and_rgb_origin(monkeypatch, kind, expected_offset):
    from rb2engine.overviews import dsp

    captured = []

    class Callback:
        def __call__(self, pointer, physical, channels, rate, frames, offset, target, count):
            captured.append((physical, channels, rate, frames, offset, count))
            for i in range(count * 3):
                target[i] = 0.5
            return count

    callback = Callback()
    monkeypatch.setattr(
        dsp.ctypes,
        "CDLL",
        lambda _path: SimpleNamespace(tri_overview_blocks=callback, rgb_overview_blocks=callback),
    )
    decoded = SimpleNamespace(
        pcm=np.zeros((840, 2), np.float32),
        frames=840,
        channels=2,
        rate=44100,
        plan={"rgb_timestamp_offset": -1024},
    )
    values, step = dsp.features(decoded, kind, "mock-library")
    assert captured == [(840, 2, 44100, 840, expected_offset, 3)]
    assert step == 420 and values.shape == (3, 3) and np.all(values == 0.5)


@pytest.mark.parametrize(
    "problem,message",
    [
        ("input_nan", "Non-finite audio"),
        ("output_nan", "invalid block"),
        ("wrong_count", "invalid block"),
    ],
)
def test_dsp_rejects_invalid_pcm_and_callback_outputs(monkeypatch, problem, message):
    from rb2engine.overviews import dsp

    class Callback:
        def __call__(self, pointer, physical, channels, rate, frames, offset, target, count):
            if problem == "output_nan":
                target[0] = float("nan")
            return count - 1 if problem == "wrong_count" else count

    monkeypatch.setattr(
        dsp.ctypes, "CDLL", lambda _path: SimpleNamespace(tri_overview_blocks=Callback())
    )
    pcm = np.zeros((840, 2), np.float32)
    if problem == "input_nan":
        pcm[0, 0] = float("nan")
    decoded = SimpleNamespace(pcm=pcm, frames=840, channels=2, rate=44100, plan={})
    with pytest.raises(ValueError, match=message):
        dsp.features(decoded, "tri", "mock-library")
    with pytest.raises(ValueError, match="Unknown waveform kind"):
        dsp.features(decoded, "invalid", "mock-library")


def test_reducer_preserves_constant_mean_clips_and_truncates():
    from rb2engine.overviews import dsp

    # Every block is the same: averaging cannot change values. Clip [2,-1,.25]
    # to [1,0,.25], then truncate after multiplying by 255 -> [255,0,63].
    values = np.tile(np.array([2, -1, 0.25], np.float32), (1025, 1))
    result = dsp.reduce_points(values, 420, 420 * 1024)
    assert result.shape == (1024, 3)
    assert np.all(result == [255, 0, 63])
    with pytest.raises(ValueError, match="Invalid reduced point count"):
        dsp.reduce_points(values[:1], 420, 420 * 1024)


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_compiler_identity_captures_actual_binary_and_sdk(monkeypatch, tmp_path, system):
    compiler = tmp_path / "cc"
    compiler.write_bytes(b"compiler identity")
    monkeypatch.setattr(_build, "supported_platform", lambda: system)
    monkeypatch.setattr(_build.shutil, "which", lambda _name: str(compiler))

    def output(command, **kwargs):
        if "--find" in command:
            return str(compiler)
        if "--show-sdk-path" in command:
            return "mock-sdk"
        return "compiler 1.0\nconfiguration details"

    monkeypatch.setattr(_build.subprocess, "check_output", output)
    found, flags, identity = _build.compiler_configuration()
    assert found == str(compiler)
    assert flags == (["-isysroot", "mock-sdk"] if system == "Darwin" else [])
    assert identity["compiler_sha256"] == hashlib.sha256(b"compiler identity").hexdigest()
    assert identity["compiler_version"] == "compiler 1.0"


def test_missing_linux_compiler_fails_before_build(monkeypatch):
    monkeypatch.setattr(_build, "supported_platform", lambda: "Linux")
    monkeypatch.setattr(_build.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="C compiler required"):
        _build.compiler_configuration()


def test_failed_compilation_does_not_leave_cache_entry(monkeypatch, tmp_path):
    source = tmp_path / "source.c"
    source.write_text("int source;")
    cache = tmp_path / "build"
    monkeypatch.setattr(_build, "build_cache", lambda: cache)
    monkeypatch.setattr(_build, "supported_platform", lambda: "Linux")
    monkeypatch.setattr(_build, "compiler_configuration", lambda: ("cc", [], {}))
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "link failed"))
    monkeypatch.setattr(_build.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="compilation failed: link failed"):
        _build.cached_build(source, "fixture", shared=True, flags=["-O2"])
    assert list(cache.iterdir()) == []
    assert run.call_args.args[0][-1] == "-lm"  # linked after the source on Linux


def test_aac_link_flags_follow_source_and_do_not_require_shared_output(monkeypatch, tmp_path):
    source = tmp_path / "source.c"
    source.write_text("int main(void) { return 0; }")
    monkeypatch.setattr(_build, "build_cache", lambda: tmp_path / "build")
    monkeypatch.setattr(_build, "supported_platform", lambda: "Linux")
    monkeypatch.setattr(_build, "compiler_configuration", lambda: ("cc", [], {}))
    commands = []

    def compile_mock(command, **kwargs):
        commands.append(command)
        Path(command[command.index("-o") + 1]).write_bytes(b"test executable")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(_build.subprocess, "run", compile_mock)
    path, report = _build.cached_build(
        source,
        "aac-client",
        shared=False,
        flags=["-O2"],
        dependencies={"link_flags": ["-lavcodec"]},
    )
    assert path.name == "aac-client" and path.read_bytes() == b"test executable"
    assert commands[0][-1] == "-lavcodec" and "-shared" not in commands[0]
    assert report["binary_sha256"] == hashlib.sha256(b"test executable").hexdigest()
