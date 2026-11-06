#!/usr/bin/env python3
"""
rb_wav_to_m4a.py - re-encode Rekordbox wavs to AAC (qaac TVBR 109) and relink master.db.

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
  python rb_wav_to_m4a.py --db "%APPDATA%\\Pioneer\\rekordbox\\master.db" --key <key> ^
      --qaac "C:\\...\\qaac64.exe" --work work --limit 10 dry-run
  ... encode --xcorr 3
  ... apply
  ... install --yes

Requires: pyrekordbox (+ sqlcipher3-wheels), ffmpeg + ffprobe on PATH, numpy only for --xcorr.
"""
import argparse
import concurrent.futures as cf
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
    from pyrekordbox.masterdb import MasterDatabase as RBDatabase
    from pyrekordbox.masterdb import ContentCue, DjmdContent, DjmdCue
except ImportError:  # pyrekordbox < 0.5
    from pyrekordbox import Rekordbox6Database as RBDatabase
    try:
        from pyrekordbox.db6 import ContentCue, DjmdContent, DjmdCue
    except ImportError:
        from pyrekordbox.db6.tables import ContentCue, DjmdContent, DjmdCue
import struct

EXPECTED_PRIMING = 2112          # samples added by Apple AAC (qaac)
FT_WAV, FT_M4A = 11, 4           # djmdContent.FileType codes
ANLZ_EXTS = ("DAT", "EXT", "2EX")
NONE_LOOP = (None, -1, 0xFFFFFFFF)
LOCK = threading.Lock()


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------
def local_path(db_path, path_map):
    """DB path string -> real Path on this machine (optional prefix mapping for dev copies)."""
    s = db_path
    for old, new in path_map:
        if s.lower().startswith(old.lower()):
            s = new + s[len(old):]
            break
    return Path(s)


def swap_ext(path_str, new_ext="m4a"):
    base, dot, _ = path_str.rpartition(".")
    return f"{base}.{new_ext}" if dot else f"{path_str}.{new_ext}"


def frame_of(ms):
    """Rekordbox cue frames are 1/150 s: floor(ms*0.15) (verified on 22k cue rows)."""
    return (ms * 15) // 100


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def ffprobe(path):
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {p.stderr.strip()[:200]}")
    return json.loads(p.stdout)


def audio_stream(j):
    for s in j.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    raise RuntimeError("no audio stream")


def wav_info(path):
    j = ffprobe(path)
    s = audio_stream(j)
    sr = int(s["sample_rate"])
    if s.get("duration_ts") and s.get("time_base"):
        n, d = (int(x) for x in s["time_base"].split("/"))
        frames = round(int(s["duration_ts"]) * n / d * sr)
    else:
        frames = round(float(j["format"]["duration"]) * sr)
    return {"sr": sr, "channels": int(s["channels"]), "frames": frames}


def parse_smpb(tag):
    p = tag.split()
    return {"priming": int(p[1], 16), "end_padding": int(p[2], 16), "valid": int(p[3], 16)}


def m4a_info(path):
    j = ffprobe(path)
    s = audio_stream(j)
    tags = {k.lower(): v for k, v in (j.get("format", {}).get("tags") or {}).items()}
    tags.update({k.lower(): v for k, v in (s.get("tags") or {}).items()})
    info = {"codec": s.get("codec_name"), "sr": int(s["sample_rate"]), "channels": int(s["channels"]),
            "size": os.path.getsize(path), "smpb": None}
    if "itunsmpb" in tags:
        info["smpb"] = parse_smpb(tags["itunsmpb"])
    return info


def raw_decode_mono(path, strip_tags_first, tmpdir):
    """Decode WITHOUT priming trim. ffmpeg honors iTunSMPB even with -ignore_editlist, so the
    tag has to be stripped by a stream-copy remux first."""
    import numpy as np
    src = str(path)
    if strip_tags_first:
        stripped = os.path.join(tmpdir, "stripped.m4a")
        p = run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c", "copy", "-map_metadata", "-1", stripped])
        if p.returncode:
            raise RuntimeError("ffmpeg remux failed")
        src = stripped
    cmd = ["ffmpeg", "-v", "error", "-y"] + (["-ignore_editlist", "1"] if strip_tags_first else []) + \
          ["-i", src, "-f", "f32le", "-ac", "1", "-"]
    p = run(cmd)
    if p.returncode:
        raise RuntimeError("ffmpeg decode failed")
    return np.frombuffer(p.stdout, dtype=np.float32)


def xcorr_lag(wav_path, m4a_path):
    """Lag (samples) of the untrimmed m4a decode relative to the wav; expect == priming."""
    import numpy as np
    with tempfile.TemporaryDirectory() as td:
        ref = raw_decode_mono(wav_path, False, td)
        raw = raw_decode_mono(m4a_path, True, td)
    win = min(10 * 44100, len(ref) // 3)
    best = None
    for frac in (0.25, 0.5, 0.75):                      # pick the loudest window
        a = int(len(ref) * frac)
        a = max(4000, min(a, len(ref) - win - 8000))
        rms = float(np.sqrt(np.mean(ref[a:a + win] ** 2)))
        if best is None or rms > best[0]:
            best = (rms, a)
    a = best[1]
    seg = raw[a - 4000: a + win + 8000]
    ref_w = ref[a:a + win]
    n = 1 << (len(seg) + len(ref_w)).bit_length()
    c = np.fft.irfft(np.fft.rfft(seg, n) * np.conj(np.fft.rfft(ref_w, n)), n)
    lags = c[: len(seg) - len(ref_w) + 1]
    return int(np.argmax(lags)) - 4000


def verify_m4a(wav, m4a_path, xcorr, wav_path):
    info = m4a_info(m4a_path)
    if info["codec"] != "aac":
        return False, f"codec is {info['codec']}", info
    if info["sr"] != wav["sr"] or info["channels"] != wav["channels"]:
        return False, f"rate/channels changed ({info['sr']}/{info['channels']} vs {wav['sr']}/{wav['channels']})", info
    smpb = info["smpb"]
    if not smpb:
        return False, "no iTunSMPB tag", info
    if smpb["priming"] != EXPECTED_PRIMING:
        return False, f"priming {smpb['priming']} != {EXPECTED_PRIMING}", info
    if smpb["valid"] != wav["frames"]:
        return False, f"valid length {smpb['valid']} != wav frames {wav['frames']}", info
    if xcorr:
        lag = xcorr_lag(wav_path, m4a_path)
        info["xcorr_lag"] = lag
        if lag != smpb["priming"]:
            return False, f"xcorr lag {lag} != priming {smpb['priming']}", info
    return True, "ok", info


# ---------------------------------------------------------------------------------------
# DB / manifest helpers
# ---------------------------------------------------------------------------------------
def open_db(path, key):
    kw = {"path": str(path)}
    if key:
        kw["key"] = key
    return RBDatabase(**kw)


def share_dir_of(db, args):
    if args.share_dir:
        return Path(args.share_dir)
    sd = getattr(db, "_share_dir", None)
    return Path(sd) if sd else Path(args.db).parent / "share"


def load_manifest(work):
    p = work / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_manifest(work, man):
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / "manifest.json.tmp"
    with LOCK:
        tmp.write_text(json.dumps(man, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, work / "manifest.json")


def select_rows(db, args):
    rows = db.query(DjmdContent).filter(DjmdContent.FileType == FT_WAV).all()
    out = []
    for r in rows:
        if r.rb_local_deleted or not r.FolderPath or not r.FolderPath.lower().endswith(".wav"):
            continue
        if args.ids and str(r.ID) not in args.ids:
            continue
        if args.match and args.match.lower() not in r.FolderPath.lower():
            continue
        out.append(r)
    out.sort(key=lambda r: str(r.ID))
    return out[: args.limit] if args.limit else out


def make_entry(r, args, used_paths):
    old = r.FolderPath
    new = swap_ext(old)
    e = {"id": str(r.ID), "old_path": old, "new_path": new,
         "src": str(local_path(old, args.path_map)), "dst": str(local_path(new, args.path_map)),
         "status": "planned", "reason": ""}
    if new.lower() in used_paths:
        e.update(status="skip", reason="new m4a path already used by another DB row")
    elif not Path(e["src"]).is_file():
        e.update(status="skip", reason="wav file missing on disk")
    return e


def anlz_files(share, content):
    """Existing ANLZ files {ext: Path} for a content row, resolved against the share dir."""
    if not content.AnalysisDataPath:
        return {}
    rel = Path(content.AnalysisDataPath.replace("\\", "/").strip("/"))
    d = share / rel.parent
    out = {}
    for ext in ANLZ_EXTS:
        for p in d.glob(f"ANLZ*.{ext}"):
            out[ext] = p
    return out


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
    off = struct.unpack_from(">I", data, 4)[0]
    while off + 12 <= len(data):
        typ = bytes(data[off:off + 4]).decode("ascii", "replace")
        lh, lt = struct.unpack_from(">II", data, off + 4)
        if lt < 12:
            break
        yield typ, off, lh, lt
        off += lt


def cue_entries(data, typ, off, lh):
    """Yield (time_offset, loop_offset) absolute offsets of cue entries in a PCOB/PCO2 tag."""
    if typ == "PCOB":
        n, toff, loff = struct.unpack_from(">H", data, off + 18)[0], 32, 36
    else:
        n, toff, loff = struct.unpack_from(">H", data, off + 16)[0], 20, 24
    pos = off + lh
    for _ in range(n):
        yield pos + toff, pos + loff
        pos += struct.unpack_from(">I", data, pos + 8)[0]


def grid_info(data):
    """First downbeat etc. from the (ORIGINAL) DAT PQTZ. Times in ms."""
    for typ, off, lh, lt in iter_tags(data):
        if typ != "PQTZ":
            continue
        n = struct.unpack_from(">I", data, off + 20)[0]
        ent = [struct.unpack_from(">HHI", data, off + 24 + 8 * i) for i in range(n)]
        idx = next((i for i, e in enumerate(ent) if e[0] == 1), None)
        if idx is None:
            return None
        bpm = ent[idx][1] / 100.0
        return {"first_beat_ms": ent[0][2], "first_downbeat_ms": ent[idx][2], "bpm": bpm,
                "bar_ms": 4 * 60000.0 / bpm if bpm else None}
    return None


def time_signature(data):
    """All beat/cue times in a file, for before/after comparison."""
    sig = []
    for typ, off, lh, lt in iter_tags(data):
        if typ == "PQTZ":
            n = struct.unpack_from(">I", data, off + 20)[0]
            sig += [struct.unpack_from(">I", data, off + 24 + 8 * i + 4)[0] for i in range(n)]
        elif typ in ("PCOB", "PCO2"):
            sig += [struct.unpack_from(">I", data, t)[0] for t, _ in cue_entries(data, typ, off, lh)]
    return sig


def shift_anlz(data, shift, new_ext="m4a"):
    """Return (patched bytes, stats). Length never changes (wav -> m4a are both 3 chars)."""
    d = bytearray(data)
    st = Counter()
    for typ, off, lh, lt in iter_tags(data):
        if typ == "PQTZ":
            n = struct.unpack_from(">I", d, off + 20)[0]
            for i in range(n):
                p = off + 24 + 8 * i + 4
                struct.pack_into(">I", d, p, struct.unpack_from(">I", d, p)[0] + shift)
                st["beats"] += 1
        elif typ in ("PCOB", "PCO2"):
            for t, lo in cue_entries(d, typ, off, lh):
                tm = struct.unpack_from(">I", d, t)[0]
                lp = struct.unpack_from(">I", d, lo)[0]
                if lp != 0xFFFFFFFF and lp >= tm:
                    struct.pack_into(">I", d, lo, lp + shift)
                struct.pack_into(">I", d, t, tm + shift)
                st["cues"] += 1
        elif typ == "PQT2":       # format not verified: shift + flag for manual review
            if struct.unpack_from(">I", d, off + 40)[0] > 0:
                for p in (off + 28, off + 36):
                    v = struct.unpack_from(">I", d, p)[0]
                    if v:
                        struct.pack_into(">I", d, p, v + shift)
                st["pqt2_review"] += 1
        elif typ == "PPTH":
            n = struct.unpack_from(">I", d, off + 12)[0]
            end = off + 16 + n - 2                      # path without the 2-byte terminator
            tail = bytes(d[end - 6:end]).decode("utf-16-be", "replace")
            if tail.lower() == "wav":
                d[end - 6:end] = new_ext.encode("utf-16-be")
                st["ppth"] += 1
    return bytes(d), st


# ---------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------
def cmd_dry_run(args):
    db = open_db(args.db, args.key)
    share = share_dir_of(db, args)
    rows = select_rows(db, args)
    used = {str(r.FolderPath).lower() for r in db.query(DjmdContent).all()
            if r.FolderPath and not r.rb_local_deleted}
    lines, cnt = [], Counter()
    for r in rows:
        e = make_entry(r, args, used)
        if e["status"] == "skip":
            cnt["skip"] += 1
            lines.append(f"[skip] {e['old_path']}  ({e['reason']})")
            continue
        w = wav_info(e["src"])
        shift = round(EXPECTED_PRIMING * 1000 / w["sr"])
        ncue = db.query(DjmdCue).filter(DjmdCue.ContentID == str(r.ID)).count()
        files = anlz_files(share, r)
        flags = []
        if "DAT" not in files:
            flags.append("NO-ANLZ-DAT")
        else:
            g = grid_info(files["DAT"].read_bytes())
            if g is None:
                flags.append("NO-GRID")
            else:
                if g["first_downbeat_ms"] <= args.first_tol_ms:
                    flags.append("GRID-ON-FIRST-SAMPLE")
                if g["bar_ms"] and abs(g["first_downbeat_ms"] - g["bar_ms"]) <= args.bar_tol_ms:
                    flags.append("DOWNBEAT-ONE-BAR")
        cnt["ok"] += 1
        lines.append(f"[ok]   {e['old_path']} -> .m4a  sr={w['sr']} shift=+{shift}ms cues={ncue} "
                     f"anlz={'/'.join(files) or '-'} {' '.join(flags)}")
    log("\n".join(lines))
    log(f"\n{cnt['ok']} would be converted, {cnt['skip']} skipped.")
    (args.work).mkdir(parents=True, exist_ok=True)
    (args.work / "dry_run.txt").write_text("\n".join(lines), encoding="utf-8")


def encode_one(e, args):
    src, dst = Path(e["src"]), Path(e["dst"])
    tmp = dst.with_name(dst.stem + ".__tmp__.m4a")
    if tmp.exists():
        tmp.unlink()
    if dst.exists() and not e.get("ours") and not args.adopt_existing:
        return "skip", "target m4a already exists (use --adopt-existing to verify and use it)"
    w = wav_info(src)
    if dst.exists() and (e.get("ours") or args.adopt_existing):
        ok, msg, info = verify_m4a(w, dst, 0, src)       # reuse previous encode
        if ok:
            return "encoded", {"w": w, "info": info}
        return "failed", f"existing m4a does not verify: {msg}"
    p = run([args.qaac, "--tvbr", "109", str(src), "-o", str(tmp)], text=True, errors="replace")
    if p.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return "failed", f"qaac exit {p.returncode}: {p.stderr.strip()[-200:]}"
    ok, msg, info = verify_m4a(w, tmp, args._xcorr_left(), src)
    if not ok:
        tmp.unlink(missing_ok=True)
        return "failed", msg
    os.replace(tmp, dst)
    info["size"] = os.path.getsize(dst)
    return "encoded", {"w": w, "info": info}


def cmd_encode(args):
    db = open_db(args.db, args.key)
    rows = select_rows(db, args)
    used = {str(r.FolderPath).lower() for r in db.query(DjmdContent).all()
            if r.FolderPath and not r.rb_local_deleted}
    man = load_manifest(args.work)
    todo = []
    for r in rows:
        cur = man.get(str(r.ID))
        if cur and cur["status"] in ("encoded", "applied", "installed"):
            continue
        e = make_entry(r, args, used)
        if cur and cur.get("ours"):
            e["ours"] = True
        man[e["id"]] = e
        if e["status"] == "planned":
            todo.append(e)
        else:
            log(f"[skip] {e['old_path']} ({e['reason']})")
    save_manifest(args.work, man)
    log(f"{len(todo)} wavs to encode")
    left = [args.xcorr]
    lock = threading.Lock()

    def xleft():
        with lock:
            if left[0] > 0:
                left[0] -= 1
                return 1
            return 0
    args._xcorr_left = xleft

    def work(e):
        e["ours"] = True
        status, res = encode_one(e, args)
        with LOCK:
            if status == "encoded":
                w, info = res["w"], res["info"]
                smpb = info["smpb"]
                dur = smpb["valid"] / w["sr"]
                e.update(status="encoded", sr=w["sr"], priming=smpb["priming"], valid=smpb["valid"],
                         shift_ms=round(smpb["priming"] * 1000 / w["sr"]), size=info["size"],
                         bitrate_kbps=round(info["size"] * 8 / 1000 / dur), reason="",
                         xcorr_lag=info.get("xcorr_lag"))
            else:
                e.update(status=status, reason=res, ours=e.get("ours") and status != "failed")
        save_manifest(args.work, man)
        log(f"[{e['status']}] {e['old_path']} {e.get('reason', '')}"
            f"{' shift=+%dms' % e['shift_ms'] if e['status'] == 'encoded' else ''}")

    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        list(ex.map(work, todo))
    c = Counter(e["status"] for e in man.values())
    log(f"done: {dict(c)}")


def cmd_apply(args):
    man = load_manifest(args.work)
    todo = [e for e in man.values() if e["status"] in ("encoded", "applied")]
    if not todo:
        log("nothing to apply (run encode first)")
        return
    work_db = args.work / "master.db"
    stage = args.work / "share"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copy2(args.db, work_db)                       # always restart from the live DB
    db = open_db(work_db, args.key)
    share = share_dir_of(open_db(args.db, args.key), args)
    rep_first, rep_bar, problems = [], [], []
    done = 0
    for e in todo:
        cid, shift = e["id"], int(e["shift_ms"])
        c = db.query(DjmdContent).filter(DjmdContent.ID == cid).one_or_none()
        if c is None or c.FolderPath != e["old_path"]:
            problems.append(f"{e['old_path']}: DB row changed or gone, skipped")
            continue
        # --- ANLZ (read the originals, stage the edited copies)
        files = anlz_files(share, c)
        info = None
        staged = []
        try:
            for ext, p in files.items():
                raw = p.read_bytes()
                before = time_signature(raw)
                if ext == "DAT":
                    info = grid_info(raw)
                patched, st = shift_anlz(raw, shift)
                if st["pqt2_review"]:
                    problems.append(f"{e['new_path']}: populated PQT2 in {ext}, check grid manually")
                if len(patched) != len(raw):
                    raise RuntimeError("length changed")
                out = stage / p.relative_to(share)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(patched)
                after = time_signature(out.read_bytes())
                if after != [t + shift for t in before]:
                    raise RuntimeError(f"staged {ext} failed self-check")
                staged.append(out)
        except Exception as ex:
            for s in staged:
                s.unlink(missing_ok=True)
            problems.append(f"{e['old_path']}: ANLZ error ({ex}), track skipped entirely")
            continue
        if "DAT" not in files:
            problems.append(f"{e['new_path']}: no ANLZ DAT found, DB cues shifted but beatgrid not")
        elif info is None:
            problems.append(f"{e['new_path']}: no beat grid in DAT")
        else:
            desc = f"{e['new_path']} | bpm {info['bpm']:.2f} | first downbeat {info['first_downbeat_ms']} ms"
            if info["first_downbeat_ms"] <= args.first_tol_ms:
                rep_first.append(desc)
            if info["bar_ms"] and abs(info["first_downbeat_ms"] - info["bar_ms"]) <= args.bar_tol_ms:
                rep_bar.append(desc + f" | 1 bar = {info['bar_ms']:.1f} ms "
                                      f"(delta {info['first_downbeat_ms'] - info['bar_ms']:+.1f} ms)")
        # --- DB: content row
        c.FolderPath = e["new_path"]
        c.FileNameL = e["new_path"].rsplit("/", 1)[-1]
        c.FileType = FT_M4A
        c.FileSize = e["size"]
        c.BitRate = e["bitrate_kbps"]
        c.BitDepth = 16
        for fld in ("OrgFolderPath", "rb_LocalFolderPath"):
            if getattr(c, fld, None) == e["old_path"]:
                setattr(c, fld, e["new_path"])
        # --- DB: cues
        snap = {}
        for q in db.query(DjmdCue).filter(DjmdCue.ContentID == cid).all():
            snap[str(q.ID)] = [q.InMsec, q.OutMsec]
            if q.InMsec is not None:
                q.InMsec += shift
                q.InFrame = frame_of(q.InMsec)
            if q.OutMsec is not None and q.OutMsec >= 0:
                q.OutMsec += shift
                q.OutFrame = frame_of(q.OutMsec)
        for cc in db.query(ContentCue).filter(ContentCue.ContentID == cid).all():
            try:
                data = json.loads(cc.Cues)
            except Exception:
                problems.append(f"{e['new_path']}: ContentCue JSON unreadable, left unchanged")
                continue
            for d in data:
                if d.get("InMsec") is not None:
                    d["InMsec"] += shift
                    d["InFrame"] = frame_of(d["InMsec"])
                if d.get("OutMsec") is not None and d["OutMsec"] >= 0:
                    d["OutMsec"] += shift
                    d["OutFrame"] = frame_of(d["OutMsec"])
            cc.Cues = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        e["cue_snapshot"] = snap
        e["anlz_staged"] = [str(s.relative_to(stage)) for s in staged]
        e["status"] = "applied"
        done += 1
    db.commit()
    db.close()
    save_manifest(args.work, man)
    # --- post-commit check of the staged DB
    chk = open_db(work_db, args.key)
    bad = 0
    for e in man.values():
        if e["status"] != "applied":
            continue
        c = chk.query(DjmdContent).filter(DjmdContent.ID == e["id"]).one()
        ok = c.FolderPath == e["new_path"] and c.FileType == FT_M4A
        for q in chk.query(DjmdCue).filter(DjmdCue.ContentID == e["id"]).all():
            i, o = e["cue_snapshot"][str(q.ID)]
            ok &= q.InMsec == (None if i is None else i + e["shift_ms"])
            ok &= q.OutMsec == (o if o is None or o < 0 else o + e["shift_ms"])
        if not ok:
            bad += 1
            problems.append(f"{e['new_path']}: staged DB self-check FAILED")
    chk.close()
    rd = args.work / "reports"
    rd.mkdir(exist_ok=True)
    (rd / "beatgrid_first_sample.txt").write_text(
        "# wavs (now m4a) whose first DOWNBEAT was on the very first sample (<= %d ms) in the old grid\n" % args.first_tol_ms
        + "\n".join(rep_first) + "\n", encoding="utf-8")
    (rd / "beatgrid_one_bar_offset.txt").write_text(
        "# wavs (now m4a) whose first downbeat was within %d ms of exactly one bar from the start\n" % args.bar_tol_ms
        + "\n".join(rep_bar) + "\n", encoding="utf-8")
    (rd / "problems.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
    log(f"applied {done} tracks ({bad} self-check failures, {len(problems)} problems).")
    log(f"staged DB: {work_db}\nstaged ANLZ: {stage}\nreports: {rd}")
    log(f"first-sample list: {len(rep_first)}, one-bar list: {len(rep_bar)}")


def rekordbox_running():
    if os.name != "nt":
        return False
    p = run(["tasklist", "/FI", "IMAGENAME eq rekordbox.exe"], text=True, errors="replace")
    return "rekordbox.exe" in (p.stdout or "").lower()


def cmd_install(args):
    if not args.yes:
        log("refusing without --yes (this overwrites your live master.db and ANLZ files)")
        return
    if rekordbox_running():
        log("Rekordbox is running, close it first")
        return
    man = load_manifest(args.work)
    todo = [e for e in man.values() if e["status"] == "applied"]
    if not todo:
        log("nothing applied")
        return
    db_probe = open_db(args.db, args.key)
    share = share_dir_of(db_probe, args)
    db_probe.close()
    bdir = args.work / ("backup_" + time.strftime("%Y%m%d_%H%M%S"))
    bdir.mkdir(parents=True)
    shutil.copy2(args.db, bdir / "master.db")
    stage = args.work / "share"
    n = 0
    for e in todo:
        for rel in e["anlz_staged"]:
            live = share / rel
            if live.exists():
                (bdir / "share" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, bdir / "share" / rel)
            shutil.copy2(stage / rel, live)
            n += 1
    shutil.copy2(args.work / "master.db", args.db)
    for e in todo:
        e["status"] = "installed"
    save_manifest(args.work, man)
    log(f"installed {len(todo)} tracks, {n} ANLZ files. backup: {bdir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to the live master.db")
    ap.add_argument("--key", default="", help="SQLCipher key (default: pyrekordbox auto-detects)")
    ap.add_argument("--share-dir", help="Rekordbox share dir (default: next to master.db)")
    ap.add_argument("--qaac", default="qaac64.exe")
    ap.add_argument("--work", type=Path, default=Path("work"))
    ap.add_argument("--path-map", action="append", default=[], metavar="OLD=NEW",
                    help="map DB path prefix to a local one (repeatable)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="", help="comma separated djmdContent IDs")
    ap.add_argument("--match", default="")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--xcorr", type=int, default=3, help="cross-correlate the first N encodes (0=off)")
    ap.add_argument("--adopt-existing", action="store_true")
    ap.add_argument("--first-tol-ms", type=int, default=0)
    ap.add_argument("--bar-tol-ms", type=int, default=15)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("cmd", choices=["dry-run", "encode", "apply", "install"])
    args = ap.parse_args()
    args.ids = [x for x in args.ids.split(",") if x]
    args.path_map = [tuple(x.split("=", 1)) for x in args.path_map]
    args.work.mkdir(parents=True, exist_ok=True)
    {"dry-run": cmd_dry_run, "encode": cmd_encode, "apply": cmd_apply, "install": cmd_install}[args.cmd](args)


if __name__ == "__main__":
    main()