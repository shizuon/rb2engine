"""Build and invoke the independent signal-processing implementation."""

import ctypes
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def build():
    from .runtime import binary

    bundled = binary("overview-dsp")
    if bundled is not None:
        return bundled
    from ._build import cached_build

    binary, _ = cached_build(
        HERE / "dsp.c", "overview-dsp", shared=True, flags=["-O2", "-ffp-contract=off"]
    )
    return binary


def block_size(rate):
    return int(np.float32(np.float32(1.5873016119003296) * np.float32(0.001)) * rate + 0.5) * 6


def features(decoded, kind, library=None):
    if kind not in ("tri", "rgb"):
        raise ValueError("Unknown waveform kind")
    step = block_size(decoded.rate)
    count = decoded.frames // step + 1
    values = np.zeros((count, 3), np.float32)
    dll = ctypes.CDLL(str(library or build()))
    function = getattr(dll, kind + "_overview_blocks")
    pointer = ctypes.POINTER(ctypes.c_float)
    function.argtypes = [
        pointer,
        ctypes.c_int64,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int64,
        ctypes.c_int64,
        pointer,
        ctypes.c_int64,
    ]
    function.restype = ctypes.c_int64
    pcm = np.asarray(decoded.pcm, dtype=np.float32, order="C")
    for start in range(0, len(pcm), 1048576):
        if not np.isfinite(pcm[start : start + 1048576]).all():
            raise ValueError("Non-finite audio samples")
    offset = decoded.plan["rgb_timestamp_offset"] if kind == "rgb" else 0
    got = function(
        pcm.ctypes.data_as(pointer),
        len(pcm),
        decoded.channels,
        decoded.rate,
        decoded.frames,
        offset,
        values.ctypes.data_as(pointer),
        count,
    )
    if got != count or not np.isfinite(values).all():
        raise ValueError(f"DSP returned invalid block count/data: {got}/{count}")
    return values, step


def reduce_points(values, step, frames):
    ratio = frames / (step * 1024)
    total = np.zeros(3, np.float32)
    n = 0
    points = []
    for index, value in enumerate(values):
        total = np.add(total, value, dtype=np.float32)
        n += 1
        boundary = (len(points) + 1) * ratio
        if boundary >= index + 1:
            continue
        average = np.divide(total, np.float32(n), dtype=np.float32)
        point = np.trunc(np.clip(average, 0, 1) * np.float32(255)).astype(np.uint8)
        while boundary < index + 1 and len(points) < 1024:
            points.append(point.copy())
            boundary += ratio
        total[:] = 0
        n = 0
    if len(points) != 1024:
        raise ValueError(f"Invalid reduced point count: {len(points)}")
    return np.asarray(points, np.uint8)
