"""Built-in embedded cover-art extraction (no mutagen).

Pulls raw image bytes from audio containers for the formats that matter on
the measured stick: MP4/M4A (`covr`) and MP3 (ID3v2 APIC/PIC). FLAC PICTURE
is supported when cheap. Read-only, lazy, never raises on corrupt input.
"""

from __future__ import annotations

import logging
import struct
import warnings
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

# Picture type 3 = front cover (ID3 / FLAC).
_FRONT_COVER = 3


def embedded_cover_bytes(path: Path) -> bytes | None:
    """Return raw embedded cover image bytes, or None if absent/unreadable.

    Never raises on malformed or truncated files — logs a warning and returns
    None. Opens the file read-only and reads only the tag region needed.
    """
    path = Path(path)
    try:
        with path.open("rb") as f:
            return _extract_from_file(f, path)
    except OSError as exc:
        _warn(f"tags: cannot open {path}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - corrupt tags never abort conversion
        _warn(f"tags: failed reading embedded cover from {path}: {exc}")
        return None


def _warn(message: str) -> None:
    logger.warning(message)
    warnings.warn(message, UserWarning, stacklevel=3)


def _extract_from_file(f: BinaryIO, path: Path) -> bytes | None:
    head = f.read(12)
    if len(head) < 4:
        return None
    f.seek(0)

    if head[:4] == b"fLaC":
        return _flac_cover(f)
    if head[:3] == b"ID3":
        return _id3_cover(f)
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return _mp4_cover(f)

    # Extension fallback when magic is ambiguous (raw MPEG without ID3).
    suffix = path.suffix.lower()
    if suffix in {".m4a", ".mp4", ".m4b", ".m4v", ".aac"}:
        return _mp4_cover(f)
    if suffix in {".mp3", ".mp2"}:
        return _id3_cover(f)
    if suffix == ".flac":
        return _flac_cover(f)
    return None


# ---------------------------------------------------------------------------
# MP4 / M4A
# ---------------------------------------------------------------------------


def _mp4_cover(f: BinaryIO) -> bytes | None:
    """Walk top-level boxes → moov → udta → meta → ilst → covr → data."""
    file_size = _file_size(f)
    for box_type, payload_start, payload_end in _iter_boxes(f, 0, file_size):
        if box_type == b"moov":
            return _mp4_scan_moov(f, payload_start, payload_end)
    return None


def _mp4_scan_moov(f: BinaryIO, start: int, end: int) -> bytes | None:
    for box_type, p_start, p_end in _iter_boxes(f, start, end):
        if box_type == b"udta":
            found = _mp4_scan_udta(f, p_start, p_end)
            if found is not None:
                return found
    return None


def _mp4_scan_udta(f: BinaryIO, start: int, end: int) -> bytes | None:
    for box_type, p_start, p_end in _iter_boxes(f, start, end):
        if box_type == b"meta":
            # meta is a FullBox: 4-byte version/flags before children.
            if p_end - p_start < 4:
                return None
            return _mp4_scan_meta_children(f, p_start + 4, p_end)
    return None


def _mp4_scan_meta_children(f: BinaryIO, start: int, end: int) -> bytes | None:
    for box_type, p_start, p_end in _iter_boxes(f, start, end):
        if box_type == b"ilst":
            return _mp4_scan_ilst(f, p_start, p_end)
    return None


def _mp4_scan_ilst(f: BinaryIO, start: int, end: int) -> bytes | None:
    for box_type, p_start, p_end in _iter_boxes(f, start, end):
        if box_type == b"covr":
            return _mp4_scan_covr(f, p_start, p_end)
    return None


def _mp4_scan_covr(f: BinaryIO, start: int, end: int) -> bytes | None:
    """Return the first data-box payload under covr."""
    for box_type, p_start, p_end in _iter_boxes(f, start, end):
        if box_type != b"data":
            continue
        # data: version/flags (4) + reserved (4) + payload
        if p_end - p_start < 8:
            continue
        f.seek(p_start + 8)
        payload = f.read(p_end - p_start - 8)
        if payload:
            return payload
    return None


def _iter_boxes(
    f: BinaryIO, start: int, end: int
) -> list[tuple[bytes, int, int]]:
    """Parse ISO BMFF boxes in [start, end). Returns (type, payload_start, payload_end)."""
    boxes: list[tuple[bytes, int, int]] = []
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8]
        header_size = 8

        if size == 0:
            # Extends to end of parent (or file).
            box_end = end
        elif size == 1:
            # 64-bit largesize follows.
            large = f.read(8)
            if len(large) < 8:
                break
            size = struct.unpack(">Q", large)[0]
            header_size = 16
            if size < header_size:
                break
            box_end = pos + size
        else:
            if size < 8:
                break
            box_end = pos + size

        if box_end > end or box_end < pos + header_size:
            break

        payload_start = pos + header_size
        boxes.append((box_type, payload_start, box_end))
        if box_end <= pos:
            break
        pos = box_end
    return boxes


def _file_size(f: BinaryIO) -> int:
    cur = f.tell()
    f.seek(0, 2)
    size = f.tell()
    f.seek(cur)
    return size


# ---------------------------------------------------------------------------
# ID3v2 / MP3
# ---------------------------------------------------------------------------


def _id3_cover(f: BinaryIO) -> bytes | None:
    header = f.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        return None

    major = header[3]
    # minor = header[4]
    flags = header[5]
    tag_size = _syncsafe_size(header[6:10])
    if tag_size < 0:
        return None

    # Extended header / footer not needed for APIC walk; read tag body only.
    tag_body = f.read(tag_size)
    if len(tag_body) < tag_size:
        # Truncated — still try with what we have.
        pass

    unsync = bool(flags & 0x80)
    if unsync and major != 4:
        # Whole-tag unsynchronisation (v2.3 and earlier; rare on modern files).
        tag_body = _deunsync(tag_body)

    # Skip extended header if present (v2.3 bit 0x40 / v2.4 bit 0x40).
    offset = 0
    if flags & 0x40:
        if major == 4:
            if len(tag_body) < 4:
                return None
            ext_size = _syncsafe_size(tag_body[0:4])
            offset = ext_size  # includes the size field itself in v2.4
        elif major == 3:
            if len(tag_body) < 4:
                return None
            ext_size = struct.unpack(">I", tag_body[0:4])[0]
            offset = 4 + ext_size
        else:
            # v2.2 has no extended header flag in the same place; ignore.
            pass

    if major == 2:
        return _id3v22_frames(tag_body[offset:])
    if major in (3, 4):
        return _id3v23_24_frames(tag_body[offset:], major=major, tag_unsync=unsync)
    _warn(f"tags: unsupported ID3 version 2.{major}")
    return None


def _id3v23_24_frames(
    data: bytes, *, major: int, tag_unsync: bool = False
) -> bytes | None:
    """Walk ID3v2.3/v2.4 frames; prefer APIC picture type 3."""
    apics: list[tuple[int, bytes]] = []
    pos = 0
    n = len(data)

    while pos + 10 <= n:
        frame_id = data[pos : pos + 4]
        if frame_id == b"\x00\x00\x00\x00" or not _is_frame_id(frame_id):
            break

        size_bytes = data[pos + 4 : pos + 8]
        format_flags = data[pos + 9]
        if major == 4:
            frame_size = _syncsafe_size(size_bytes)
        else:
            frame_size = struct.unpack(">I", size_bytes)[0]

        body_start = pos + 10
        body_end = body_start + frame_size
        if frame_size < 0 or body_end > n:
            break

        if frame_id == b"APIC":
            raw_body = data[body_start:body_end]
            body: bytes | None = raw_body
            if major == 4:
                body = _id3v24_payload(raw_body, format_flags, tag_unsync=tag_unsync)
            parsed = _parse_apic_body(body) if body is not None else None
            if parsed is not None:
                apics.append(parsed)

        pos = body_end

    return _pick_cover(apics)


def _id3v24_payload(
    body: bytes, format_flags: int, *, tag_unsync: bool
) -> bytes | None:
    """Decode v2.4 format fields in ID3 section 4.1.2 order.

    Unsynchronisation covers every byte after the frame header, including
    grouping identity and the data-length indicator. Undo it before reading
    those fields, while the caller advances using the original stored size.
    """
    # Only grouping, unsynchronisation and data-length fields are supported.
    # In particular, never mistake compressed/encrypted APIC bytes for an image.
    if format_flags & ~0x43:
        return None
    if tag_unsync or format_flags & 0x02:
        body = _deunsync(body)
    pos = int(bool(format_flags & 0x40))
    if pos > len(body):
        return None
    if format_flags & 0x01:
        if pos + 4 > len(body):
            return None
        length_bytes = body[pos : pos + 4]
        if any(value & 0x80 for value in length_bytes):
            return None
        expected_size = _syncsafe_size(length_bytes)
        pos += 4
        if expected_size != len(body) - pos:
            return None
    return body[pos:]


def _id3v22_frames(data: bytes) -> bytes | None:
    """ID3v2.2: 3-char id, 3-byte size, PIC frame."""
    apics: list[tuple[int, bytes]] = []
    pos = 0
    n = len(data)

    while pos + 6 <= n:
        frame_id = data[pos : pos + 3]
        if frame_id == b"\x00\x00\x00" or not all(
            0x20 <= b < 0x7F for b in frame_id
        ):
            break
        frame_size = (
            (data[pos + 3] << 16) | (data[pos + 4] << 8) | data[pos + 5]
        )
        body_start = pos + 6
        body_end = body_start + frame_size
        if frame_size < 0 or body_end > n:
            break

        if frame_id == b"PIC":
            parsed = _parse_pic_body(data[body_start:body_end])
            if parsed is not None:
                apics.append(parsed)

        pos = body_end

    return _pick_cover(apics)


def _parse_apic_body(body: bytes) -> tuple[int, bytes] | None:
    """APIC: encoding, mime\\0, pic_type, desc\\0, image."""
    if len(body) < 4:
        return None
    encoding = body[0]
    pos = 1

    # MIME: always latin-1, NUL-terminated.
    nul = body.find(b"\x00", pos)
    if nul < 0:
        return None
    # mime = body[pos:nul]
    pos = nul + 1
    if pos >= len(body):
        return None

    pic_type = body[pos]
    pos += 1

    desc_end = _find_desc_end(body, pos, encoding)
    if desc_end is None:
        return None
    image = body[desc_end:]
    if not image:
        return None
    return pic_type, image


def _parse_pic_body(body: bytes) -> tuple[int, bytes] | None:
    """ID3v2.2 PIC: encoding, 3-char format, pic_type, desc\\0, image."""
    if len(body) < 6:
        return None
    encoding = body[0]
    # image_format = body[1:4]
    pic_type = body[4]
    desc_end = _find_desc_end(body, 5, encoding)
    if desc_end is None:
        return None
    image = body[desc_end:]
    if not image:
        return None
    return pic_type, image


def _find_desc_end(body: bytes, start: int, encoding: int) -> int | None:
    """Return index just past the description terminator, or None."""
    if encoding in (1, 2):
        # UTF-16 with BOM (1) or UTF-16BE (2): terminated by 0x00 0x00 on
        # even boundary relative to the description start.
        i = start
        n = len(body)
        while i + 1 < n:
            if body[i] == 0 and body[i + 1] == 0:
                return i + 2
            i += 2
        return None
    # Latin-1 (0) or UTF-8 (3): single NUL.
    nul = body.find(b"\x00", start)
    if nul < 0:
        return None
    return nul + 1


def _pick_cover(apics: list[tuple[int, bytes]]) -> bytes | None:
    if not apics:
        return None
    # A malformed type-3 APIC must not hide another frame's usable image.
    # If every candidate is unrecognizable, preserve the raw result so the
    # artwork layer can report corruption rather than silently claiming absence.
    usable = [(ptype, data) for ptype, data in apics if looks_like_image(data)]
    candidates = usable or apics
    for ptype, data in candidates:
        if ptype == _FRONT_COVER:
            return data
    return candidates[0][1]


def looks_like_image(data: bytes) -> bool:
    """Recognize supported image headers, without claiming full image validity."""
    if len(data) < 8:
        return False
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a", b"BM")):
        return True
    return bool(data.startswith(b"RIFF") and data[8:12] == b"WEBP")


def _is_frame_id(frame_id: bytes) -> bool:
    if len(frame_id) != 4:
        return False
    return all(0x30 <= b < 0x5B and (b <= 0x39 or b >= 0x41) for b in frame_id)


def _syncsafe_size(raw: bytes) -> int:
    if len(raw) != 4:
        return -1
    # Reject non-syncsafe high bits so garbage sizes fail early.
    if any(b & 0x80 for b in raw):
        # Still decode with 7-bit mask (v2.3 frames may land here if misrouted).
        pass
    return (
        ((raw[0] & 0x7F) << 21)
        | ((raw[1] & 0x7F) << 14)
        | ((raw[2] & 0x7F) << 7)
        | (raw[3] & 0x7F)
    )


def _deunsync(data: bytes) -> bytes:
    """Strip ID3 unsynchronisation: FF 00 → FF."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        out.append(data[i])
        if data[i] == 0xFF and i + 1 < n and data[i + 1] == 0x00:
            i += 2
        else:
            i += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------


def _flac_cover(f: BinaryIO) -> bytes | None:
    magic = f.read(4)
    if magic != b"fLaC":
        return None

    pictures: list[tuple[int, bytes]] = []
    while True:
        header = f.read(4)
        if len(header) < 4:
            break
        block_type = header[0] & 0x7F
        is_last = bool(header[0] & 0x80)
        length = (header[1] << 16) | (header[2] << 8) | header[3]
        payload = f.read(length)
        if len(payload) < length:
            break

        if block_type == 6:  # PICTURE
            parsed = _parse_flac_picture(payload)
            if parsed is not None:
                pictures.append(parsed)

        if is_last:
            break

    return _pick_cover(pictures)


def _parse_flac_picture(payload: bytes) -> tuple[int, bytes] | None:
    """FLAC PICTURE block → (type, image_bytes)."""
    if len(payload) < 32:
        return None
    pos = 0
    pic_type = struct.unpack(">I", payload[pos : pos + 4])[0]
    pos += 4
    mime_len = struct.unpack(">I", payload[pos : pos + 4])[0]
    pos += 4
    if pos + mime_len > len(payload):
        return None
    pos += mime_len
    if pos + 4 > len(payload):
        return None
    desc_len = struct.unpack(">I", payload[pos : pos + 4])[0]
    pos += 4
    if pos + desc_len > len(payload):
        return None
    pos += desc_len
    # w, h, depth, colors
    if pos + 16 > len(payload):
        return None
    pos += 16
    if pos + 4 > len(payload):
        return None
    data_len = struct.unpack(">I", payload[pos : pos + 4])[0]
    pos += 4
    if pos + data_len > len(payload):
        return None
    data = payload[pos : pos + data_len]
    if not data:
        return None
    return pic_type, data
