#!/usr/bin/env python3
"""
convert_library_to_m4a.py - re-encode Rekordbox wavs to AAC (qaac TVBR 109) and relink master.db.

Why shifts are needed: qaac/Apple AAC adds 2112 priming samples at the start. The iTunSMPB
header says so, but Rekordbox ignores it, so the m4a plays 2112/sr seconds late compared to
the wav. Every cue and beat time (DB + ANLZ files) therefore moves LATER by 2112/sr
(48 ms @44.1k, 44 ms @48k, rounded to whole ms because Rekordbox stores integer ms).

Stages (nothing touches your live library until `install`):
  dry-run  report what would happen, no writes
  encode   qaac each wav -> m4a next to it, verify (iTunSMPB, rate, length, optional xcorr)
  apply    copy master.db into the work dir, relink rows, shift cues, stage edited ANLZ files,
           write the beatgrid reports
  install  back up live master.db + ANLZ files, copy the staged ones over them (Rekordbox closed!)

Example (dev copy, 10 tracks first):
  python convert_library_to_m4a.py --database-path "%APPDATA%\\Pioneer\\rekordbox\\master.db" --encryption-key <key> ^
      --qaac-path "C:\\...\\qaac64.exe" --work-directory work --limit 10 dry-run
  ... encode --cross-correlation-checks 3
  ... apply
  ... install --yes

Requires: pyrekordbox (+ sqlcipher3-wheels), ffmpeg + ffprobe on PATH, numpy only for --cross-correlation-checks.
"""
import argparse
import concurrent.futures as futures
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---- pyrekordbox imports (0.5 layout first, older layout as fallback) ------------------
try:
    from pyrekordbox.masterdb import MasterDatabase as RekordboxDatabase
    from pyrekordbox.masterdb import ContentCue, DjmdContent, DjmdCue
except ImportError:  # pyrekordbox < 0.5
    from pyrekordbox import Rekordbox6Database as RekordboxDatabase
    try:
        from pyrekordbox.db6 import ContentCue, DjmdContent, DjmdCue
    except ImportError:
        from pyrekordbox.db6.tables import ContentCue, DjmdContent, DjmdCue
import struct

EXPECTED_PRIMING = 2112          # samples added by Apple AAC (qaac)
FILE_TYPE_WAV, FILE_TYPE_M4A = 11, 4           # djmdContent.FileType codes
ANLZ_EXTENSIONS = ("DAT", "EXT", "2EX")
NONE_LOOP = (None, -1, 0xFFFFFFFF)
LOCK = threading.Lock()


def log(*messages):
    print(*messages, flush=True)


# ---------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------
def local_path(database_path, path_map):
    """DB path string -> real Path on this machine (optional prefix mapping for dev copies)."""
    path_string = database_path
    for old_prefix, new_prefix in path_map:
        if path_string.lower().startswith(old_prefix.lower()):
            path_string = new_prefix + path_string[len(old_prefix):]
            break
    return Path(path_string)


def swap_extension(path_string, new_extension="m4a"):
    base, dot, _ = path_string.rpartition(".")
    return f"{base}.{new_extension}" if dot else f"{path_string}.{new_extension}"


def frame_of(milliseconds):
    """Rekordbox cue frames are 1/150 s: floor(ms*0.15) (verified on 22k cue rows)."""
    return (milliseconds * 15) // 100


def run(command, **keyword_arguments):
    return subprocess.run(command, capture_output=True, **keyword_arguments)


def ffprobe(path):
    process = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            text=True, encoding="utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {process.stderr.strip()[:200]}")
    return json.loads(process.stdout)


def audio_stream(probe_result):
    for stream in probe_result.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream
    raise RuntimeError("no audio stream")


def wav_info(path):
    probe_result = ffprobe(path)
    stream = audio_stream(probe_result)
    sample_rate = int(stream["sample_rate"])
    if stream.get("duration_ts") and stream.get("time_base"):
        numerator, denominator = (int(part) for part in stream["time_base"].split("/"))
        frames = round(int(stream["duration_ts"]) * numerator / denominator * sample_rate)
    else:
        frames = round(float(probe_result["format"]["duration"]) * sample_rate)
    return {"sr": sample_rate, "channels": int(stream["channels"]), "frames": frames}


def parse_gapless_info(tag):
    parts = tag.split()
    return {"priming": int(parts[1], 16), "end_padding": int(parts[2], 16), "valid": int(parts[3], 16)}


def m4a_info(path):
    probe_result = ffprobe(path)
    stream = audio_stream(probe_result)
    tags = {key.lower(): value for key, value in (probe_result.get("format", {}).get("tags") or {}).items()}
    tags.update({key.lower(): value for key, value in (stream.get("tags") or {}).items()})
    info = {"codec": stream.get("codec_name"), "sr": int(stream["sample_rate"]), "channels": int(stream["channels"]),
            "size": os.path.getsize(path), "smpb": None}
    if "itunsmpb" in tags:
        info["smpb"] = parse_gapless_info(tags["itunsmpb"])
    return info


def raw_decode_mono(path, strip_tags_first, temporary_directory):
    """Decode WITHOUT priming trim. ffmpeg honors iTunSMPB even with -ignore_editlist, so the
    tag has to be stripped by a stream-copy remux first."""
    import numpy as np
    source_path = str(path)
    if strip_tags_first:
        stripped = os.path.join(temporary_directory, "stripped.m4a")
        process = run(["ffmpeg", "-v", "error", "-y", "-i", source_path, "-c", "copy", "-map_metadata", "-1", stripped])
        if process.returncode:
            raise RuntimeError("ffmpeg remux failed")
        source_path = stripped
    command = ["ffmpeg", "-v", "error", "-y"] + (["-ignore_editlist", "1"] if strip_tags_first else []) + \
          ["-i", source_path, "-f", "f32le", "-ac", "1", "-"]
    process = run(command)
    if process.returncode:
        raise RuntimeError("ffmpeg decode failed")
    return np.frombuffer(process.stdout, dtype=np.float32)


def cross_correlation_lag(wav_path, m4a_path):
    """Lag (samples) of the untrimmed m4a decode relative to the wav; expect == priming."""
    import numpy as np
    with tempfile.TemporaryDirectory() as temporary_directory:
        reference = raw_decode_mono(wav_path, False, temporary_directory)
        raw_decode = raw_decode_mono(m4a_path, True, temporary_directory)
    window = min(10 * 44100, len(reference) // 3)
    best = None
    for fraction in (0.25, 0.5, 0.75):                      # pick the loudest window
        window_start = int(len(reference) * fraction)
        window_start = max(4000, min(window_start, len(reference) - window - 8000))
        root_mean_square = float(np.sqrt(np.mean(reference[window_start:window_start + window] ** 2)))
        if best is None or root_mean_square > best[0]:
            best = (root_mean_square, window_start)
    window_start = best[1]
    segment = raw_decode[window_start - 4000: window_start + window + 8000]
    reference_window = reference[window_start:window_start + window]
    fft_size = 1 << (len(segment) + len(reference_window)).bit_length()
    correlation = np.fft.irfft(np.fft.rfft(segment, fft_size) * np.conj(np.fft.rfft(reference_window, fft_size)), fft_size)
    lags = correlation[: len(segment) - len(reference_window) + 1]
    return int(np.argmax(lags)) - 4000


def verify_m4a(wav_properties, m4a_path, cross_correlation_checks, wav_path):
    info = m4a_info(m4a_path)
    if info["codec"] != "aac":
        return False, f"codec is {info['codec']}", info
    if info["sr"] != wav_properties["sr"] or info["channels"] != wav_properties["channels"]:
        return False, f"rate/channels changed ({info['sr']}/{info['channels']} vs {wav_properties['sr']}/{wav_properties['channels']})", info
    gapless_info = info["smpb"]
    if not gapless_info:
        return False, "no iTunSMPB tag", info
    if gapless_info["priming"] != EXPECTED_PRIMING:
        return False, f"priming {gapless_info['priming']} != {EXPECTED_PRIMING}", info
    if gapless_info["valid"] != wav_properties["frames"]:
        return False, f"valid length {gapless_info['valid']} != wav frames {wav_properties['frames']}", info
    if cross_correlation_checks:
        lag = cross_correlation_lag(wav_path, m4a_path)
        info["xcorr_lag"] = lag
        if lag != gapless_info["priming"]:
            return False, f"xcorr lag {lag} != priming {gapless_info['priming']}", info
    return True, "ok", info


# ---------------------------------------------------------------------------------------
# DB / manifest helpers
# ---------------------------------------------------------------------------------------
def open_database(path, key):
    keyword_arguments = {"path": str(path)}
    if key:
        keyword_arguments["key"] = key
    return RekordboxDatabase(**keyword_arguments)


def share_directory_of(database, arguments):
    if arguments.share_directory:
        return Path(arguments.share_directory)
    share_directory_value = getattr(database, "_share_dir", None)
    return Path(share_directory_value) if share_directory_value else Path(arguments.database_path).parent / "share"


def load_manifest(work_directory):
    manifest_path = work_directory / "manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}


def save_manifest(work_directory, manifest):
    work_directory.mkdir(parents=True, exist_ok=True)
    temporary_path = work_directory / "manifest.json.tmp"
    with LOCK:
        temporary_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(temporary_path, work_directory / "manifest.json")
                break
            except PermissionError:
                print(f"Couldn't access manifest.json. Trying agian in 5 seconds. Attempt {attempt + 1} out of 20")
                if attempt == 19:
                    raise
                time.sleep(5)

def select_rows(database, arguments):
    rows = database.query(DjmdContent).filter(DjmdContent.FileType == FILE_TYPE_WAV).all()
    selected_rows = []
    for content_row in rows:
        if content_row.rb_local_deleted or not content_row.FolderPath or not content_row.FolderPath.lower().endswith(".wav"):
            continue
        if arguments.track_ids and str(content_row.ID) not in arguments.track_ids:
            continue
        if arguments.match and arguments.match.lower() not in content_row.FolderPath.lower():
            continue
        selected_rows.append(content_row)
    selected_rows.sort(key=lambda content_row: str(content_row.ID))
    return selected_rows[: arguments.limit] if arguments.limit else selected_rows


def make_entry(content_row, arguments, used_paths):
    old_path = content_row.FolderPath
    new_path = swap_extension(old_path)
    entry = {"id": str(content_row.ID), "old_path": old_path, "new_path": new_path,
         "src": str(local_path(old_path, arguments.path_prefix_map)), "dst": str(local_path(new_path, arguments.path_prefix_map)),
         "status": "planned", "reason": ""}
    if new_path.lower() in used_paths:
        entry.update(status="skip", reason="new m4a path already used by another DB row")
    elif not Path(entry["src"]).is_file():
        entry.update(status="skip", reason="wav file missing on disk")
    return entry


def anlz_files(share, content):
    """Existing ANLZ files {ext: Path} for a content row, resolved against the share dir."""
    if not content.AnalysisDataPath:
        return {}
    relative_path = Path(content.AnalysisDataPath.replace("\\", "/").strip("/"))
    directory = share / relative_path.parent
    files_by_extension = {}
    for extension in ANLZ_EXTENSIONS:
        for path in directory.glob(f"ANLZ*.{extension}"):
            files_by_extension[extension] = path
    return files_by_extension


# ---- ANLZ byte-level access ------------------------------------------------------------
# pyrekordbox 0.5 cannot REBUILD ANLZ files that contain cue entries (its PCPT struct has a
# duplicate "type" field), so beat/cue times are patched directly in the raw bytes. Layout
# (big endian): file header "PMAI" (len_header @4, len_file @8), then tags
# [type 4][len_header 4][len_tag 4]...  Offsets below are relative to the tag start.
#   PQTZ: entry_count @20, entries from 24, 8 bytes each (beat u16, tempo u16, time u32)
#   PCOB: count u16 @18, entries from len_header; PCPT entry: len_entry @8, time @32, loop @36
#   PCO2: count u16 @16, entries from len_header; PCP2 entry: len_entry @8, time @20, loop @24
#   PQT2: two (beat,tempo,time) pairs @24 and @32, entry_count @40
#   PPTH: len_path @12, UTF-16BE path from @16
def iter_tags(data):
    offset = struct.unpack_from(">I", data, 4)[0]
    while offset + 12 <= len(data):
        tag_type = bytes(data[offset:offset + 4]).decode("ascii", "replace")
        header_length, tag_length = struct.unpack_from(">II", data, offset + 4)
        if tag_length < 12:
            break
        yield tag_type, offset, header_length, tag_length
        offset += tag_length


def cue_entries(data, tag_type, offset, header_length):
    """Yield (time_offset, loop_offset) absolute offsets of cue entries in a PCOB/PCO2 tag."""
    if tag_type == "PCOB":
        count, time_offset, loop_offset = struct.unpack_from(">H", data, offset + 18)[0], 32, 36
    else:
        count, time_offset, loop_offset = struct.unpack_from(">H", data, offset + 16)[0], 20, 24
    position = offset + header_length
    for _ in range(count):
        yield position + time_offset, position + loop_offset
        position += struct.unpack_from(">I", data, position + 8)[0]


def grid_info(data):
    """First downbeat etc. from the (ORIGINAL) DAT PQTZ. Times in ms."""
    for tag_type, offset, header_length, tag_length in iter_tags(data):
        if tag_type != "PQTZ":
            continue
        count = struct.unpack_from(">I", data, offset + 20)[0]
        entries = [struct.unpack_from(">HHI", data, offset + 24 + 8 * i) for i in range(count)]
        downbeat_index = next((i for i, entry in enumerate(entries) if entry[0] == 1), None)
        if downbeat_index is None:
            return None
        bpm = entries[downbeat_index][1] / 100.0
        return {"first_beat_ms": entries[0][2], "first_downbeat_ms": entries[downbeat_index][2], "bpm": bpm,
                "bar_ms": 4 * 60000.0 / bpm if bpm else None}
    return None


def time_signature(data):
    """All beat/cue times in a file, for before/after comparison."""
    times = []
    for tag_type, offset, header_length, tag_length in iter_tags(data):
        if tag_type == "PQTZ":
            count = struct.unpack_from(">I", data, offset + 20)[0]
            times += [struct.unpack_from(">I", data, offset + 24 + 8 * i + 4)[0] for i in range(count)]
        elif tag_type in ("PCOB", "PCO2"):
            times += [struct.unpack_from(">I", data, time_offset)[0] for time_offset, _ in cue_entries(data, tag_type, offset, header_length)]
    return times


def shift_anlz(data, shift, new_extension="m4a"):
    """Return (patched bytes, stats). Length never changes (wav -> m4a are both 3 chars)."""
    patched = bytearray(data)
    statistics = Counter()
    for tag_type, offset, header_length, tag_length in iter_tags(data):
        if tag_type == "PQTZ":
            count = struct.unpack_from(">I", patched, offset + 20)[0]
            for i in range(count):
                position = offset + 24 + 8 * i + 4
                struct.pack_into(">I", patched, position, struct.unpack_from(">I", patched, position)[0] + shift)
                statistics["beats"] += 1
        elif tag_type in ("PCOB", "PCO2"):
            for time_offset, loop_offset in cue_entries(patched, tag_type, offset, header_length):
                cue_time = struct.unpack_from(">I", patched, time_offset)[0]
                loop_time = struct.unpack_from(">I", patched, loop_offset)[0]
                if loop_time != 0xFFFFFFFF and loop_time >= cue_time:
                    struct.pack_into(">I", patched, loop_offset, loop_time + shift)
                struct.pack_into(">I", patched, time_offset, cue_time + shift)
                statistics["cues"] += 1
        elif tag_type == "PQT2":       # format not verified: shift + flag for manual review
            if struct.unpack_from(">I", patched, offset + 40)[0] > 0:
                for position in (offset + 28, offset + 36):
                    value = struct.unpack_from(">I", patched, position)[0]
                    if value:
                        struct.pack_into(">I", patched, position, value + shift)
                statistics["pqt2_review"] += 1
        elif tag_type == "PPTH":
            count = struct.unpack_from(">I", patched, offset + 12)[0]
            end = offset + 16 + count - 2                      # path without the 2-byte terminator
            tail = bytes(patched[end - 6:end]).decode("utf-16-be", "replace")
            if tail.lower() == "wav":
                patched[end - 6:end] = new_extension.encode("utf-16-be")
                statistics["ppth"] += 1
    return bytes(patched), statistics


# ---------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------
def command_dry_run(arguments):
    database = open_database(arguments.database_path, arguments.encryption_key)
    share = share_directory_of(database, arguments)
    rows = select_rows(database, arguments)
    used_paths = {str(content_row.FolderPath).lower() for content_row in database.query(DjmdContent).all()
            if content_row.FolderPath and not content_row.rb_local_deleted}
    lines, counts = [], Counter()
    for content_row in rows:
        entry = make_entry(content_row, arguments, used_paths)
        if entry["status"] == "skip":
            counts["skip"] += 1
            lines.append(f"[skip] {entry['old_path']}  ({entry['reason']})")
            continue
        wav_properties = wav_info(entry["src"])
        shift = round(EXPECTED_PRIMING * 1000 / wav_properties["sr"])
        cue_count = database.query(DjmdCue).filter(DjmdCue.ContentID == str(content_row.ID)).count()
        files = anlz_files(share, content_row)
        flags = []
        if "DAT" not in files:
            flags.append("NO-ANLZ-DAT")
        else:
            grid = grid_info(files["DAT"].read_bytes())
            if grid is None:
                flags.append("NO-GRID")
            else:
                if grid["first_downbeat_ms"] <= arguments.first_sample_tolerance_ms:
                    flags.append("GRID-ON-FIRST-SAMPLE")
                if grid["bar_ms"] and abs(grid["first_downbeat_ms"] - grid["bar_ms"]) <= arguments.one_bar_tolerance_ms:
                    flags.append("DOWNBEAT-ONE-BAR")
        counts["ok"] += 1
        lines.append(f"[ok]   {entry['old_path']} -> .m4a  sr={wav_properties['sr']} shift=+{shift}ms cues={cue_count} "
                     f"anlz={'/'.join(files) or '-'} {' '.join(flags)}")
    log("\n".join(lines))
    log(f"\n{counts['ok']} would be converted, {counts['skip']} skipped.")
    (arguments.work_directory).mkdir(parents=True, exist_ok=True)
    (arguments.work_directory / "dry_run.txt").write_text("\n".join(lines), encoding="utf-8")


def encode_one(entry, arguments):
    source_path, destination_path = Path(entry["src"]), Path(entry["dst"])
    temporary_path = destination_path.with_name(destination_path.stem + ".__tmp__.m4a")
    if temporary_path.exists():
        temporary_path.unlink()
    if destination_path.exists() and not entry.get("ours") and not arguments.adopt_existing:
        return "skip", "target m4a already exists (use --adopt-existing to verify and use it)"
    wav_properties = wav_info(source_path)
    if destination_path.exists() and (entry.get("ours") or arguments.adopt_existing):
        ok, message, info = verify_m4a(wav_properties, destination_path, 0, source_path)       # reuse previous encode
        if ok:
            return "encoded", {"w": wav_properties, "info": info}
        return "failed", f"existing m4a does not verify: {message}"
    process = run([arguments.qaac_path, "--true-vbr-quality", str(arguments.true_vbr_quality), str(source_path), "-o", str(temporary_path)], text=True, errors="replace")
    if process.returncode != 0 or not temporary_path.exists():
        temporary_path.unlink(missing_ok=True)
        return "failed", f"qaac exit {process.returncode}: {process.stderr.strip()[-200:]}"
    ok, message, info = verify_m4a(wav_properties, temporary_path, arguments._cross_correlation_checks_left(), source_path)
    if not ok:
        temporary_path.unlink(missing_ok=True)
        return "failed", message
    os.replace(temporary_path, destination_path)
    info["size"] = os.path.getsize(destination_path)
    return "encoded", {"w": wav_properties, "info": info}


def command_encode(arguments):
    database = open_database(arguments.database_path, arguments.encryption_key)
    rows = select_rows(database, arguments)
    used_paths = {str(content_row.FolderPath).lower() for content_row in database.query(DjmdContent).all()
            if content_row.FolderPath and not content_row.rb_local_deleted}
    manifest = load_manifest(arguments.work_directory)
    todo = []
    for content_row in rows:
        current = manifest.get(str(content_row.ID))
        if current and current["status"] in ("encoded", "applied", "installed"):
            continue
        entry = make_entry(content_row, arguments, used_paths)
        if current and current.get("ours"):
            entry["ours"] = True
        manifest[entry["id"]] = entry
        if entry["status"] == "planned":
            todo.append(entry)
        else:
            log(f"[skip] {entry['old_path']} ({entry['reason']})")
    save_manifest(arguments.work_directory, manifest)
    log(f"{len(todo)} wavs to encode")
    cross_correlation_checks_left = [arguments.cross_correlation_checks]
    check_lock = threading.Lock()

    def take_cross_correlation_check():
        with check_lock:
            if cross_correlation_checks_left[0] > 0:
                cross_correlation_checks_left[0] -= 1
                return 1
            return 0
    arguments._cross_correlation_checks_left = take_cross_correlation_check

    def encode_entry(entry):
        entry["ours"] = True
        status, result = encode_one(entry, arguments)
        with LOCK:
            if status == "encoded":
                wav_properties, info = result["w"], result["info"]
                gapless_info = info["smpb"]
                duration = gapless_info["valid"] / wav_properties["sr"]
                entry.update(status="encoded", sr=wav_properties["sr"], priming=gapless_info["priming"], valid=gapless_info["valid"],
                         shift_ms=round(gapless_info["priming"] * 1000 / wav_properties["sr"]), size=info["size"],
                         bitrate_kbps=round(info["size"] * 8 / 1000 / duration), reason="",
                         xcorr_lag=info.get("xcorr_lag"))
            else:
                entry.update(status=status, reason=result, ours=entry.get("ours") and status != "failed")
        save_manifest(arguments.work_directory, manifest)
        log(f"[{entry['status']}] {entry['old_path']} {entry.get('reason', '')}"
            f"{' shift=+%dms' % entry['shift_ms'] if entry['status'] == 'encoded' else ''}")

    with futures.ThreadPoolExecutor(max_workers=arguments.parallel_workers) as executor:
        list(executor.map(encode_entry, todo))
    status_counts = Counter(entry["status"] for entry in manifest.values())
    log(f"done: {dict(status_counts)}")


def command_apply(arguments):
    manifest = load_manifest(arguments.work_directory)
    todo = [entry for entry in manifest.values() if entry["status"] in ("encoded", "applied")]
    if not todo:
        log("nothing to apply (run encode first)")
        return
    work_database = arguments.work_directory / "master.db"
    staging_directory = arguments.work_directory / "share"
    if staging_directory.exists():
        shutil.rmtree(staging_directory)
    shutil.copy2(arguments.database_path, work_database)                       # always restart from the live DB
    database = open_database(work_database, arguments.encryption_key)
    share = share_directory_of(open_database(arguments.database_path, arguments.encryption_key), arguments)
    first_sample_report, one_bar_report, problems = [], [], []
    applied_count = 0
    for entry in todo:
        content_id, shift = entry["id"], int(entry["shift_ms"])
        content = database.query(DjmdContent).filter(DjmdContent.ID == content_id).one_or_none()
        if content is None or content.FolderPath != entry["old_path"]:
            problems.append(f"{entry['old_path']}: DB row changed or gone, skipped")
            continue
        # --- ANLZ (read the originals, stage the edited copies)
        files = anlz_files(share, content)
        info = None
        staged = []
        try:
            for extension, path in files.items():
                raw = path.read_bytes()
                before = time_signature(raw)
                if extension == "DAT":
                    info = grid_info(raw)
                patched, statistics = shift_anlz(raw, shift)
                if statistics["pqt2_review"]:
                    problems.append(f"{entry['new_path']}: populated PQT2 in {extension}, check grid manually")
                if len(patched) != len(raw):
                    raise RuntimeError("length changed")
                staged_path = staging_directory / path.relative_to(share)
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_bytes(patched)
                after = time_signature(staged_path.read_bytes())
                if after != [time_value + shift for time_value in before]:
                    raise RuntimeError(f"staged {extension} failed self-check")
                staged.append(staged_path)
        except Exception as error:
            for staged_file in staged:
                staged_file.unlink(missing_ok=True)
            problems.append(f"{entry['old_path']}: ANLZ error ({error}), track skipped entirely")
            continue
        if "DAT" not in files:
            problems.append(f"{entry['new_path']}: no ANLZ DAT found, DB cues shifted but beatgrid not")
        elif info is None:
            problems.append(f"{entry['new_path']}: no beat grid in DAT")
        else:
            description = f"{entry['new_path']} | bpm {info['bpm']:.2f} | first downbeat {info['first_downbeat_ms']} ms"
            if info["first_downbeat_ms"] <= arguments.first_sample_tolerance_ms:
                first_sample_report.append(description)
            if info["bar_ms"] and abs(info["first_downbeat_ms"] - info["bar_ms"]) <= arguments.one_bar_tolerance_ms:
                one_bar_report.append(description + f" | 1 bar = {info['bar_ms']:.1f} ms "
                                      f"(delta {info['first_downbeat_ms'] - info['bar_ms']:+.1f} ms)")
        # --- DB: content row
        content.FolderPath = entry["new_path"]
        content.FileNameL = entry["new_path"].rsplit("/", 1)[-1]
        content.FileType = FILE_TYPE_M4A
        content.FileSize = entry["size"]
        content.BitRate = entry["bitrate_kbps"]
        content.BitDepth = 16
        for field_name in ("OrgFolderPath", "rb_LocalFolderPath"):
            if getattr(content, field_name, None) == entry["old_path"]:
                setattr(content, field_name, entry["new_path"])
        # --- DB: cues
        cue_snapshot = {}
        for cue_row in database.query(DjmdCue).filter(DjmdCue.ContentID == content_id).all():
            cue_snapshot[str(cue_row.ID)] = [cue_row.InMsec, cue_row.OutMsec]
            if cue_row.InMsec is not None:
                cue_row.InMsec += shift
                cue_row.InFrame = frame_of(cue_row.InMsec)
            if cue_row.OutMsec is not None and cue_row.OutMsec >= 0:
                cue_row.OutMsec += shift
                cue_row.OutFrame = frame_of(cue_row.OutMsec)
        for content_cue in database.query(ContentCue).filter(ContentCue.ContentID == content_id).all():
            try:
                data = json.loads(content_cue.Cues)
            except Exception:
                problems.append(f"{entry['new_path']}: ContentCue JSON unreadable, left unchanged")
                continue
            for cue_data in data:
                if cue_data.get("InMsec") is not None:
                    cue_data["InMsec"] += shift
                    cue_data["InFrame"] = frame_of(cue_data["InMsec"])
                if cue_data.get("OutMsec") is not None and cue_data["OutMsec"] >= 0:
                    cue_data["OutMsec"] += shift
                    cue_data["OutFrame"] = frame_of(cue_data["OutMsec"])
            content_cue.Cues = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        entry["cue_snapshot"] = cue_snapshot
        entry["anlz_staged"] = [str(staged_file.relative_to(staging_directory)) for staged_file in staged]
        entry["status"] = "applied"
        applied_count += 1
    database.commit()
    database.close()
    save_manifest(arguments.work_directory, manifest)
    # --- post-commit check of the staged DB
    checking_database = open_database(work_database, arguments.encryption_key)
    failure_count = 0
    for entry in manifest.values():
        if entry["status"] != "applied":
            continue
        content = checking_database.query(DjmdContent).filter(DjmdContent.ID == entry["id"]).one()
        ok = content.FolderPath == entry["new_path"] and content.FileType == FILE_TYPE_M4A
        for cue_row in checking_database.query(DjmdCue).filter(DjmdCue.ContentID == entry["id"]).all():
            in_milliseconds, out_milliseconds = entry["cue_snapshot"][str(cue_row.ID)]
            ok &= cue_row.InMsec == (None if in_milliseconds is None else in_milliseconds + entry["shift_ms"])
            ok &= cue_row.OutMsec == (out_milliseconds if out_milliseconds is None or out_milliseconds < 0 else out_milliseconds + entry["shift_ms"])
        if not ok:
            failure_count += 1
            problems.append(f"{entry['new_path']}: staged DB self-check FAILED")
    checking_database.close()
    reports_directory = arguments.work_directory / "reports"
    reports_directory.mkdir(exist_ok=True)
    (reports_directory / "beatgrid_first_sample.txt").write_text(
        "# wavs (now m4a) whose first DOWNBEAT was on the very first sample (<= %d ms) in the old grid\n" % arguments.first_sample_tolerance_ms
        + "\n".join(first_sample_report) + "\n", encoding="utf-8")
    (reports_directory / "beatgrid_one_bar_offset.txt").write_text(
        "# wavs (now m4a) whose first downbeat was within %d ms of exactly one bar from the start\n" % arguments.one_bar_tolerance_ms
        + "\n".join(one_bar_report) + "\n", encoding="utf-8")
    (reports_directory / "problems.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
    log(f"applied {applied_count} tracks ({failure_count} self-check failures, {len(problems)} problems).")
    log(f"staged DB: {work_database}\nstaged ANLZ: {staging_directory}\nreports: {reports_directory}")
    log(f"first-sample list: {len(first_sample_report)}, one-bar list: {len(one_bar_report)}")


def rekordbox_running():
    if os.name != "nt":
        return False
    process = run(["tasklist", "/FI", "IMAGENAME eq rekordbox.exe"], text=True, errors="replace")
    return "rekordbox.exe" in (process.stdout or "").lower()


def command_install(arguments):
    if not arguments.yes:
        log("refusing without --yes (this overwrites your live master.db and ANLZ files)")
        return
    if rekordbox_running():
        log("Rekordbox is running, close it first")
        return
    manifest = load_manifest(arguments.work_directory)
    todo = [entry for entry in manifest.values() if entry["status"] == "applied"]
    if not todo:
        log("nothing applied")
        return
    probe_database = open_database(arguments.database_path, arguments.encryption_key)
    share = share_directory_of(probe_database, arguments)
    probe_database.close()
    backup_directory = arguments.work_directory / ("backup_" + time.strftime("%Y%m%d_%H%M%S"))
    backup_directory.mkdir(parents=True)
    shutil.copy2(arguments.database_path, backup_directory / "master.db")
    staging_directory = arguments.work_directory / "share"
    file_count = 0
    for entry in todo:
        for relative_path in entry["anlz_staged"]:
            live_path = share / relative_path
            if live_path.exists():
                (backup_directory / "share" / relative_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live_path, backup_directory / "share" / relative_path)
            shutil.copy2(staging_directory / relative_path, live_path)
            file_count += 1
    shutil.copy2(arguments.work_directory / "master.db", arguments.database_path)
    for entry in todo:
        entry["status"] = "installed"
    save_manifest(arguments.work_directory, manifest)
    log(f"installed {len(todo)} tracks, {file_count} ANLZ files. backup: {backup_directory}")


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    argument_parser.add_argument("--database-path", required=True, help="path to the live master.db")
    argument_parser.add_argument("--encryption-key", default="", help="SQLCipher key (default: pyrekordbox auto-detects)")
    argument_parser.add_argument("--share-directory", help="Rekordbox share dir (default: next to master.db)")
    argument_parser.add_argument("--qaac-path", default="qaac64.exe")
    argument_parser.add_argument("--work-directory", type=Path, default=Path("work"))
    argument_parser.add_argument("--path-prefix-map", action="append", default=[], metavar="OLD=NEW",
                                 help="map DB path prefix to a local one (repeatable)")
    argument_parser.add_argument("--limit", type=int, default=0)
    argument_parser.add_argument("--track-ids", default="", help="comma separated djmdContent IDs")
    argument_parser.add_argument("--match", default="")
    argument_parser.add_argument("--true-vbr-quality", type=int, default=109, help="qaac TVBR quality 0-127 (default 109)")
    argument_parser.add_argument("--parallel-workers", type=int, default=1)
    argument_parser.add_argument("--cross-correlation-checks", type=int, default=3, help="cross-correlate the first N encodes (0=off)")
    argument_parser.add_argument("--adopt-existing", action="store_true")
    argument_parser.add_argument("--first-sample-tolerance-ms", type=int, default=0)
    argument_parser.add_argument("--one-bar-tolerance-ms", type=int, default=15)
    argument_parser.add_argument("--yes", action="store_true")
    argument_parser.add_argument("command", choices=["dry-run", "encode", "apply", "install"])
    arguments = argument_parser.parse_args()
    if not 0 <= arguments.true_vbr_quality <= 127:
        argument_parser.error("--true-vbr-quality must be between 0 and 127")
    arguments.track_ids = [item for item in arguments.track_ids.split(",") if item]
    arguments.path_prefix_map = [tuple(item.split("=", 1)) for item in arguments.path_prefix_map]
    arguments.work_directory.mkdir(parents=True, exist_ok=True)
    {"dry-run": command_dry_run, "encode": command_encode, "apply": command_apply, "install": command_install}[arguments.command](arguments)


if __name__ == "__main__":
    main()