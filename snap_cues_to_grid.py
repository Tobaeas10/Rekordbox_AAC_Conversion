#!/usr/bin/env python3
"""
snap_cues_to_grid.py - snap memory cues, hot cues and loop ends to the beat grid, whole collection.

For every non-deleted track (wav, m4a, mp3, ...): read the beat times from the track's ANLZ .DAT
(PQTZ), and move every cue whose nearest beat is within CUE_SNAP_MARGIN_MS (default 10) onto that beat.
Cues farther away stay untouched (unquantized vocal pickups etc).
Edited in place: djmdCue (InMsec/InFrame/OutMsec/OutFrame), ContentCue JSON, and the cue entries
in the ANLZ files (PCOB/PCO2). Settings come from .env (REKORDBOX_DATABASE_PATH, REKORDBOX_ENCRYPTION_KEY, CUE_SNAP_MARGIN_MS). NO BACKUP is made.
Rekordbox must be closed. Run:  python snap_cues_to_grid.py      (add --dry-run to only report)
"""
import argparse
import bisect
import json
import os
import struct
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import convert_library_to_m4a as converter  # noqa: E402
from convert_library_to_m4a import ContentCue, DjmdContent, DjmdCue  # noqa: E402


def load_env(path):
    configuration = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                configuration[key.strip()] = value.strip().strip('"').strip("'")
    return configuration


def beats_from_dat(data):
    for tag_type, offset, header_length, tag_length in converter.iter_tags(data):
        if tag_type == "PQTZ":
            count = struct.unpack_from(">I", data, offset + 20)[0]
            return sorted(struct.unpack_from(">I", data, offset + 24 + 8 * i + 4)[0] for i in range(count))
    return []


def snap(time_milliseconds, beats, margin):
    insertion_index = bisect.bisect_left(beats, time_milliseconds)
    candidates = ([beats[insertion_index]] if insertion_index < len(beats) else []) + ([beats[insertion_index - 1]] if insertion_index > 0 else [])
    if not candidates:
        return time_milliseconds
    best = min(candidates, key=lambda beat: (abs(beat - time_milliseconds), beat))
    return best if abs(best - time_milliseconds) <= margin else time_milliseconds


def snap_anlz(data, beats, margin):
    """Patch cue/loop times in raw ANLZ bytes. Returns (bytes, [(old, new)])."""
    patched = bytearray(data)
    changes = []
    for tag_type, offset, header_length, tag_length in converter.iter_tags(data):
        if tag_type not in ("PCOB", "PCO2"):
            continue
        for time_offset, loop_offset in converter.cue_entries(patched, tag_type, offset, header_length):
            cue_time = struct.unpack_from(">I", patched, time_offset)[0]
            loop_time = struct.unpack_from(">I", patched, loop_offset)[0]
            if loop_time != 0xFFFFFFFF and loop_time >= cue_time:
                new_loop_time = snap(loop_time, beats, margin)
                if new_loop_time != loop_time:
                    struct.pack_into(">I", patched, loop_offset, new_loop_time)
                    changes.append((loop_time, new_loop_time))
            new_cue_time = snap(cue_time, beats, margin)
            if new_cue_time != cue_time:
                struct.pack_into(">I", patched, time_offset, new_cue_time)
                changes.append((cue_time, new_cue_time))
    return bytes(patched), changes


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    argument_parser.add_argument("--dry-run", action="store_true")
    argument_parser.add_argument("--cue-snap-margin-ms", type=int)
    argument_parser.add_argument("--database-path")
    argument_parser.add_argument("--encryption-key")
    argument_parser.add_argument("--share-directory")
    arguments = argument_parser.parse_args()
    environment_values = load_env(HERE / ".env")
    database_path = arguments.database_path or environment_values.get("REKORDBOX_DATABASE_PATH")
    key = arguments.encryption_key or environment_values.get("REKORDBOX_ENCRYPTION_KEY", "")
    margin = arguments.cue_snap_margin_ms if arguments.cue_snap_margin_ms is not None else int(environment_values.get("CUE_SNAP_MARGIN_MS", "10"))
    if not database_path:
        sys.exit("REKORDBOX_DATABASE_PATH missing (.env or --database-path)")
    if not arguments.dry_run and converter.rekordbox_running():
        sys.exit("Rekordbox is running, close it first")

    database = converter.open_database(database_path, key)
    share = converter.share_directory_of(database, SimpleNamespace(share_directory=arguments.share_directory, database_path=database_path))
    rows = [content_row for content_row in database.query(DjmdContent).all() if not content_row.rb_local_deleted]
    statistics = Counter()
    report = []
    pending = {}                                    # ANLZ path -> patched bytes
    start_time = time.time()
    for track_number, content_row in enumerate(rows, 1):
        files = converter.anlz_files(share, content_row)
        if "DAT" not in files:
            statistics["tracks without ANLZ"] += 1
            continue
        beats = beats_from_dat(files["DAT"].read_bytes())
        if not beats:
            statistics["tracks without grid"] += 1
            continue
        statistics["tracks"] += 1
        label = content_row.FolderPath or str(content_row.ID)
        content_id = str(content_row.ID)

        def fix(value):
            new_value = snap(value, beats, margin)
            if new_value == value:
                statistics["cue values left (off grid or already on it)"] += 1
            else:
                statistics["cue values snapped"] += 1
                report.append(f"{label} | {value} -> {new_value} ({new_value - value:+d} ms)")
            return new_value

        for cue_row in database.query(DjmdCue).filter(DjmdCue.ContentID == content_id).all():
            if cue_row.InMsec is not None and cue_row.InMsec >= 0:
                new_value = fix(cue_row.InMsec)
                if new_value != cue_row.InMsec:
                    cue_row.InMsec, cue_row.InFrame = new_value, converter.frame_of(new_value)
            if cue_row.OutMsec is not None and cue_row.OutMsec >= 0:
                new_value = fix(cue_row.OutMsec)
                if new_value != cue_row.OutMsec:
                    cue_row.OutMsec, cue_row.OutFrame = new_value, converter.frame_of(new_value)
        for content_cue in database.query(ContentCue).filter(ContentCue.ContentID == content_id).all():
            try:
                data = json.loads(content_cue.Cues)
            except Exception:
                continue
            changed = False
            for cue in data:
                for field_name, frame_field in (("InMsec", "InFrame"), ("OutMsec", "OutFrame")):
                    value = cue.get(field_name)
                    if value is not None and value >= 0:
                        new_value = snap(value, beats, margin)
                        if new_value != value:
                            cue[field_name], cue[frame_field], changed = new_value, converter.frame_of(new_value), True
            if changed:
                content_cue.Cues = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        for extension, path in files.items():
            if extension == "2EX":
                continue
            raw = path.read_bytes()
            patched, changes = snap_anlz(raw, beats, margin)
            if changes:
                pending[path] = patched
        if track_number % 200 == 0:
            print(f"  {track_number}/{len(rows)} tracks scanned...", flush=True)

    if not arguments.dry_run:
        database.commit()
        for path, data in pending.items():
            temporary_path = path.with_name(path.name + ".tmp")
            temporary_path.write_bytes(data)
            os.replace(temporary_path, path)
    database.close()
    report_path = HERE / f"snap_report_{time.strftime('%Y%m%d_%H%M%S')}{'_dryrun' if arguments.dry_run else ''}.txt"
    report_path.write_text(f"# margin {margin} ms, {'DRY RUN' if arguments.dry_run else 'applied'}\n" + "\n".join(report) + "\n",
                   encoding="utf-8")
    print(f"\n{'DRY RUN - nothing written' if arguments.dry_run else 'Done'} in {time.time() - start_time:.0f}s, margin {margin} ms")
    for statistic_name, value in statistics.items():
        print(f"  {statistic_name}: {value}")
    print(f"  ANLZ files {'that would be ' if arguments.dry_run else ''}rewritten: {len(pending)}")
    print(f"  details: {report_path}")


if __name__ == "__main__":
    main()