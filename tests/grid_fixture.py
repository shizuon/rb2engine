"""Authored drum/bass fixture with deliberate grid errors; no recorded music."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from tests.desktop_fixture import make_drive
from tests.unit.test_anlz import _pmai, _pqtz


def percussion(rate: int = 44100, duration: int = 96) -> np.ndarray:
    signal = np.zeros(rate * duration, dtype=np.float32)
    rng = np.random.default_rng(4231)

    def add(start: float, sound: np.ndarray) -> None:
        index = round(start * rate)
        size = min(len(sound), len(signal) - index)
        if size > 0:
            signal[index : index + size] += sound[:size]

    for i in range(duration * 2):
        start = 0.25 + i * 0.5
        t = np.arange(round(rate * 0.28)) / rate
        kick = 0.7 * np.sin(2 * np.pi * (48 * t + 5 * (1 - np.exp(-30 * t)))) * np.exp(-18 * t)
        add(start, kick * (1 if i % 4 == 0 else .4))
        if i % 4 == 2:
            snare = rng.standard_normal(len(t)) * 0.24 * np.exp(-24 * t)
            snare += np.sin(2 * np.pi * 180 * t) * 0.12 * np.exp(-25 * t)
            add(start, snare)
        for off in (0, 0.25):
            t = np.arange(round(rate * 0.07)) / rate
            noise = rng.standard_normal(len(t))
            hat = np.diff(noise, prepend=0) * 0.035 * np.exp(-70 * t)
            add(start + off, hat)
        t = np.arange(round(rate * 0.43)) / rate
        freq = [55, 55, 82.4069, 65.4064][i % 4]
        bass = sum(np.sin(2 * np.pi * freq * h * t) / h for h in (1, 2, 3))
        add(start, bass * (0.18 if i % 4 == 0 else 0.10) * np.exp(-8 * t))
        if i % 4 == 0:
            t = np.arange(round(rate * 0.7)) / rate
            chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 261.6256, 329.6276))
            add(start, 0.25 * chord * np.exp(-7 * t))
    return np.clip(signal, -0.95, 0.95)


def make_grid_drive(root: Path) -> Path:
    make_drive(root, analysis=True, duration=96)
    signal = (percussion() * 32767).astype("<i2")
    with wave.open(str(root / "Contents/test.wav"), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(np.repeat(signal[:, None], 2, axis=1).tobytes())
    dat = root / "PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT"
    # Audio beat 1 at .25 seconds; source is 80 ms late and calls that beat 3.
    dat.write_bytes(_pmai([_pqtz([((i + 2) % 4 + 1, 12000, 330 + i * 500) for i in range(192)])]))
    return root
