"""Metadata clock and decoded PCM are deliberately separate."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np


class UnsupportedAudio(ValueError):
    pass


def executable(name):
    found = shutil.which(name)
    if not found and Path("/opt/homebrew/bin", name).is_file():
        found = str(Path("/opt/homebrew/bin", name))
    if not found:
        raise RuntimeError(f"{name} is required; install FFmpeg and put it on PATH")
    return found


def probe(path, *, ffprobe=None):
    command = [
        ffprobe or executable("ffprobe"),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-read_intervals",
        "%+#1",
        "-show_packets",
        "-show_streams",
        "-show_format",
        "-show_entries",
        "packet=pts,dts,duration,size,pos,side_data_list:"
        "stream=codec_name,sample_rate,channels,time_base,start_pts,start_time,duration_ts,duration,bit_rate:"
        "stream_tags=encoder:format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(  # noqa: S603 - argument vector; no shell
        command, capture_output=True, text=True, timeout=45, check=False
    )
    if result.returncode:
        raise ValueError(f"ffprobe could not read audio: {result.stderr[-2000:]}")
    data = json.loads(result.stdout)
    if not data.get("streams"):
        raise UnsupportedAudio("No audio stream")
    return data


def mpeg_header(word):
    if word >> 21 != 0x7FF:
        return None
    version = (word >> 19) & 3
    layer = (word >> 17) & 3
    rate_index = (word >> 10) & 3
    bitrate_index = (word >> 12) & 15
    if version == 1 or layer != 1 or rate_index == 3 or bitrate_index in (0, 15):
        return None
    base_rates = (44100, 48000, 32000)
    rate = base_rates[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
    bitrates = (
        (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        if version == 3
        else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    )
    mono = (word >> 6) & 3 == 3
    return {
        "rate": rate,
        "samples_per_frame": 1152 if version == 3 else 576,
        "frame_bytes": (144000 if version == 3 else 72000) * bitrates[bitrate_index] // rate
        + ((word >> 9) & 1),
        "side_info_bytes": (17 if mono else 32) if version == 3 else (9 if mono else 17),
    }


def mp3_info(path, first_packet):
    """Read Xing/Info only at a valid MPEG frame immediately before audio."""
    if "pos" not in first_packet:
        return None
    first = int(first_packet["pos"])
    start = max(0, first - 4096)
    with Path(path).open("rb") as f:
        f.seek(start)
        data = f.read(first - start)
    for offset in range(max(0, len(data) - 4)):
        header = mpeg_header(int.from_bytes(data[offset : offset + 4], "big"))
        if not header or offset + header["frame_bytes"] != len(data):
            continue
        at = offset + 4 + header["side_info_bytes"]
        if data[at : at + 4] not in (b"Info", b"Xing"):
            continue
        flags = int.from_bytes(data[at + 4 : at + 8], "big")
        if flags & ~15:
            raise UnsupportedAudio("Unknown Xing flags")
        cur = at + 8
        frames = size = None
        if flags & 1:
            frames = int.from_bytes(data[cur : cur + 4], "big")
            cur += 4
        if flags & 2:
            size = int.from_bytes(data[cur : cur + 4], "big")
            cur += 4
        if flags & 4:
            cur += 100
        if flags & 8:
            cur += 4
        encoder = data[cur : cur + 9].rstrip(b"\0").decode("ascii", "replace")
        start_pad = end_pad = None
        if encoder.startswith(("LAME", "Lavc", "Lavf")) and cur + 24 <= len(data):
            pads = int.from_bytes(data[cur + 21 : cur + 24], "big")
            start_pad, end_pad = pads >> 12, pads & 4095
        # Native same-generation FFmpeg discards stale tags on concatenation.
        audio_bytes = Path(path).stat().st_size - (start + offset + 4)
        concatenated = bool(
            size and audio_bytes > size and audio_bytes - size > min(audio_bytes, size) >> 4
        )
        return {
            **header,
            "tag": data[at : at + 4].decode(),
            "frames": frames,
            "tag_bytes": size,
            "encoder": encoder,
            "start_pad": start_pad,
            "end_pad": end_pad,
            "concatenated_size_mismatch": concatenated,
        }
    return None


def unindexed_mp3_frames(path, first_packet, rate, channels):
    """Count a complete, contiguous VBR stream; never resync across damage.

    Without Xing/VBRI, FFmpeg may estimate duration from early-frame bitrate.
    This fallback accepts only a plain MPEG Layer III stream with optional
    leading ID3v2 / trailing ID3v1 tags. Unknown bytes and partial frames fail.
    The caller must also compare this count with the complete decoded PCM.
    """
    if "pos" not in first_packet:
        raise UnsupportedAudio("Cannot verify unindexed MP3 without first packet position")
    first = int(first_packet["pos"])
    end = Path(path).stat().st_size
    count = samples = 0
    bitrates = set()
    with Path(path).open("rb") as f:
        # ffprobe may skip corrupt leading data. Accept only parsed ID3 tags.
        at = 0
        while at < first:
            f.seek(at)
            tag = f.read(10)
            if (
                len(tag) != 10
                or tag[:3] != b"ID3"
                or tag[3] not in (2, 3, 4)
                or any(b & 128 for b in tag[6:10])
            ):
                raise UnsupportedAudio("Unrecognized bytes before unindexed MP3 audio")
            length = 0
            for b in tag[6:10]:
                length = (length << 7) | b
            at += 10 + length + (10 if tag[3] == 4 and tag[5] & 16 else 0)
        if at != first:
            raise UnsupportedAudio("ID3 length does not match first MP3 packet")
        if end >= 128:
            f.seek(end - 128)
            if f.read(3) == b"TAG":
                end -= 128
        while at < end:
            f.seek(at)
            raw = f.read(4)
            word = int.from_bytes(raw, "big")
            header = mpeg_header(word) if len(raw) == 4 else None
            if not header or at + header["frame_bytes"] > end:
                raise UnsupportedAudio(f"Invalid or incomplete MP3 frame at byte {at}")
            frame_channels = 1 if (word >> 6) & 3 == 3 else 2
            if header["rate"] != rate or frame_channels != channels:
                raise UnsupportedAudio("MP3 rate/channel changes within stream")
            if samples and samples != header["samples_per_frame"]:
                raise UnsupportedAudio("MP3 version changes within stream")
            samples = header["samples_per_frame"]
            bitrates.add((word >> 12) & 15)
            count += 1
            at += header["frame_bytes"]
    if not count or len(bitrates) < 2:
        raise UnsupportedAudio("Duration recovery requires a complete variable-bitrate MP3")
    return {"mpeg_frames": count, "samples_per_frame": samples, "sample_frames": count * samples}


def round_fraction(value):
    return int(value + Fraction(1, 2))


def native_frames(stream, timestamp):
    """Reader's float32 time-base conversion, including truncation to int64."""
    timebase = Fraction(stream["time_base"])
    factor = np.float32(np.float32(timebase.numerator) / np.float32(timebase.denominator))
    factor = np.float32(factor * np.float32(int(stream["sample_rate"])))
    return int(np.float32(factor * np.float32(timestamp)))


def timing_plan(path, metadata):
    stream = metadata["streams"][0]
    codec = stream["codec_name"]
    rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    if rate not in (44100, 48000) or channels not in (1, 2):
        raise UnsupportedAudio("Currently verified rate/channel range: 44.1/48 kHz mono or stereo")
    if codec not in (
        "mp3",
        "aac",
        "flac",
        "pcm_s16le",
        "pcm_s24le",
        "pcm_s32le",
        "pcm_f32le",
        "pcm_s16be",
        "pcm_s24be",
    ):
        raise UnsupportedAudio(f"Codec not yet verified: {codec}")
    packets = metadata.get("packets", [])
    info = mp3_info(path, packets[0]) if codec == "mp3" and packets else None
    if info and info["frames"] and not info["concatenated_size_mismatch"]:
        frames = info["frames"] * info["samples_per_frame"]
        policy = "mpeg_xing_declared_frames"
        # Native ffmpegFileReader 0x1022220e4..108 converts stream time_base,
        # sample rate and duration to float32 in this order, then truncates.
        timebase = Fraction(stream["time_base"])
        duration_pts = round_fraction(Fraction(frames, rate) / timebase)
        frames = native_frames(stream, duration_pts)
    elif "duration_ts" in stream and "time_base" in stream:
        frames = native_frames(stream, stream["duration_ts"])
        policy = "container_float32_duration"
    elif "duration" in stream:
        frames = round_fraction(Fraction(stream["duration"]) * rate)
        policy = "stream_decimal_duration"
    else:
        frames = None
        policy = "decoded_frame_count"
    start_offset = 0
    if codec == "mp3" and frames is not None:
        # measureStreamOffset subtracts packet skip only for these *stream*
        # encoder tags. A string found inside Xing alone does not enable it.
        start_offset = native_frames(stream, stream.get("start_pts", 0))
        encoder = stream.get("tags", {}).get("encoder", "")
        if encoder.startswith(("LAME", "Lavf", "Lavc")) and packets:
            start_offset -= next(
                (
                    int(s.get("skip_samples", 0))
                    for s in packets[0].get("side_data_list", [])
                    if s.get("side_data_type") == "Skip Samples"
                ),
                0,
            )
        frames -= start_offset
    if frames is not None and frames <= 0:
        raise UnsupportedAudio("Invalid declared length")
    # Native classic callbacks subtract the reader's start timestamp. RGB
    # doesn't, so an MP4 edit-list priming packet retains its negative origin.
    origin = 0
    if codec == "aac" and packets and packets[0].get("pts", 0) < 0:
        pos = Fraction(packets[0]["pts"]) * Fraction(stream["time_base"]) * rate
        if pos.denominator != 1:
            raise UnsupportedAudio("Fractional AAC start timestamp")
        origin = int(pos)
    return {
        "codec": codec,
        "rate": rate,
        "channels": channels,
        "declared_frames": frames,
        "clock_policy": policy,
        "reader_start_offset": start_offset,
        "mpeg_info": info,
        "rgb_timestamp_offset": origin,
    }


@dataclass
class DecodedAudio:
    pcm: np.ndarray
    frames: int
    decoded_frames: int
    processed_frames: int
    rate: int
    channels: int
    plan: dict
    warnings: list


def align_aac_reads(metadata, plan, pcmfile, frames):
    """Reproduce timestamp trimming at the reader's 16,384-frame requests.

    Decoded frames may overlap in container time. The native reader drains
    retained PCM first, but does not advance its trimming clock for that drain
    (0x10222119c..1c4 versus 0x102221540..554). Normalizing every frame to its
    timestamp would therefore be wrong. This rule depends on frame metadata,
    never the song name or a measured waveform offset.
    """
    channels = plan["channels"]
    physical = pcmfile.stat().st_size // (channels * 4)
    if not frames or sum(int(f["nb_samples"]) for f in frames) != physical:
        raise UnsupportedAudio("AAC frame metadata and PCM count disagree")
    origins = []
    for f in frames:
        if "pkt_dts" not in f:
            raise UnsupportedAudio("AAC frame has no timestamp")
        origins.append(native_frames(metadata["streams"][0], f["pkt_dts"]))
    origin = origins[0]
    source = np.memmap(pcmfile, dtype="<f4", mode="r", shape=(physical, channels))
    aligned = pcmfile.with_name("aligned.f32")
    offset = index = written = pending = pending_at = skipped = 0
    with aligned.open("wb") as out:
        while index < len(frames) or pending:
            request = 16384
            read_clock = written + origin
            take = min(pending, request)
            if take:
                out.write(source[pending_at : pending_at + take].tobytes())
                pending_at += take
                pending -= take
                written += take
                request -= take
            while request and index < len(frames):
                length = int(frames[index]["nb_samples"])
                trim = min(length, max(0, read_clock - origins[index]))
                skipped += trim
                available = length - trim
                take = min(request, available)
                out.write(source[offset + trim : offset + trim + take].tobytes())
                pending_at = offset + trim + take
                pending = available - take
                offset += length
                index += 1
                request -= take
                written += take
                read_clock += take
            if request and index == len(frames) and not pending:
                break
    del source
    os.replace(aligned, pcmfile)
    return {
        "decoded_frames_before_timestamp_trimming": physical,
        "timestamp_trimmed_frames": skipped,
        "aligned_frames": written,
        "timestamp_policy": "native_buffered_read_float32_frame_clock",
    }


def decode(path, scratch, *, metadata=None, aac_reader=None, ffmpeg=None):
    """Decode to disk/mmap so large songs do not require a whole-file RAM copy."""
    path = Path(path).resolve()
    metadata = probe(path) if metadata is None else metadata
    plan = timing_plan(path, metadata)
    pcmfile = Path(scratch) / "decoded.f32"
    if plan["codec"] == "aac":
        from .aac_backend import build as build_aac

        reader = aac_reader
        if reader is None:
            reader, _ = build_aac()
        bootstrap_report = Path(scratch) / "aac-bootstrap.json"
        command = [str(reader), str(path), str(pcmfile), str(bootstrap_report)]
    else:
        command = [
            ffmpeg or executable("ffmpeg"),
            "-v",
            "warning",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            str(pcmfile),
        ]
    result = subprocess.run(  # noqa: S603 - argument vector; no shell
        command, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise ValueError(f"Audio decode failed: {result.stderr[-3000:]}")
    frame_bytes = plan["channels"] * 4
    size = pcmfile.stat().st_size
    if not size or size % frame_bytes:
        raise ValueError("Empty or incomplete decoded PCM")
    if plan["codec"] == "aac":
        plan["aac_bootstrap"] = json.loads(bootstrap_report.read_text())
        bootstrap_report.unlink()
        frame_metadata = plan["aac_bootstrap"].pop("frames")
        start_offset = native_frames(
            metadata["streams"][0], plan["aac_bootstrap"]["measurement_frame_dts"]
        )
        plan["reader_start_offset"] = start_offset
        if plan["declared_frames"] is not None:
            plan["declared_frames"] -= start_offset
            if plan["declared_frames"] <= 0:
                raise UnsupportedAudio("Invalid AAC length after start timestamp")
        plan.update(align_aac_reads(metadata, plan, pcmfile, frame_metadata))
    physical = pcmfile.stat().st_size // frame_bytes
    declared = plan["declared_frames"] or physical
    warnings = [line for line in result.stderr.splitlines() if line.strip()]
    estimate_warning = "Estimating duration from bitrate, this may be inaccurate"
    if (
        plan["codec"] == "mp3"
        and plan["mpeg_info"] is None
        and abs(declared - physical) > max(plan["rate"], declared // 100)
        and any(line.endswith(estimate_warning) for line in warnings)
    ):
        if plan["reader_start_offset"] or any(
            not line.endswith(estimate_warning) for line in warnings
        ):
            raise UnsupportedAudio("Unindexed MP3 has decode warnings or a nonzero start offset")
        packets = metadata.get("packets", [])
        scan = unindexed_mp3_frames(
            path, packets[0] if packets else {}, plan["rate"], plan["channels"]
        )
        if scan["sample_frames"] != physical:
            raise UnsupportedAudio("MP3 frame scan and decoded PCM count disagree")
        plan["estimated_frames_before_recovery"] = declared
        plan["clock_policy_before_recovery"] = plan["clock_policy"]
        plan["mpeg_frame_scan"] = scan
        plan["clock_policy"] = "mpeg_unindexed_vbr_verified_frame_count"
        plan["declared_frames"] = declared = physical
        warnings.append(
            "Unindexed VBR MP3 duration recovered from complete MPEG frames and decoded PCM"
        )
    if declared > physical:
        shortage = declared - physical
        if shortage > max(plan["rate"], declared // 100):
            raise UnsupportedAudio(
                "Declared audio exceeds decoded audio by >1s/1%; repair file before publication"
            )
        # Native processing submits the requested size after a partial read.
        # Preserve one reused read buffer, then stop submitting audio and flush.
        processed = min(declared, (physical // 16384 + 1) * 16384)
        previous = b""
        if physical >= 16384:
            with pcmfile.open("rb") as f:
                f.seek((physical - 16384) * frame_bytes)
                previous = f.read((processed - physical) * frame_bytes)
        if not previous:
            previous = bytes((processed - physical) * frame_bytes)
        with pcmfile.open("ab") as f:
            f.write(previous)
        warnings.append(
            f"Native EOF compatibility: decoded={physical}, processed={processed}, "
            f"declared={declared}"
        )
    else:
        processed = declared
    mmap = np.memmap(pcmfile, dtype="<f4", mode="r", shape=(processed, plan["channels"]))
    return DecodedAudio(
        mmap, declared, physical, processed, plan["rate"], plan["channels"], plan, warnings
    )
