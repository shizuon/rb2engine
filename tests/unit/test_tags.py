"""Unit tests for built-in embedded-cover extraction (reader/tags.py).

WHY: mutagen is GPL and must leave the runtime dependency set before public
MIT release. Extraction must stay byte-identical to what mutagen reads so
AlbumArt BLOBs and content hashes do not silently change. Mutagen remains
the test-only oracle for writing fixtures and for expected bytes.
"""

from __future__ import annotations

import base64
import struct
import warnings
from pathlib import Path

import pytest

mutagen = pytest.importorskip("mutagen")
from mutagen.id3 import APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from rb2engine.reader.tags import embedded_cover_bytes

# Silent ~0.1s bases (same scaffolding as test_artwork.py). Not Engine-authored.
_BASE_MP3 = base64.b64decode(
    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYyLjMuMTAwAAAAAAAAAAAAAAD/+0DAAAAAAAAAAAAA"
    "AAAAAAAAAABJbmZvAAAADwAAAAUAAATKAFFRUVFRUVFRUVFRUVFRUVFRUVF9fX19fX19fX19fX19"
    "fX19fX19faioqKioqKioqKioqKioqKioqKio1NTU1NTU1NTU1NTU1NTU1NTU1NT/////////////"
    "/////////////wAAAABMYXZjNjIuMTEAAAAAAAAAAAAAAAAkAwYAAAAAAAAEyqvNp2YAAAAAAP/7"
    "UMQAA8AAAaQAAAAgAAA0gAAABExBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+1LEXYPAAAGkAAAAIAAANIAAAARV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUz"
    "LjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVf/7UsShg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//tS"
    "xKGDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+1LEoYPAAAGkAAAAIAAANIAAAARV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVQ=="
)
_BASE_M4A = base64.b64decode(
    "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAMW1kYXTeAgBMYXZjNjIuMTEu"
    "MTAwAAIwQA4BGCAHARggBwEYIAcBGCAHARggBwAAAxJtb292AAAAbG12aGQAAAAAAAAAAAAAAAAA"
    "AAPoAAAAZAABAAABAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACPXRyYWsAAABcdGtoZAAAAAMAAAAAAAAA"
    "AAAAAAEAAAAAAAAAZAAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAA"
    "AAAAAEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAGQAAAQAAAEAAAAAAbVt"
    "ZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAAKxEAAAVOlXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAA"
    "AAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAFgbWluZgAAABBzbWhkAAAAAAAAAAAAAAAkZGluZgAA"
    "ABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAEkc3RibAAAAGpzdHNkAAAAAAAAAAEAAABabXA0"
    "YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAKxEAAAAAAA2ZXNkcwAAAAADgICAJQABAASAgIAXQBUA"
    "AAAAAPoAAAAKZQWAgIAFEghW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAABQAABAAAAAABAAAB"
    "OgAAABxzdHNjAAAAAAAAAAEAAAABAAAABgAAAAEAAAAsc3RzegAAAAAAAAAAAAAABgAAABUAAAAE"
    "AAAABAAAAAQAAAAEAAAABAAAABRzdGNvAAAAAAAAAAEAAAAsAAAAGnNncGQBAAAAcm9sbAAAAAIA"
    "AAAB//8AAAAcc2JncAAAAAByb2xsAAAAAQAAAAYAAAABAAAAYXVkdGEAAABZbWV0YQAAAAAAAAAh"
    "aGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAsaWxzdAAAACSpdG9vAAAAHGRhdGEAAAAB"
    "AAAAAExhdmY2Mi4zLjEwMA=="
)

# 1×1 PNG (69 bytes)
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4//8/AAX+Av6nNYGE"
    "AAAAAElFTkSuQmCC"
)
# Minimal 1×1 JPEG — magic FF D8 FF; mutagen stores/returns these bytes as-is.
_JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAHwAAAQUBAQEB"
    "AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh"
    "ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZ"
    "WmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APvV2yCo8UUA/9k="
)


def _mutagen_cover_bytes(path: Path) -> bytes | None:
    """Oracle: what mutagen reads back from the same file."""
    audio = mutagen.File(path)
    if audio is None:
        return None
    pictures = getattr(audio, "pictures", None)
    if pictures:
        data = getattr(pictures[0], "data", None)
        if data:
            return bytes(data)
    tags = getattr(audio, "tags", None)
    if tags is None:
        return None
    if hasattr(tags, "__contains__") and "covr" in tags:
        covers = tags["covr"]
        if covers:
            return bytes(covers[0])
    frames: list[object] = []
    try:
        keys = list(tags.keys())
    except (TypeError, AttributeError):
        return None
    for key in keys:
        sk = str(key)
        if sk.startswith(("APIC", "PIC")):
            frames.append(tags[key])
    if not frames:
        return None
    for frame in frames:
        ptype = getattr(frame, "type", None)
        data = getattr(frame, "data", None)
        if data and ptype is not None and int(ptype) == 3:
            return bytes(data)
    data = getattr(frames[0], "data", None)
    return bytes(data) if data else None


def _mp3_with_apic(
    path: Path,
    image: bytes,
    *,
    mime: str = "image/png",
    v2_version: int = 4,
    picture_type: int = 3,
    desc: str = "Cover",
) -> Path:
    path.write_bytes(_BASE_MP3)
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.delall("APIC")
    audio.tags.add(
        APIC(
            encoding=3,
            mime=mime,
            type=picture_type,
            desc=desc,
            data=image,
        )
    )
    if v2_version == 3:
        audio.tags.update_to_v23()
    audio.save(v2_version=v2_version, v1=0)
    return path


def _m4a_with_covr(path: Path, image: bytes) -> Path:
    path.write_bytes(_BASE_M4A)
    audio = MP4(path)
    fmt = (
        MP4Cover.FORMAT_PNG
        if image.startswith(b"\x89PNG")
        else MP4Cover.FORMAT_JPEG
    )
    audio["covr"] = [MP4Cover(image, imageformat=fmt)]
    audio.save()
    return path


def _assert_matches_mutagen(path: Path) -> None:
    ours = embedded_cover_bytes(path)
    oracle = _mutagen_cover_bytes(path)
    assert ours == oracle, (
        f"byte mismatch for {path.name}: "
        f"ours={None if ours is None else len(ours)}B "
        f"mutagen={None if oracle is None else len(oracle)}B"
    )


# ---------------------------------------------------------------------------
# Mutagen-oracle round-trips (formats we actually ship for)
# ---------------------------------------------------------------------------


def test_mp3_id3v24_apic_matches_mutagen(tmp_path: Path) -> None:
    """ID3v2.4 is mutagen's default; frame sizes are syncsafe.

    WHY: misreading v2.4 sizes as plain BE yields wrong offsets and either
    truncated garbage or None — both would corrupt AlbumArt BLOBs.
    """
    track = _mp3_with_apic(tmp_path / "v24.mp3", _PNG_1X1, v2_version=4)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_mp3_id3v23_apic_matches_mutagen(tmp_path: Path) -> None:
    """ID3v2.3 frame sizes are plain big-endian, not syncsafe.

    WHY: many DJ tools still write v2.3; the two size encodings diverge for
    any frame body ≥ 128 bytes, so the wrong decoder is not a theoretical bug.
    """
    track = _mp3_with_apic(tmp_path / "v23.mp3", _PNG_1X1, v2_version=3)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_m4a_covr_png_matches_mutagen(tmp_path: Path) -> None:
    """MP4 `covr` with PNG is common on the measured stick (m4a-heavy library)."""
    track = _m4a_with_covr(tmp_path / "cover.m4a", _PNG_1X1)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_m4a_covr_jpeg_matches_mutagen(tmp_path: Path) -> None:
    """JPEG covr is the other flags value (13); sniffing magic must still win."""
    track = _m4a_with_covr(tmp_path / "cover.jpg.m4a", _JPEG_1X1)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _JPEG_1X1


def test_no_art_returns_none(tmp_path: Path) -> None:
    """Missing embedded art is the common case — None, never an error."""
    path = tmp_path / "plain.mp3"
    path.write_bytes(_BASE_MP3)
    assert embedded_cover_bytes(path) is None
    assert _mutagen_cover_bytes(path) is None


def test_does_not_modify_source_file(tmp_path: Path) -> None:
    """Read-only contract: never open audio for writing."""
    track = _m4a_with_covr(tmp_path / "ro.m4a", _PNG_1X1)
    before = track.read_bytes()
    embedded_cover_bytes(track)
    assert track.read_bytes() == before


# ---------------------------------------------------------------------------
# Direct structural cases (not mutagen-authored)
# ---------------------------------------------------------------------------


def test_truncated_file_returns_none_without_raising(tmp_path: Path) -> None:
    """Malformed/truncated audio must warn and return None, never raise.

    WHY: one bad file among thousands must not abort the conversion run.
    """
    bad = tmp_path / "trunc.mp3"
    bad.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\xff")  # claims huge size, EOF
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        result = embedded_cover_bytes(bad)
    assert result is None


def test_corrupt_m4a_returns_none(tmp_path: Path) -> None:
    """Truncated ftyp/moov must not raise."""
    bad = tmp_path / "trunc.m4a"
    bad.write_bytes(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00")
    assert embedded_cover_bytes(bad) is None


def test_prefer_front_cover_picture_type(tmp_path: Path) -> None:
    """When several APIC frames exist, type 3 (front cover) wins.

    WHY: some rippers embed icon + front cover; wrong pick changes the hash.
    """
    path = tmp_path / "multi.mp3"
    path.write_bytes(_BASE_MP3)
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.delall("APIC")
    # type 0 = other, type 3 = front cover
    other = b"\xff\xd8\xff\xe0" + b"OTHERJPEG" + b"\xff\xd9"
    front = _PNG_1X1
    audio.tags.add(
        APIC(encoding=3, mime="image/jpeg", type=0, desc="other", data=other)
    )
    audio.tags.add(
        APIC(encoding=3, mime="image/png", type=3, desc="front", data=front)
    )
    audio.save(v2_version=4, v1=0)

    assert embedded_cover_bytes(path) == front
    _assert_matches_mutagen(path)


def _syncsafe(n: int) -> bytes:
    return bytes(
        [
            (n >> 21) & 0x7F,
            (n >> 14) & 0x7F,
            (n >> 7) & 0x7F,
            n & 0x7F,
        ]
    )


def _be32(n: int) -> bytes:
    return struct.pack(">I", n)


def _build_id3v2(
    *,
    major: int,
    frames: list[bytes],
) -> bytes:
    """Build a minimal ID3v2 tag (no unsync) + a tiny MPEG frame stub."""
    body = b"".join(frames)
    header = b"ID3" + bytes([major, 0, 0]) + _syncsafe(len(body))
    # Minimal silence-ish audio so the file is not tag-only empty.
    return header + body + b"\xff\xfb\x90\x00" + b"\x00" * 32


def _apic_frame_v23(image: bytes, *, mime: bytes = b"image/png") -> bytes:
    """ID3v2.3 APIC: frame size is plain big-endian."""
    # encoding(1) + mime\0 + pic_type(1) + desc\0 + image
    body = b"\x03" + mime + b"\x00" + b"\x03" + b"\x00" + image
    return b"APIC" + _be32(len(body)) + b"\x00\x00" + body


def _apic_frame_v24(image: bytes, *, mime: bytes = b"image/png") -> bytes:
    """ID3v2.4 APIC: frame size is syncsafe."""
    body = b"\x03" + mime + b"\x00" + b"\x03" + b"\x00" + image
    return b"APIC" + _syncsafe(len(body)) + b"\x00\x00" + body


def test_id3v23_vs_v24_frame_size_encoding(tmp_path: Path) -> None:
    """A payload that makes plain-BE and syncsafe size bytes diverge.

    Frame body length 128 → v2.3 stores ``00 00 00 80``; v2.4 stores
    ``00 00 01 00``. Syncsafe-decoding the v2.3 size yields 0; plain-BE
    decoding the v2.4 size yields 256. Either mistake fails this test.

    WHY: this is the classic silent misread that would ship wrong art or None.
    """
    # APIC body layout: enc(1)+mime(10)+"\0"+type(1)+desc(0)+"\0"+image
    # mime "image/png" = 9 chars + NUL = 10; total overhead = 1+10+1+1 = 13
    # Want total body == 128 so size encoding differs: image = 128 - 13 = 115
    overhead = 1 + len(b"image/png") + 1 + 1 + 1  # enc, mime, nul, type, desc nul
    assert overhead == 13
    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256))[: 128 - overhead - 8]
    image = image + b"\x00" * (128 - overhead - len(image))
    assert len(image) + overhead == 128

    v23 = tmp_path / "size_v23.mp3"
    v23.write_bytes(_build_id3v2(major=3, frames=[_apic_frame_v23(image)]))
    v24 = tmp_path / "size_v24.mp3"
    v24.write_bytes(_build_id3v2(major=4, frames=[_apic_frame_v24(image)]))

    assert embedded_cover_bytes(v23) == image
    assert embedded_cover_bytes(v24) == image

    # Prove the encodings actually differ in the file bytes.
    assert b"\x00\x00\x00\x80" in v23.read_bytes()  # plain BE 128
    assert b"\x00\x00\x01\x00" in v24.read_bytes()  # syncsafe 128


def test_mp4_meta_fullbox_version_flags_skipped(tmp_path: Path) -> None:
    """``meta`` is a FullBox: 4 version/flags bytes before children.

    Hand-build moov/udta/meta/ilst/covr/data. If the parser treats meta as a
    plain box, the first child type becomes the version/flags word and covr
    is never found.

    WHY: this is a classic MP4 metadata bug; mutagen-written files always
    include those 4 bytes, so a naive walk fails on every real m4a.
    """
    # data box: size + 'data' + version/flags(4) + reserved(4) + payload
    payload = _PNG_1X1
    data_body = b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + payload  # flags=0
    data_box = _be32(8 + len(data_body)) + b"data" + data_body

    covr_body = data_box
    covr_box = _be32(8 + len(covr_body)) + b"covr" + covr_body

    ilst_body = covr_box
    ilst_box = _be32(8 + len(ilst_body)) + b"ilst" + ilst_body

    # meta FullBox: version/flags then hdlr (optional) + ilst
    meta_children = b"\x00\x00\x00\x00" + ilst_box  # version=0, flags=0
    meta_box = _be32(8 + len(meta_children)) + b"meta" + meta_children

    udta_body = meta_box
    udta_box = _be32(8 + len(udta_body)) + b"udta" + udta_body

    moov_body = udta_box
    moov_box = _be32(8 + len(moov_body)) + b"moov" + moov_body

    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    # free atom padding so structure is plausible
    free = _be32(16) + b"free" + b"\x00" * 8

    path = tmp_path / "hand_meta.m4a"
    path.write_bytes(ftyp + free + moov_box)

    assert embedded_cover_bytes(path) == payload


def test_unknown_extension_with_id3_magic(tmp_path: Path) -> None:
    """Dispatch by content magic, not only by file extension."""
    track = _mp3_with_apic(tmp_path / "song.bin", _PNG_1X1, v2_version=4)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_empty_file_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "empty.mp3"
    path.write_bytes(b"")
    assert embedded_cover_bytes(path) is None


# ---------------------------------------------------------------------------
# ID3v2.2 PIC (hand-built — mutagen no longer writes v2.2)
# ---------------------------------------------------------------------------


def _id3v22_pic_frame(
    image: bytes,
    *,
    encoding: int = 0,
    fmt: bytes = b"PNG",
    pic_type: int = 3,
    desc: bytes = b"",
) -> bytes:
    """ID3v2.2 PIC body + 6-byte frame header (3-char id, 3-byte size).

    Spec (id3v2.2.0): encoding, 3-char image format (not MIME), pic type,
    description terminated per encoding, then image bytes.
    """
    if encoding in (1, 2):
        # UTF-16 family: double-NUL terminator on even boundary.
        body = bytes([encoding]) + fmt + bytes([pic_type]) + desc + b"\x00\x00" + image
    else:
        body = bytes([encoding]) + fmt + bytes([pic_type]) + desc + b"\x00" + image
    size = len(body)
    size3 = bytes([(size >> 16) & 0xFF, (size >> 8) & 0xFF, size & 0xFF])
    return b"PIC" + size3 + body


def test_id3v22_pic_front_cover(tmp_path: Path) -> None:
    """ID3v2.2 uses 3-char frame ids and PIC with a 3-char format, not MIME.

    Hand-built: mutagen stopped writing v2.2. Expected image is the payload
    we embed, not a round-trip through tags.py.

    WHY: silent miss on v2.2 returns None and the user loses album art with
    no error — common on older ripped libraries.
    """
    image = _PNG_1X1
    frame = _id3v22_pic_frame(image, encoding=0, fmt=b"PNG", pic_type=3)
    path = tmp_path / "v22.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=[frame]))

    assert embedded_cover_bytes(path) == image


def test_id3v22_pic_prefer_front_cover_over_other(tmp_path: Path) -> None:
    """Among multiple PIC frames, picture type 3 (front cover) must win."""
    other = b"\xff\xd8\xff\xe0OTHER\xff\xd9"
    front = _PNG_1X1
    frames = [
        _id3v22_pic_frame(other, pic_type=0, fmt=b"JPG"),
        _id3v22_pic_frame(front, pic_type=3, fmt=b"PNG"),
    ]
    path = tmp_path / "v22_multi.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=frames))
    assert embedded_cover_bytes(path) == front


def test_id3v22_pic_falls_back_to_first_when_no_front(tmp_path: Path) -> None:
    """No type-3 PIC → first parseable image is returned (not None)."""
    only = _JPEG_1X1
    frame = _id3v22_pic_frame(only, pic_type=0, fmt=b"JPG")
    path = tmp_path / "v22_nofront.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=[frame]))
    assert embedded_cover_bytes(path) == only


def test_id3v22_pic_utf16_description(tmp_path: Path) -> None:
    """PIC encoding 1: description ends at double-NUL on even boundary.

    UTF-16BE letter 'A' is 00 41. A single-NUL scan would stop at that 00
    and treat 41 00 00 + image as the image start — garbage bytes, not None.

    Hand-built from id3v2.2.0 §4.15 (PIC).
    """
    image = _PNG_1X1
    # encoding=1, empty-ish desc: BOM + nothing, then double-NUL. Use a
    # non-empty UTF-16BE-looking desc without relying on BOM placement:
    # desc bytes relative to start: FF FE 41 00 (UTF-16LE 'A') then 00 00.
    desc = b"\xff\xfe\x41\x00"
    frame = _id3v22_pic_frame(image, encoding=1, fmt=b"PNG", desc=desc)
    path = tmp_path / "v22_utf16.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=[frame]))
    assert embedded_cover_bytes(path) == image


def test_id3v22_pic_truncated_body_returns_none(tmp_path: Path) -> None:
    """Truncated PIC body must return None, never raise."""
    # Frame claims size 20 but body is only a few bytes past the header.
    frame = b"PIC" + bytes([0, 0, 20]) + b"\x00PNG\x03"  # short
    path = tmp_path / "v22_trunc.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=[frame]))
    assert embedded_cover_bytes(path) is None


# ---------------------------------------------------------------------------
# UTF-16 APIC descriptions (ID3v2.3/2.4)
# ---------------------------------------------------------------------------


def _apic_body(
    image: bytes,
    *,
    encoding: int,
    mime: bytes = b"image/png",
    pic_type: int = 3,
    desc: bytes = b"",
) -> bytes:
    """Raw APIC frame body (no frame header)."""
    if encoding in (1, 2):
        return (
            bytes([encoding])
            + mime
            + b"\x00"
            + bytes([pic_type])
            + desc
            + b"\x00\x00"
            + image
        )
    return (
        bytes([encoding])
        + mime
        + b"\x00"
        + bytes([pic_type])
        + desc
        + b"\x00"
        + image
    )


def _wrap_apic_v24(body: bytes) -> bytes:
    return b"APIC" + _syncsafe(len(body)) + b"\x00\x00" + body


@pytest.mark.parametrize("major", [2, 3, 4])
@pytest.mark.parametrize("bad_first", [False, True])
def test_invalid_front_cover_does_not_hide_usable_picture(
    tmp_path: Path, major: int, bad_first: bool
) -> None:
    """A URL masquerading as the front image must not discard a real JPEG."""
    from rb2engine.reader.artwork import extract_artwork, read_artwork_bytes

    pictures = [(0, _JPEG_1X1), (3, b"https://example.invalid/cover.jpg")]
    if bad_first:
        pictures.reverse()
    frames = []
    for picture_type, data in pictures:
        if major == 2:
            frame = _id3v22_pic_frame(data, pic_type=picture_type, fmt=b"JPG")
        else:
            body = _apic_body(data, encoding=0, pic_type=picture_type)
            size = _syncsafe(len(body)) if major == 4 else _be32(len(body))
            frame = b"APIC" + size + b"\x00\x00" + body
        frames.append(frame)
    original = _build_id3v2(major=major, frames=frames)
    path = tmp_path / "fallback.mp3"
    path.write_bytes(original)

    assert embedded_cover_bytes(path) == _JPEG_1X1
    art = extract_artwork(path)
    assert art is not None
    assert read_artwork_bytes(art) == _JPEG_1X1
    assert path.read_bytes() == original


def test_all_invalid_cover_candidates_still_report_corruption(tmp_path: Path) -> None:
    """Fallback must not turn unrecognizable bytes into a published image."""
    from rb2engine.reader.artwork import extract_artwork

    frames = [
        _wrap_apic_v24(_apic_body(b"not an image", encoding=0, pic_type=0)),
        _wrap_apic_v24(_apic_body(b"https://example.invalid", encoding=0, pic_type=3)),
    ]
    path = tmp_path / "invalid.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=frames))
    with pytest.warns(UserWarning, match="undecodable/corrupt image"):
        assert extract_artwork(path) is None


def test_apic_utf16be_description_not_single_nul(tmp_path: Path) -> None:
    """Encoding 2 (UTF-16BE): desc 'A' is 00 41 — single-NUL would mis-split.

    WHY: wrong terminator shifts the image start into the description and
    yields corrupted cover bytes that still hash — silent AlbumArt wrongness.
    Hand-built from ID3v2.4.0 §4.14; expected image is the payload we embed.
    """
    image = _PNG_1X1
    # UTF-16BE 'A' = 00 41; terminator 00 00. Single-NUL finds first 00.
    desc = b"\x00\x41"
    body = _apic_body(image, encoding=2, desc=desc)
    path = tmp_path / "utf16be.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[_wrap_apic_v24(body)]))
    assert embedded_cover_bytes(path) == image


def test_apic_utf16_with_bom_description(tmp_path: Path) -> None:
    """Encoding 1 (UTF-16 with BOM): double-NUL ends the description."""
    image = _PNG_1X1
    desc = b"\xff\xfe\x43\x00"  # UTF-16LE 'C'
    body = _apic_body(image, encoding=1, desc=desc)
    path = tmp_path / "utf16bom.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[_wrap_apic_v24(body)]))
    assert embedded_cover_bytes(path) == image


def test_apic_utf16_missing_terminator_returns_none(tmp_path: Path) -> None:
    """UTF-16 description without a double-NUL must not invent an image."""
    # encoding + mime\0 + type + incomplete UTF-16 desc, no terminator, no image
    body = b"\x01" + b"image/png\x00" + b"\x03" + b"\xff\xfe\x41\x00"
    path = tmp_path / "utf16_noterm.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[_wrap_apic_v24(body)]))
    assert embedded_cover_bytes(path) is None


def test_apic_latin1_missing_mime_nul_returns_none(tmp_path: Path) -> None:
    """MIME without a NUL terminator → None (not a partial parse).

    Body has zero 0x00 bytes after the encoding byte so find() cannot
    invent a mime boundary from PNG IHDR fields.
    """
    body = b"\x00" + b"image/pngNOMORENULS"
    path = tmp_path / "no_mime_nul.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[_wrap_apic_v24(body)]))
    assert embedded_cover_bytes(path) is None


def test_apic_empty_image_returns_none(tmp_path: Path) -> None:
    """APIC with valid headers but zero image bytes is absent art, not b''."""
    body = _apic_body(b"", encoding=0)
    path = tmp_path / "empty_img.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[_wrap_apic_v24(body)]))
    assert embedded_cover_bytes(path) is None


def test_apic_body_too_short_returns_none(tmp_path: Path) -> None:
    """APIC body shorter than the minimum header fields → None."""
    frame = b"APIC" + _syncsafe(2) + b"\x00\x00" + b"\x00\x00"
    path = tmp_path / "short_apic.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[frame]))
    assert embedded_cover_bytes(path) is None


# ---------------------------------------------------------------------------
# Unsynchronisation (hand-built — mutagen rarely emits whole-tag unsync)
# ---------------------------------------------------------------------------


def _unsync(data: bytes) -> bytes:
    """ID3 unsynchronisation: insert 0x00 after every 0xFF."""
    out = bytearray()
    for b in data:
        out.append(b)
        if b == 0xFF:
            out.append(0x00)
    return bytes(out)


def test_id3_unsynchronisation_deescapes_jpeg(tmp_path: Path) -> None:
    """The v2.4 tag unsync flag applies to each stored frame body.

    JPEG magic is FF D8 FF … so without de-escaping the image is corrupted
    (extra 0x00 bytes) or unreadable. Hand-built: we embed known JPEG bytes,
    apply the unsync transform ourselves, set the header flag.

    WHY: returning corrupted JPEG bytes is worse than None — AlbumArt stores
    garbage that still has a hash.
    """
    image = _JPEG_1X1
    assert b"\xff" in image  # precondition: unsync will touch this payload
    apic_body = _apic_body(image, encoding=0, mime=b"image/jpeg")
    stored_body = _unsync(apic_body)
    # ID3v2.4 section 4: frame size is AFTER unsynchronisation, unlike v2.3.
    frame = b"APIC" + _syncsafe(len(stored_body)) + b"\x00\x02" + stored_body
    header = b"ID3" + bytes([4, 0, 0x80]) + _syncsafe(len(frame))
    path = tmp_path / "unsync.mp3"
    path.write_bytes(header + frame + b"\xff\xfb\x90\x00" + b"\x00" * 32)

    got = embedded_cover_bytes(path)
    assert got == image
    # Explicitly reject the corrupted form (FF 00 still present in image).
    assert got is not None
    assert b"\xff\x00" not in got or image.count(b"\xff\x00") == got.count(
        b"\xff\x00"
    )


@pytest.mark.parametrize("tag_unsync", [False, True])
@pytest.mark.parametrize("group_id", [None, 7, 255])
def test_id3v24_group_unsync_and_length_fields(
    tmp_path: Path, tag_unsync: bool, group_id: int | None
) -> None:
    """Section 4.1.2 unsync covers the prefix too; FF group IDs insert a byte.

    The expected image is hand-authored before any transforms. The leading
    FF 00 pair belongs to group identity, so decoding only APIC data would
    read the wrong four-byte length field and discard or corrupt the cover.
    """
    image = b"\xff\xd8\xff\xe0\x00\x10JFIF\xff\x00\xff\xd9"
    payload = _apic_body(image, encoding=0, mime=b"image/jpeg")
    prefix = b"" if group_id is None else bytes([group_id])
    stored = _unsync(prefix + _syncsafe(len(payload)) + payload)
    flags = 0x03 | (0x40 if group_id is not None else 0)
    frame = b"APIC" + _syncsafe(len(stored)) + bytes([0, flags]) + stored
    header = b"ID3" + bytes([4, 0, 0x80 if tag_unsync else 0]) + _syncsafe(len(frame))
    path = tmp_path / "grouped.mp3"
    path.write_bytes(header + frame)

    assert embedded_cover_bytes(path) == image


def test_id3v24_frame_sizes_still_locate_later_cover_after_unsync(tmp_path: Path) -> None:
    """Do not shrink the tag before following on-disk frame sizes."""
    text = _unsync(b"\x00prefix\xff\xe0suffix")
    first = b"TIT2" + _syncsafe(len(text)) + b"\x00\x02" + text
    payload = _apic_body(_PNG_1X1, encoding=0)
    stored = _unsync(payload)
    second = b"APIC" + _syncsafe(len(stored)) + b"\x00\x02" + stored
    frames = first + second
    path = tmp_path / "later_cover.mp3"
    path.write_bytes(b"ID3" + bytes([4, 0, 0x80]) + _syncsafe(len(frames)) + frames)

    assert embedded_cover_bytes(path) == _PNG_1X1


@pytest.mark.parametrize("length_delta", [-1, 1])
def test_id3v24_data_length_mismatch_is_rejected(tmp_path: Path, length_delta: int) -> None:
    """A declared size must never truncate the returned image to fit itself."""
    payload = _apic_body(_PNG_1X1, encoding=0)
    body = _syncsafe(len(payload) + length_delta) + payload
    frame = b"APIC" + _syncsafe(len(body)) + b"\x00\x01" + body
    path = tmp_path / "bad_length.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[frame]))

    assert embedded_cover_bytes(path) is None


@pytest.mark.parametrize("flags", [0x04, 0x08, 0x80])
def test_id3v24_unsupported_frame_format_keeps_later_cover(
    tmp_path: Path, flags: int
) -> None:
    """Encrypted/compressed/unknown payloads must not be parsed as APIC."""
    body = _apic_body(_JPEG_1X1, encoding=0)
    first = b"APIC" + _syncsafe(len(body)) + bytes([0, flags]) + body
    second = _wrap_apic_v24(_apic_body(_PNG_1X1, encoding=0))
    path = tmp_path / "unsupported.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[first, second]))

    assert embedded_cover_bytes(path) == _PNG_1X1


def test_id3_unsync_never_returns_escaped_ff00_corruption(tmp_path: Path) -> None:
    """If unsync is mishandled, image would contain FF 00 for every FF.

    Assert the returned bytes equal the original JPEG we embedded — the
    oracle is the pre-unsync payload, not tags.py output.
    """
    # Tiny synthetic JPEG-like payload with several 0xFF bytes.
    image = b"\xff\xd8\xff\xe0\x00\x10JFIF\xff\xd9"
    apic_body = _apic_body(image, encoding=0, mime=b"image/jpeg")
    frame = b"APIC" + _syncsafe(len(apic_body)) + b"\x00\x00" + apic_body
    unsynced_body = _unsync(frame)
    # Sanity: unsynced form differs from original frame.
    assert unsynced_body != frame
    header = b"ID3" + bytes([3, 0, 0x80]) + _syncsafe(len(unsynced_body))
    path = tmp_path / "unsync23.mp3"
    path.write_bytes(header + unsynced_body + b"\xff\xfb\x90\x00" + b"\x00" * 16)
    assert embedded_cover_bytes(path) == image


# ---------------------------------------------------------------------------
# Extended header, unsupported version, truncated ID3
# ---------------------------------------------------------------------------


def test_id3v24_extended_header_skipped(tmp_path: Path) -> None:
    """v2.4 extended header: size is syncsafe and includes the size field.

    Hand-built: flags bit 0x40 set, 10-byte extended header (size=10), then
    APIC. Parser must start frames after the extended header.
    """
    image = _PNG_1X1
    apic = _wrap_apic_v24(_apic_body(image, encoding=0))
    # v2.4 ext header: 4-byte syncsafe size (includes itself) + 1 flag byte
    # + 1 flag data length + optional data. Minimal: size=6, flags=0, len=0.
    ext = _syncsafe(6) + b"\x00\x00"
    assert len(ext) == 6
    body = ext + apic
    header = b"ID3" + bytes([4, 0, 0x40]) + _syncsafe(len(body))
    path = tmp_path / "ext24.mp3"
    path.write_bytes(header + body + b"\xff\xfb\x90\x00" + b"\x00" * 16)
    assert embedded_cover_bytes(path) == image


def test_id3v23_extended_header_skipped(tmp_path: Path) -> None:
    """v2.3 extended header: 4-byte plain size excludes itself; skip 4+size."""
    image = _PNG_1X1
    body_apic = _apic_body(image, encoding=0)
    apic = b"APIC" + _be32(len(body_apic)) + b"\x00\x00" + body_apic
    # v2.3: size of extended header excluding the 4 size bytes; pad 6 bytes.
    ext = _be32(6) + b"\x00" * 6
    body = ext + apic
    header = b"ID3" + bytes([3, 0, 0x40]) + _syncsafe(len(body))
    path = tmp_path / "ext23.mp3"
    path.write_bytes(header + body + b"\xff\xfb\x90\x00" + b"\x00" * 16)
    assert embedded_cover_bytes(path) == image


def test_unsupported_id3_version_returns_none(tmp_path: Path) -> None:
    """ID3v2.5 (and other unknown majors) must warn and return None."""
    body = b"\x00" * 16
    header = b"ID3" + bytes([5, 0, 0]) + _syncsafe(len(body))
    path = tmp_path / "v25.mp3"
    path.write_bytes(header + body)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert embedded_cover_bytes(path) is None
    assert any("unsupported ID3" in str(w.message) for w in caught)


def test_id3_header_too_short_returns_none(tmp_path: Path) -> None:
    """Fewer than 10 header bytes after ID3 magic → None."""
    path = tmp_path / "short_hdr.mp3"
    path.write_bytes(b"ID3\x04\x00")  # magic matches dispatch, header incomplete
    assert embedded_cover_bytes(path) is None


def test_id3_non_frame_id_stops_walk(tmp_path: Path) -> None:
    """Padding (NUL frame id) ends the frame walk without raising."""
    image = _PNG_1X1
    apic = _wrap_apic_v24(_apic_body(image, encoding=0))
    # Valid frame then padding NULs (normal ID3 padding).
    body = apic + b"\x00" * 32
    path = tmp_path / "pad.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[body]))
    # _build_id3v2 joins frames — pass combined body as single chunk:
    header = b"ID3" + bytes([4, 0, 0]) + _syncsafe(len(body))
    path.write_bytes(header + body + b"\xff\xfb\x90\x00" + b"\x00" * 8)
    assert embedded_cover_bytes(path) == image


# ---------------------------------------------------------------------------
# FLAC PICTURE (hand-built from flac format block layout)
# ---------------------------------------------------------------------------


def _flac_picture_block(
    image: bytes,
    *,
    pic_type: int = 3,
    mime: bytes = b"image/png",
    desc: bytes = b"",
    is_last: bool = True,
) -> bytes:
    """One FLAC METADATA_BLOCK of type PICTURE (6).

    Layout (flac spec): type(4) mime_len(4) mime desc_len(4) desc
    width/height/depth/colors (4×4) data_len(4) data.
    """
    payload = b"".join(
        [
            _be32(pic_type),
            _be32(len(mime)),
            mime,
            _be32(len(desc)),
            desc,
            _be32(1),  # width
            _be32(1),  # height
            _be32(8),  # depth
            _be32(0),  # colors
            _be32(len(image)),
            image,
        ]
    )
    header0 = (0x80 if is_last else 0x00) | 6
    length = len(payload)
    hdr = bytes(
        [header0, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF]
    )
    return hdr + payload


def _flac_streaminfo_block(*, is_last: bool = False) -> bytes:
    """Minimal 34-byte STREAMINFO (type 0); contents ignored by tags.py."""
    payload = b"\x00" * 34
    header0 = (0x80 if is_last else 0x00) | 0
    length = 34
    hdr = bytes(
        [header0, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF]
    )
    return hdr + payload


def test_flac_picture_after_streaminfo(tmp_path: Path) -> None:
    """FLAC: non-picture STREAMINFO before PICTURE must not hide the art.

    Hand-built from the FLAC format spec (METADATA_BLOCK_PICTURE). Expected
    bytes are the image we place in the data field — not tags.py output.
    """
    image = _PNG_1X1
    data = (
        b"fLaC"
        + _flac_streaminfo_block(is_last=False)
        + _flac_picture_block(image, is_last=True)
    )
    path = tmp_path / "cover.flac"
    path.write_bytes(data)
    assert embedded_cover_bytes(path) == image


def test_flac_prefer_front_cover_among_pictures(tmp_path: Path) -> None:
    """Multiple PICTURE blocks: type 3 wins over type 0."""
    other = b"\xff\xd8\xff\xe0OTHER\xff\xd9"
    front = _PNG_1X1
    data = (
        b"fLaC"
        + _flac_streaminfo_block(is_last=False)
        + _flac_picture_block(other, pic_type=0, mime=b"image/jpeg", is_last=False)
        + _flac_picture_block(front, pic_type=3, is_last=True)
    )
    path = tmp_path / "multi.flac"
    path.write_bytes(data)
    assert embedded_cover_bytes(path) == front


def test_flac_no_picture_returns_none(tmp_path: Path) -> None:
    """STREAMINFO-only FLAC has no embedded art."""
    path = tmp_path / "plain.flac"
    path.write_bytes(b"fLaC" + _flac_streaminfo_block(is_last=True))
    assert embedded_cover_bytes(path) is None


def test_flac_truncated_picture_payload_returns_none(tmp_path: Path) -> None:
    """PICTURE block shorter than claimed length → None, no raise."""
    # Type 6, is_last, length claims 100 but only 4 bytes follow.
    hdr = bytes([0x80 | 6, 0, 0, 100]) + b"\x00\x00\x00\x03"
    path = tmp_path / "trunc.flac"
    path.write_bytes(b"fLaC" + hdr)
    assert embedded_cover_bytes(path) is None


def test_flac_picture_payload_under_32_bytes_returns_none(tmp_path: Path) -> None:
    """PICTURE payload that is fully read but <32 bytes cannot be valid."""
    payload = b"\x00" * 16
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "tiny_pic.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_corrupt_picture_fields_return_none(tmp_path: Path) -> None:
    """PICTURE with mime_len past end of payload → None.

    Payload is padded to ≥32 bytes so we pass the coarse length gate and
    exercise the mime_len overrun check specifically.
    """
    # pic_type + mime_len huge + pad so len>=32 but mime still overruns
    payload = _be32(3) + _be32(0x00FFFFFF) + b"\x00" * 32
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "badpic.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_via_extension_fallback(tmp_path: Path) -> None:
    """When magic is wrong but suffix is .flac, still attempt FLAC walk.

    File content is not fLaC — parser returns None after the flac path.
    Covers extension-dispatch branch without false-positive art.
    """
    path = tmp_path / "misnamed.flac"
    path.write_bytes(b"notf" + b"\x00" * 20)
    assert embedded_cover_bytes(path) is None


def test_flac_magic_dispatch(tmp_path: Path) -> None:
    """fLaC magic dispatches even with a non-.flac extension."""
    image = _PNG_1X1
    data = (
        b"fLaC"
        + _flac_streaminfo_block(is_last=False)
        + _flac_picture_block(image, is_last=True)
    )
    path = tmp_path / "cover.bin"
    path.write_bytes(data)
    assert embedded_cover_bytes(path) == image


# ---------------------------------------------------------------------------
# MP4 edge cases (hand-built boxes)
# ---------------------------------------------------------------------------


def _mp4_data_box(payload: bytes) -> bytes:
    data_body = b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + payload
    return _be32(8 + len(data_body)) + b"data" + data_body


def _mp4_box(type_: bytes, body: bytes) -> bytes:
    return _be32(8 + len(body)) + type_ + body


def _mp4_with_covr_payload(payload: bytes, *, extra_data: list[bytes] | None = None) -> bytes:
    """Minimal ftyp + moov/udta/meta/ilst/covr tree carrying payload."""
    data_boxes = _mp4_data_box(payload)
    if extra_data:
        data_boxes = data_boxes + b"".join(_mp4_data_box(p) for p in extra_data)
    covr = _mp4_box(b"covr", data_boxes)
    ilst = _mp4_box(b"ilst", covr)
    meta_children = b"\x00\x00\x00\x00" + ilst  # FullBox version/flags
    meta = _mp4_box(b"meta", meta_children)
    udta = _mp4_box(b"udta", meta)
    moov = _mp4_box(b"moov", udta)
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    return ftyp + moov


def test_mp4_size_zero_means_extends_to_eof(tmp_path: Path) -> None:
    """Box size 0 means 'extends to end of parent' (ISO BMFF).

    Hand-built: moov size field is 0; payload runs to EOF. If ignored, the
    walker never enters moov and art is lost.
    """
    payload = _PNG_1X1
    data_boxes = _mp4_data_box(payload)
    covr = _mp4_box(b"covr", data_boxes)
    ilst = _mp4_box(b"ilst", covr)
    meta_children = b"\x00\x00\x00\x00" + ilst
    meta = _mp4_box(b"meta", meta_children)
    udta = _mp4_box(b"udta", meta)
    # size == 0 for moov: header 8 bytes then body to EOF
    moov = _be32(0) + b"moov" + udta
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "size0.m4a"
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) == payload


def test_mp4_64bit_largesize_box(tmp_path: Path) -> None:
    """Box size == 1 → 64-bit largesize follows the type field.

    Hand-built free box with largesize, then a normal moov with covr.
    Walker must consume the 16-byte header and continue.
    """
    payload = _PNG_1X1
    # free box with size=1 and largesize = 16 + 8 (header 16, 8 pad bytes)
    free = (
        _be32(1)
        + b"free"
        + struct.pack(">Q", 24)
        + b"\x00" * 8
    )
    _rest = _mp4_with_covr_payload(payload)
    # rest already has ftyp+moov; prepend free after rebuilding:
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    data_boxes = _mp4_data_box(payload)
    covr = _mp4_box(b"covr", data_boxes)
    ilst = _mp4_box(b"ilst", covr)
    meta = _mp4_box(b"meta", b"\x00\x00\x00\x00" + ilst)
    udta = _mp4_box(b"udta", meta)
    moov = _mp4_box(b"moov", udta)
    path = tmp_path / "large.m4a"
    path.write_bytes(ftyp + free + moov)
    assert embedded_cover_bytes(path) == payload


def test_mp4_covr_multiple_data_boxes_returns_first(tmp_path: Path) -> None:
    """covr may hold several data boxes; first non-empty payload wins.

    Spec/common practice: multiple covers under covr. Our reader documents
    'first data-box payload'. Expected = first image we wrote.
    """
    first = _PNG_1X1
    second = _JPEG_1X1
    path = tmp_path / "multi_data.m4a"
    path.write_bytes(_mp4_with_covr_payload(first, extra_data=[second]))
    assert embedded_cover_bytes(path) == first


def test_mp4_covr_skips_short_data_box(tmp_path: Path) -> None:
    """data box shorter than version/flags+reserved (8 bytes) is skipped.

    A non-data sibling first ensures the ``box_type != data`` continue path
    also runs, then a too-short data box, then a valid one.
    """
    payload = _PNG_1X1
    noise = _mp4_box(b"mean", b"skip-me")
    short = _be32(12) + b"data" + b"\x00\x00\x00\x00"  # only 4 body bytes (<8)
    good = _mp4_data_box(payload)
    covr = _mp4_box(b"covr", noise + short + good)
    ilst = _mp4_box(b"ilst", covr)
    meta = _mp4_box(b"meta", b"\x00\x00\x00\x00" + ilst)
    udta = _mp4_box(b"udta", meta)
    moov = _mp4_box(b"moov", udta)
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "short_data.m4a"
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) == payload


def test_mp4_meta_too_short_for_fullbox_returns_none(tmp_path: Path) -> None:
    """meta FullBox with fewer than 4 version/flags bytes → None."""
    meta = _be32(10) + b"meta" + b"\x00\x00"  # only 2 bytes body
    udta = _mp4_box(b"udta", meta)
    moov = _mp4_box(b"moov", udta)
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "short_meta.m4a"
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) is None


def test_mp4_no_moov_returns_none(tmp_path: Path) -> None:
    """ftyp without moov → no art."""
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "nomoov.m4a"
    path.write_bytes(ftyp + _mp4_box(b"free", b"\x00" * 8))
    assert embedded_cover_bytes(path) is None


def test_mp4_moov_without_udta_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "noudta.m4a"
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    moov = _mp4_box(b"moov", _mp4_box(b"mvhd", b"\x00" * 16))
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) is None


def test_mp4_ilst_without_covr_returns_none(tmp_path: Path) -> None:
    """ilst present but no covr atom → None."""
    too = _mp4_box(b"\xa9too", _mp4_data_box(b"title"))  # ©too
    ilst = _mp4_box(b"ilst", too)
    meta = _mp4_box(b"meta", b"\x00\x00\x00\x00" + ilst)
    udta = _mp4_box(b"udta", meta)
    moov = _mp4_box(b"moov", udta)
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "nocovr.m4a"
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) is None


def test_mp4_extension_fallback_without_ftyp_magic(tmp_path: Path) -> None:
    """Ambiguous magic + .m4a suffix still runs the MP4 walker."""
    # Content has no ftyp at offset 4; extension triggers _mp4_cover.
    path = tmp_path / "odd.m4a"
    path.write_bytes(b"\x00" * 12)
    assert embedded_cover_bytes(path) is None


def test_mp4_invalid_box_size_stops_walker(tmp_path: Path) -> None:
    """size < 8 (and not 0/1) must stop the box walk without raising."""
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    bad = _be32(4) + b"moov"  # illegal size 4
    path = tmp_path / "badsize.m4a"
    path.write_bytes(ftyp + bad)
    assert embedded_cover_bytes(path) is None


def test_mp4_truncated_largesize_header(tmp_path: Path) -> None:
    """size==1 but EOF before 8-byte largesize → None."""
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    # size=1, type=moov, then only 2 of 8 largesize bytes
    bad = _be32(1) + b"moov" + b"\x00\x00"
    path = tmp_path / "trunc_large.m4a"
    path.write_bytes(ftyp + bad)
    assert embedded_cover_bytes(path) is None


# ---------------------------------------------------------------------------
# Dispatch, errors, garbage at every layer
# ---------------------------------------------------------------------------


def test_missing_file_returns_none_with_warning(tmp_path: Path) -> None:
    """OSError on open (missing path) → None + warning, never raise."""
    path = tmp_path / "nope.mp3"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert embedded_cover_bytes(path) is None
    assert any("cannot open" in str(w.message) for w in caught)


def test_mp3_extension_fallback_without_id3(tmp_path: Path) -> None:
    """Raw MPEG frame (no ID3) + .mp3 suffix: attempt ID3 path → None."""
    path = tmp_path / "raw.mp3"
    # MPEG frame sync without ID3 header
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 64)
    assert embedded_cover_bytes(path) is None


def test_unknown_type_returns_none(tmp_path: Path) -> None:
    """No recognized magic and unknown extension → None."""
    path = tmp_path / "song.xyz"
    path.write_bytes(b"RIFF" + b"\x00" * 20)
    assert embedded_cover_bytes(path) is None


def test_tiny_file_under_4_bytes(tmp_path: Path) -> None:
    path = tmp_path / "x.mp3"
    path.write_bytes(b"ID")
    assert embedded_cover_bytes(path) is None


def test_id3v22_pic_body_too_short(tmp_path: Path) -> None:
    """PIC body < 6 bytes cannot hold format+type → None."""
    # size=3, body too short for format
    frame = b"PIC" + bytes([0, 0, 3]) + b"\x00PN"
    path = tmp_path / "short_pic.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=[frame]))
    assert embedded_cover_bytes(path) is None


def test_id3v22_pic_empty_image_after_desc(tmp_path: Path) -> None:
    """Valid PIC headers with zero image bytes → None."""
    body = b"\x00" + b"PNG" + b"\x03" + b"\x00"  # enc, fmt, type, empty desc, no img
    size3 = bytes([0, 0, len(body)])
    frame = b"PIC" + size3 + body
    path = tmp_path / "pic_empty.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=[frame]))
    assert embedded_cover_bytes(path) is None


def test_id3_frame_size_past_tag_end_stops(tmp_path: Path) -> None:
    """Frame size that overruns the tag body stops the walk → None if no prior APIC."""
    # Claim a huge frame size
    frame = b"APIC" + _syncsafe(0x0FFFFF) + b"\x00\x00" + b"\x00" * 8
    body = frame  # tag body shorter than claimed frame
    header = b"ID3" + bytes([4, 0, 0]) + _syncsafe(len(body))
    path = tmp_path / "overrun.mp3"
    path.write_bytes(header + body)
    assert embedded_cover_bytes(path) is None


def test_apic_mime_only_no_picture_type(tmp_path: Path) -> None:
    """Body ends right after mime NUL — no room for pic_type → None."""
    body = b"\x00" + b"image/png\x00"
    path = tmp_path / "no_ptype.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[_wrap_apic_v24(body)]))
    assert embedded_cover_bytes(path) is None


def test_latin1_desc_missing_nul_returns_none(tmp_path: Path) -> None:
    """Latin-1 description without NUL and no image boundary → None."""
    # enc + mime\0 + type + desc without NUL or image
    body = b"\x00" + b"image/png\x00" + b"\x03" + b"Cover"
    path = tmp_path / "nodesc.mp3"
    path.write_bytes(_build_id3v2(major=4, frames=[_wrap_apic_v24(body)]))
    assert embedded_cover_bytes(path) is None


def test_multiple_apic_front_cover_not_first(tmp_path: Path) -> None:
    """Type-3 APIC later in the tag still wins (mutagen-authored).

    WHY: order in the file is not priority; picture type is.
    """
    path = tmp_path / "order.mp3"
    path.write_bytes(_BASE_MP3)
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.delall("APIC")
    icon = b"\xff\xd8\xff\xe0ICON\xff\xd9"
    front = _PNG_1X1
    # Front first, then other — and the reverse order file
    audio.tags.add(
        APIC(encoding=3, mime="image/jpeg", type=1, desc="icon", data=icon)
    )
    audio.tags.add(
        APIC(encoding=3, mime="image/png", type=3, desc="front", data=front)
    )
    audio.save(v2_version=4, v1=0)
    assert embedded_cover_bytes(path) == front
    _assert_matches_mutagen(path)


def test_flac_picture_empty_data_skipped(tmp_path: Path) -> None:
    """PICTURE with data_len=0 is not a cover."""
    payload = b"".join(
        [
            _be32(3),
            _be32(9),
            b"image/png",
            _be32(0),
            b"",
            _be32(0),
            _be32(0),
            _be32(0),
            _be32(0),
            _be32(0),  # data_len = 0
        ]
    )
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "empty_data.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_picture_truncated_after_mime(tmp_path: Path) -> None:
    """Stops mid-structure after mime (no room for desc_len) → None.

    type(4)+mime_len(4)+mime(24) = 32 bytes; after mime ``pos==32`` so the
    desc_len field cannot be read. Coarse ``len < 32`` gate does not mask it.
    """
    payload = _be32(3) + _be32(24) + b"x" * 24
    assert len(payload) == 32
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "mid.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_picture_desc_len_overruns(tmp_path: Path) -> None:
    """desc_len past end of payload → None."""
    payload = (
        _be32(3)
        + _be32(3)
        + b"png"
        + _be32(100)  # desc_len too large
        + b"short" + b"\x00" * 20
    )
    assert len(payload) >= 32
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "desc_ovr.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_picture_truncated_before_dimensions(tmp_path: Path) -> None:
    """Missing width/height/depth/colors (16 bytes) → None.

    After type+mime_len0+desc_len8+desc: pos=20. Only 12 trailing bytes
    (len=32) so pos+16 overruns — dims check fails.
    """
    payload = _be32(3) + _be32(0) + _be32(8) + b"descdesc" + b"\x00" * 12
    assert len(payload) == 32
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "nodims.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_picture_data_len_overruns(tmp_path: Path) -> None:
    """data_len claims more bytes than remain → None."""
    payload = b"".join(
        [
            _be32(3),
            _be32(3),
            b"png",
            _be32(0),
            _be32(1),
            _be32(1),
            _be32(8),
            _be32(0),
            _be32(50),  # data_len
            b"nope",  # only 4 bytes
        ]
    )
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "data_ovr.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_picture_missing_data_len_field(tmp_path: Path) -> None:
    """Dims present but data_len field absent → None.

    After desc+dims ``pos`` equals ``len(payload)`` so the data_len int
    cannot be read. len≥32 so the coarse gate does not mask this branch.
    """
    payload = b"".join(
        [
            _be32(3),
            _be32(0),  # mime_len
            _be32(8),
            b"descdesc",
            _be32(1),
            _be32(1),
            _be32(8),
            _be32(0),
            # no data_len — pos after dims == len == 36
        ]
    )
    assert len(payload) == 36
    length = len(payload)
    hdr = bytes([0x80 | 6, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    path = tmp_path / "nodlen.flac"
    path.write_bytes(b"fLaC" + hdr + payload)
    assert embedded_cover_bytes(path) is None


def test_flac_eof_mid_block_header(tmp_path: Path) -> None:
    """EOF after fLaC magic before a full 4-byte block header → None."""
    path = tmp_path / "short.flac"
    path.write_bytes(b"fLaC\x00\x00")
    assert embedded_cover_bytes(path) is None


def test_id3v22_utf16_desc_without_terminator(tmp_path: Path) -> None:
    """PIC encoding 1 with no double-NUL → None (not a half-parsed image)."""
    body = b"\x01" + b"PNG" + b"\x03" + b"\xff\xfe\x41\x00"  # no 00 00, no image
    size3 = bytes([0, 0, len(body)])
    frame = b"PIC" + size3 + body
    path = tmp_path / "pic_utf16_noterm.mp3"
    path.write_bytes(_build_id3v2(major=2, frames=[frame]))
    assert embedded_cover_bytes(path) is None


def test_id3v22_zero_frame_id_stops(tmp_path: Path) -> None:
    """NUL frame id padding ends the v2.2 walk; prior PIC still returned."""
    image = _PNG_1X1
    pic = _id3v22_pic_frame(image)
    body = pic + b"\x00" * 16
    header = b"ID3" + bytes([2, 0, 0]) + _syncsafe(len(body))
    path = tmp_path / "v22_pad.mp3"
    path.write_bytes(header + body + b"\xff\xfb\x90\x00" + b"\x00" * 8)
    assert embedded_cover_bytes(path) == image


def test_id3v24_extended_header_truncated_body(tmp_path: Path) -> None:
    """Ext-header flag set but tag body shorter than 4 bytes → None."""
    header = b"ID3" + bytes([4, 0, 0x40]) + _syncsafe(2)
    path = tmp_path / "ext_trunc24.mp3"
    path.write_bytes(header + b"\x00\x00")
    assert embedded_cover_bytes(path) is None


def test_id3v23_extended_header_truncated_body(tmp_path: Path) -> None:
    """v2.3 ext-header flag with tiny body → None."""
    header = b"ID3" + bytes([3, 0, 0x40]) + _syncsafe(2)
    path = tmp_path / "ext_trunc23.mp3"
    path.write_bytes(header + b"\x00\x00")
    assert embedded_cover_bytes(path) is None


def test_id3v22_with_extended_header_flag_ignored(tmp_path: Path) -> None:
    """v2.2 has no extended-header flag meaning; flag bit is ignored.

    Hand-built: bit 0x40 set on v2.2, frames start immediately (no skip).
    """
    image = _PNG_1X1
    frame = _id3v22_pic_frame(image)
    header = b"ID3" + bytes([2, 0, 0x40]) + _syncsafe(len(frame))
    path = tmp_path / "v22_extflag.mp3"
    path.write_bytes(header + frame + b"\xff\xfb\x90\x00" + b"\x00" * 8)
    assert embedded_cover_bytes(path) == image


def test_mp4_udta_without_meta_returns_none(tmp_path: Path) -> None:
    """udta present but no meta child → None."""
    udta = _mp4_box(b"udta", _mp4_box(b"name", b"x" * 8))
    moov = _mp4_box(b"moov", udta)
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "nometa.m4a"
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) is None


def test_mp4_meta_without_ilst_returns_none(tmp_path: Path) -> None:
    """meta FullBox with hdlr only (no ilst) → None."""
    hdlr = _mp4_box(b"hdlr", b"\x00" * 24)
    meta = _mp4_box(b"meta", b"\x00\x00\x00\x00" + hdlr)
    udta = _mp4_box(b"udta", meta)
    moov = _mp4_box(b"moov", udta)
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "noilst.m4a"
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) is None


def test_mp4_covr_empty_data_payload_returns_none(tmp_path: Path) -> None:
    """data box with version/flags+reserved only (zero image) → None."""
    empty_data = _be32(16) + b"data" + b"\x00" * 8  # 8 header + 8 fullbox fields
    covr = _mp4_box(b"covr", empty_data)
    ilst = _mp4_box(b"ilst", covr)
    meta = _mp4_box(b"meta", b"\x00\x00\x00\x00" + ilst)
    udta = _mp4_box(b"udta", meta)
    moov = _mp4_box(b"moov", udta)
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    path = tmp_path / "empty_covr.m4a"
    path.write_bytes(ftyp + moov)
    assert embedded_cover_bytes(path) is None


def test_mp4_largesize_smaller_than_header_stops(tmp_path: Path) -> None:
    """size==1 but largesize < 16 (header) → stop walk, None."""
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    bad = _be32(1) + b"moov" + struct.pack(">Q", 8)  # largesize 8 < 16
    path = tmp_path / "small_large.m4a"
    path.write_bytes(ftyp + bad)
    assert embedded_cover_bytes(path) is None


def test_generic_exception_on_read_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-OSError failures while reading must warn and return None.

    WHY: corrupt tags must never abort a bulk conversion run. We inject a
    RuntimeError from Path.open to exercise the broad except without a
    real filesystem fault.
    """
    path = tmp_path / "x.mp3"
    path.write_bytes(_BASE_MP3)

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("simulated corrupt read")

    monkeypatch.setattr(Path, "open", boom)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert embedded_cover_bytes(path) is None
    assert any("failed reading" in str(w.message) for w in caught)


def test_mp4_box_end_beyond_parent_stops(tmp_path: Path) -> None:
    """Child box size that overruns parent end → stop, no raise."""
    # moov claims size 30 but contains a child claiming size 1000
    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    child = _be32(1000) + b"udta" + b"\x00" * 8
    moov = _be32(8 + len(child)) + b"moov" + child
    # Actually child fits in moov; make moov end before child end by
    # declaring a smaller moov size than child needs.
    moov = _be32(20) + b"moov" + child  # moov size 20, child wants 1000
    path = tmp_path / "over_parent.m4a"
    path.write_bytes(ftyp + moov + b"\x00" * 50)
    assert embedded_cover_bytes(path) is None

