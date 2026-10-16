"""Fill empty Engine overview slots without rebuilding the music library."""
from __future__ import annotations

import datetime
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any


def fill(database: Path, progress, *, context=None, cache_root=None, rebuilt=False) -> dict:
    from rb2engine.overviews import publication as pub
    from rb2engine.overviews._build import build_cache
    from rb2engine.overviews.generator import create_context, generate

    database = pub.database_path(database)
    pub.quiescent(database)
    # Snapshot paths/eligibility, then release the read handle during expensive DSP.
    # prepare/apply recheck the complete logical DB, waveform bytes and audio hashes.
    with closing(pub.connect(database)) as connection:
        connection.execute("BEGIN")
        uid = pub.schema(connection)
        baseline = pub.fingerprint(connection)
        rows = connection.execute(
            "SELECT t.id,t.path,p.overviewWaveFormData FROM Track t "
            "JOIN PerformanceData p ON p.trackId=t.id ORDER BY t.id"
        ).fetchall()
    entries = []
    failures: list[dict[str, Any]] = []
    pending, preserved = [], 0
    for row in rows:
        existing_rgb = pub.rgb_path(database, uid, row["id"]).exists()
        if row["overviewWaveFormData"] or (existing_rgb and not rebuilt):
            # Never replace an existing Engine waveform, including a partial pair.
            preserved += 1
        else:
            pending.append(row)
    result = {"generated": 0, "preserved": preserved, "failures": failures,
              "batch": None, "verified": None, "hardware_validated": False}
    if not pending:
        return result
    context = context or create_context()
    cache = Path(cache_root) if cache_root else build_cache().parent / "audio"
    for index, row in enumerate(pending):
        progress("generating waveforms", index, len(pending))
        try:
            audio = pub.audio_path(database, row["path"])
            generated = generate(audio, cache, context=context)
            entries.append({"track_id": row["id"], "cache_path": generated["cache_path"]})
        except (OSError, ValueError, RuntimeError) as exc:
            failures.append({"track_id": row["id"], "error": str(exc)})
    progress("generating waveforms", len(pending), len(pending))
    if failures:
        # Keep successful cached work for retry, but publish no partial batch.
        return result
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    batch = database.parent.parent / "rb2engine-waveforms" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    progress("writing waveforms", 0, len(entries))
    pub.prepare(database, entries, batch, expected_fingerprint=baseline,
                empty_only=True, replace_orphan_rgb=rebuilt)
    result["batch"] = str(batch)
    result["verified"] = pub.apply(batch)
    result["generated"] = len(entries)
    progress("writing waveforms", len(entries), len(entries))
    return result


def describe(result: dict) -> str:
    lines = [f"Engine overview waveforms: {result['generated']} filled; "
             f"{result['preserved']} existing waveforms preserved."]
    if result["failures"]:
        lines.append("No waveform batch was written. Fix the following files and retry:")
        lines.extend(f"Track {row['track_id']}: {row['error']}" for row in result["failures"])
    if result["batch"]:
        lines.append(f"Waveform backup and verification: {result['batch']}")
    return "\n".join(lines)
