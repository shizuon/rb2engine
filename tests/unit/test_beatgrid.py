"""Beatgrid compression: dense PQTZ → sparse Engine markers.

WHY: Engine stores tempo only as (Δsample_offset)/(Δbeat_number) between sparse
markers. Emitting every rekordbox beat (~550 for a 6-min track) would bloat
beatData and still render wrong without the -4 / past-end convention. These
tests pin the compression invariants that keep grids aligned mid-set.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from rb2engine.ir import SourceBeat, SourceBeatgrid
from rb2engine.ir_engine import EngineBeatGrid, EngineBeatMarker
from rb2engine.mapper.beatgrid import compress_beatgrid

SAMPLE_RATE = 44100


def _spb(bpm: float, sample_rate: int = SAMPLE_RATE) -> float:
    return sample_rate * 60.0 / bpm


def _dense_constant(
    bpm: float,
    n_beats: int,
    *,
    sample_rate: int = SAMPLE_RATE,
    start_sample: int = 0,
    first_beat_in_bar: int = 1,
) -> SourceBeatgrid:
    """Every beat listed, constant tempo — the rekordbox PQTZ shape."""
    spb = _spb(bpm, sample_rate)
    beats: list[SourceBeat] = []
    bib = first_beat_in_bar
    for i in range(n_beats):
        beats.append(
            SourceBeat(
                beat_in_bar=bib,
                sample_offset=start_sample + round(i * spb),
                bpm=bpm,
            )
        )
        bib = bib % 4 + 1
    return SourceBeatgrid(beats=beats, is_adjusted=False)


def _dense_tempo_change(
    bpm_a: float,
    n_a: int,
    bpm_b: float,
    n_b: int,
    *,
    sample_rate: int = SAMPLE_RATE,
    start_sample: int = 0,
) -> SourceBeatgrid:
    """n_a beats at bpm_a, then n_b beats at bpm_b (boundary at index n_a)."""
    spb_a = _spb(bpm_a, sample_rate)
    spb_b = _spb(bpm_b, sample_rate)
    beats: list[SourceBeat] = []
    bib = 1
    for i in range(n_a):
        beats.append(
            SourceBeat(
                beat_in_bar=bib,
                sample_offset=start_sample + round(i * spb_a),
                bpm=bpm_a,
            )
        )
        bib = bib % 4 + 1
    # Boundary sample: continue from end of segment A.
    boundary = start_sample + round(n_a * spb_a)
    for j in range(n_b):
        beats.append(
            SourceBeat(
                beat_in_bar=bib,
                sample_offset=boundary + round(j * spb_b),
                bpm=bpm_b,
            )
        )
        bib = bib % 4 + 1
    return SourceBeatgrid(beats=beats, is_adjusted=False)


def _implied_bpm(
    a: EngineBeatMarker, b: EngineBeatMarker, sample_rate: int = SAMPLE_RATE
) -> float:
    """Tempo Engine infers between adjacent markers."""
    d_beats = b.beat_number - a.beat_number
    assert d_beats != 0
    samples_per_beat = (b.sample_offset - a.sample_offset) / d_beats
    return 60.0 * sample_rate / samples_per_beat


def _markers(grid: EngineBeatGrid) -> list[EngineBeatMarker]:
    # default and adjusted must agree when only PQTZ is available
    assert grid.default_markers == grid.adjusted_markers
    return grid.default_markers


def _reconstructed_sample(markers: list[EngineBeatMarker], beat: int) -> float:
    """Engine's documented linear interpolation, independent of compression."""
    for left, right in pairwise(markers):
        if left.beat_number <= beat <= right.beat_number:
            fraction = (beat - left.beat_number) / (right.beat_number - left.beat_number)
            return left.sample_offset + fraction * (right.sample_offset - left.sample_offset)
    raise AssertionError(f"beat {beat} outside marker range")


@pytest.mark.parametrize("sample_rate", [44100, 48000])
@pytest.mark.parametrize("correction", ["isolated", "step", "drift"])
def test_constant_bpm_preserves_manual_timestamp_geometry(
    sample_rate: int, correction: str
) -> None:
    """Reported 120 BPM does not erase observed beat-position corrections.

    These are explicit millisecond PQTZ positions: one displaced beat, a
    persistent edit from that beat onward, or a continuously drifting grid.
    The expected positions are these inputs, not the compressor's output.
    """
    beats = []
    for index in range(96):
        correction_ms = 0
        if (correction == "isolated" and index == 48) or (correction == "step" and index >= 48):
            correction_ms = 12
        elif correction == "drift":
            correction_ms = index * index // 20
        beats.append(
            SourceBeat(
                beat_in_bar=index % 4 + 1,
                sample_offset=round((index * 500 + correction_ms) * sample_rate / 1000),
                bpm=120.0,
            )
        )
    source = SourceBeatgrid(beats=beats, is_adjusted=True)
    total = beats[-1].sample_offset + sample_rate // 2

    markers = _markers(compress_beatgrid(source, sample_rate=sample_rate, total_samples=total))

    assert len(markers) > 2
    assert markers[0].beat_number == -4
    assert markers[-1].sample_offset > total
    for index, beat in enumerate(beats):
        assert abs(_reconstructed_sample(markers, index) - beat.sample_offset) <= (
            sample_rate / 1000 + 1e-7
        )


def test_millisecond_quantized_constant_grid_remains_sparse() -> None:
    """The tolerance removes timestamp quantization without losing real edits."""
    beats = [
        SourceBeat(
            beat_in_bar=index % 4 + 1,
            sample_offset=round(round(index * 60000 / 128) * SAMPLE_RATE / 1000),
            bpm=128.0,
        )
        for index in range(550)
    ]
    source = SourceBeatgrid(beats=beats, is_adjusted=False)
    total = beats[-1].sample_offset + round(_spb(128))

    markers = _markers(compress_beatgrid(source, sample_rate=SAMPLE_RATE, total_samples=total))

    # Conservative streaming bounds can keep a few extra quantized anchors,
    # but a regular six-minute grid must still compress at least fiftyfold.
    assert len(markers) * 50 < len(beats)
    for index, beat in enumerate(beats):
        assert abs(_reconstructed_sample(markers, index) - beat.sample_offset) <= (
            SAMPLE_RATE / 1000 + 1e-7
        )


class TestConstantTempoCollapses:
    """Constant BPM must become exactly 2 markers (start + end).

    WHY: Without collapse, a 6-minute track writes ~550 markers; Engine only
    needs endpoints, and sparse-tempo math is the documented on-disk form.
    """

    def test_128_bpm_constant_is_two_markers(self) -> None:
        n = 64  # 16 bars
        src = _dense_constant(128.0, n)
        total = src.beats[-1].sample_offset + int(_spb(128.0))
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)

        assert out.is_beatgrid_set is True
        markers = _markers(out)
        assert len(markers) == 2

    def test_implied_tempo_matches_source_bpm(self) -> None:
        bpm = 128.0
        src = _dense_constant(bpm, 32)
        total = src.beats[-1].sample_offset + int(_spb(bpm))
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        m0, m1 = _markers(out)
        assert _implied_bpm(m0, m1) == pytest.approx(bpm, rel=1e-4)


class TestFirstIndexAndExtrapolation:
    """Engine normalize_beatgrid convention.

    WHY: Miss -4 or end-of-track extrapolation and Engine renders the grid
    oddly even when sample positions are otherwise correct.
    """

    def test_first_marker_beat_number_is_minus_four(self) -> None:
        src = _dense_constant(120.0, 16)
        total = src.beats[-1].sample_offset + 10_000
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        assert _markers(out)[0].beat_number == -4

    def test_last_marker_is_past_track_end(self) -> None:
        src = _dense_constant(120.0, 16)
        total = src.beats[-1].sample_offset + 5_000
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        last = _markers(out)[-1]
        assert last.sample_offset > total

    def test_end_exactly_on_a_beat_is_still_extrapolated_past_end(self) -> None:
        src = _dense_constant(120.0, 16)
        total = 16 * 22050
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        last = _markers(out)[-1]
        assert last.beat_number == 17
        assert last.sample_offset == 17 * 22050

    def test_number_of_beats_spans_to_next_marker(self) -> None:
        """number_of_beats on a marker is the run length to the next marker.

        WHY: Encoder/decoder and Engine use this field; last marker is 0.
        """
        src = _dense_constant(100.0, 20)
        total = src.beats[-1].sample_offset + int(_spb(100.0))
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        m0, m1 = _markers(out)
        assert m0.number_of_beats == m1.beat_number - m0.beat_number
        assert m1.number_of_beats == 0


class TestTempoChange:
    """A real mid-track tempo change must emit a boundary marker.

    WHY: Without a marker at the boundary, Engine implies a single average
    tempo across the whole track and every post-change cue lands off-grid.
    """

    def test_boundary_marker_and_implied_tempos(self) -> None:
        bpm_a, bpm_b = 128.0, 140.0
        n_a, n_b = 40, 40
        src = _dense_tempo_change(bpm_a, n_a, bpm_b, n_b)
        total = src.beats[-1].sample_offset + int(_spb(bpm_b))
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)

        markers = _markers(out)
        # start + change + end (after normalize still three sparse points)
        assert len(markers) == 3
        assert markers[0].beat_number == -4

        assert _implied_bpm(markers[0], markers[1]) == pytest.approx(bpm_a, rel=1e-3)
        assert _implied_bpm(markers[1], markers[2]) == pytest.approx(bpm_b, rel=1e-3)

        # Boundary sample should sit at the first beat of the new tempo.
        boundary_sample = float(src.beats[n_a].sample_offset)
        # After normalize only the first marker's sample_offset shifts earlier;
        # the boundary marker (middle) keeps its absolute sample.
        assert markers[1].sample_offset == pytest.approx(boundary_sample, abs=1.0)


class TestRealWorldSizedGrid:
    """~550 beats must compress to a tiny marker list.

    WHY: R6 risk — marker explosion. Compression is the product requirement.
    """

    def test_550_beat_constant_grid_is_tiny(self) -> None:
        n = 550
        src = _dense_constant(128.0, n)
        total = src.beats[-1].sample_offset + int(_spb(128.0))
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        markers = _markers(out)
        assert len(markers) == 2
        assert len(markers) * 50 < n  # drastically smaller than dense list


class TestDownbeatAlignment:
    """rekordbox beat_in_bar == 1 must remain a downbeat after mapping.

    WHY: Muscle-memory phrasing and hot cues on downbeats depend on bar
    alignment surviving the -4 renumber.
    """

    @pytest.mark.parametrize("first_beat_in_bar", [1, 2, 3, 4])
    @pytest.mark.parametrize("sample_rate", [44100, 48000])
    @pytest.mark.parametrize("start_sample", [0, 12345])
    def test_pickup_preserves_source_bar_phase_and_beat_times(
        self, first_beat_in_bar: int, sample_rate: int, start_sample: int
    ) -> None:
        """The first stored beat can be a pickup, not the bar's downbeat."""
        src = _dense_constant(
            120.0, 32, sample_rate=sample_rate, start_sample=start_sample,
            first_beat_in_bar=first_beat_in_bar,
        )
        total = src.beats[-1].sample_offset + sample_rate
        markers = _markers(compress_beatgrid(src, sample_rate=sample_rate, total_samples=total))

        assert markers[0].beat_number == -4
        assert markers[-1].sample_offset > total
        for index, beat in enumerate(src.beats):
            engine_index = index + first_beat_in_bar - 1
            assert (engine_index % 4 == 0) == (beat.beat_in_bar == 1)
            assert _reconstructed_sample(markers, engine_index) == pytest.approx(
                beat.sample_offset, abs=1.0
            )

    def test_downbeats_land_on_mod4_after_normalize(self) -> None:
        bpm = 128.0
        n = 16
        src = _dense_constant(bpm, n, first_beat_in_bar=1)
        total = src.beats[-1].sample_offset + int(_spb(bpm))
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        m0, m1 = _markers(out)

        # First source beat is a downbeat at original sample; after normalize
        # the first marker is 4 beats earlier at index -4, so original first
        # downbeat is beat 0 ≡ 0 (mod 4).
        spb = (m1.sample_offset - m0.sample_offset) / (m1.beat_number - m0.beat_number)
        first_downbeat_sample = m0.sample_offset + (0 - m0.beat_number) * spb
        assert first_downbeat_sample == pytest.approx(
            float(src.beats[0].sample_offset), abs=1.0
        )
        assert m0.beat_number % 4 == 0  # -4


class TestEmptyAndEdgeCases:
    """Missing analysis must not crash the pipeline."""

    def test_empty_beats_is_unset(self) -> None:
        src = SourceBeatgrid(beats=[], is_adjusted=False)
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=1000)
        assert out.is_beatgrid_set is False
        assert out.default_markers == []
        assert out.adjusted_markers == []

    def test_none_total_samples_still_produces_grid(self) -> None:
        src = _dense_constant(128.0, 16)
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=None)
        assert out.is_beatgrid_set is True
        assert len(_markers(out)) == 2
        assert _markers(out)[0].beat_number == -4

    def test_is_adjusted_still_populates_both_grids(self) -> None:
        """PQT2 presence flags is_adjusted; without separate PQT2 times both grids match.

        WHY: Engine reads the adjusted grid for playback — it must never be empty
        when a grid exists (write policy).
        """
        src = _dense_constant(128.0, 16)
        src = SourceBeatgrid(beats=src.beats, is_adjusted=True)
        total = src.beats[-1].sample_offset + int(_spb(128.0))
        out = compress_beatgrid(src, sample_rate=SAMPLE_RATE, total_samples=total)
        assert out.is_beatgrid_set is True
        assert out.default_markers
        assert out.adjusted_markers == out.default_markers
