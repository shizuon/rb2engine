# Beatgrid alignment and downbeat numbering

rb2engine reads the source grid; it does not currently infer a new grid from
audio. Correct overview waveforms therefore do not establish that the source
beat times or bar labels are correct.

## Conversion mechanism

1. Rekordbox's PQTZ entries supply the timestamp in milliseconds, BPM metadata
   and a beat-within-bar value from 1 to 4.
2. The reader converts the timestamp to samples at the track sample rate.
3. The mapper compresses the dense positions into sparse Engine markers while
   bounding interpolation error. Engine reconstructs intermediate beats from
   the sample distance divided by the beat-number distance between markers.
4. Engine bar boundaries have a beat number divisible by four. The first
   marker is extrapolated to index -4 and the last past the audio end.
5. The writer stores the default and adjusted marker lists in `beatData`.
   Overview waveform data is generated and stored separately.

The Engine normalization convention is documented by the implementation in
[libdjinterop](https://github.com/xsco/libdjinterop/blob/main/src/djinterop/engine/engine.cpp).
[Mixxx's Engine export](https://github.com/mixxxdj/mixxx/blob/main/src/library/export/engineprimeexportjob.cpp)
likewise chooses a bar-aligned beat as index zero before normalization.

## The bar-phase defect fixed in 0.7.1

The previous mapper used the dense array index as the beat number, ignoring
the first entry's position within its bar. If the source began on beat 3,
every beat timestamp could remain correct while the bar emphasis was two
beats out of phase.

At 120 BPM, consider source entries at 0.25, 0.75, 1.25 and 1.75 seconds,
labelled 3, 4, 1 and 2. Their Engine beat numbers must be 2, 3, 4 and 5,
respectively. The downbeat is 1.25 seconds; choosing array indices 0, 1, 2
and 3 would incorrectly emphasize 0.25 seconds instead.

Version 0.7.1 preserves this initial bar phase. It does not move source beat
timestamps, cues or audio, nor does it infer a new tempo. Tests cover all four
initial bar positions and 44.1/48 kHz. The packaged-app fixture checks the
written database against the authored downbeat/Cue time, not just against a
second invocation of the mapper.

This correction addresses a specific conversion error. It does not prove that
every perceived first-beat problem has that cause, and it does not infer
intentional mid-track changes in meter or bar phase.

## Distinguishing symptoms

| Observed behavior | What to investigate |
|---|---|
| Beat lines land on attacks, but the emphasized bar line is 1–3 beats away | Source bar phase and the converter's beat numbering |
| Every line is early/late by roughly the same fraction of a beat | Source timing offset and decoded-audio time origin |
| Alignment gets progressively worse | Tempo error, half/double tempo interpretation, or source grid changes |
| Only some codecs show a small fixed error | Encoder priming/padding and the time origin used by each decoder |
| Grid and Cue data disappear together | ANLZ parse failures, including optional tags |

PQTZ's whole-millisecond resolution is a different issue from an entire beat
or bar being emphasized incorrectly. Optional PQT2 microsecond refinements are
not enabled in 0.7.1.

## Reusing BeatThis for audio-based checks

The earlier audio-analysis approach uses `beat-this` 1.1.0 with the `small0`
checkpoint and `dbn=False`. The public model returns both beat timestamps and
downbeat timestamps. Its
[documented API](https://github.com/CPJKU/beat_this#python-class) accepts either
an audio file (`File2Beats`) or decoded samples plus their rate (`Audio2Beats`).

That makes it suitable for an independent diagnostic, especially when the
source application has already labelled the wrong downbeat. A basic onset
detector alone cannot reliably decide which of four beats is the musical
downbeat; the loudest attack or first audible event need not be beat 1.

A conservative fixed-tempo correction would:

1. Analyze a local audio copy in a defined sample clock and retain the original
   PQTZ grid and file fingerprints.
2. Compare several separated rhythmic passages. Estimate the global beat-time
   offset separately from the bar-phase offset modulo four.
3. Keep the source BPM when the evidence supports it. Flag tempo disagreement,
   half/double ambiguity, weak intros and inconsistent downbeat predictions.
4. Produce a reviewable shift proposal; use a bar-label shift when beat times
   already align, and a time shift only when timing evidence supports it.
5. Apply only an explicitly selected proposal with backup and readback checks.
   Do not move cues, loops, audio or waveforms merely to match a new grid.

Model agreement is evidence of consistency, not independent musical ground
truth. A first output downbeat should never be treated as an automatically
verified song start. Multiple passages and listening/visual review are still
needed for an acceptance claim. No automatic BeatThis correction or heavyweight
model runtime is included in this release.
