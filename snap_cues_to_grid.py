#!/usr/bin/env python3
"""
snap_cues_to_grid.py - snap memory cues, hot cues and loop ends to the beat grid, whole collection.

For every non-deleted track (wav, m4a, mp3, ...): read the beat times from the track's ANLZ .DAT
(PQTZ), and move every cue whose nearest beat is within SNAP_MS (default 10) onto that beat.
Cues farther away stay untouched (unquantized vocal pickups etc).
Edited in place: djmdCue (InMsec/InFrame/OutMsec/OutFrame), ContentCue JSON, and the cue entries
in the ANLZ files (PCOB/PCO2). Settings come from .env (RB_DB, RB_KEY, SNAP_MS). NO BACKUP is made.
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
import rb_wav_to_m4a as rb  # noqa: E402
from rb_wav_to_m4a import ContentCue, DjmdContent, DjmdCue  # noqa: E402


def load_env(path):
    cfg = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def beats_from_dat(data):
    for typ, off, lh, lt in rb.iter_tags(data):
        if typ == "PQTZ":
            n = struct.unpack_from(">I", data, off + 20)[0]
            return sorted(struct.unpack_from(">I", data, off + 24 + 8 * i + 4)[0] for i in range(n))
    return []


def snap(t, beats, margin):
    i = bisect.bisect_left(beats, t)
    cands = ([beats[i]] if i < len(beats) else []) + ([beats[i - 1]] if i > 0 else [])
    if not cands:
        return t
    best = min(cands, key=lambda b: (abs(b - t), b))
    return best if abs(best - t) <= margin else t


def snap_anlz(data, beats, margin):
    """Patch cue/loop times in raw ANLZ bytes. Returns (bytes, [(old, new)])."""
    d = bytearray(data)
    ch = []
    for typ, off, lh, lt in rb.iter_tags(data):
        if typ not in ("PCOB", "PCO2"):
            continue
        for t, lo in rb.cue_entries(d, typ, off, lh):
            tm = struct.unpack_from(">I", d, t)[0]
            lp = struct.unpack_from(">I", d, lo)[0]
            if lp != 0xFFFFFFFF and lp >= tm:
                nl = snap(lp, beats, margin)
                if nl != lp:
                    struct.pack_into(">I", d, lo, nl)
                    ch.append((lp, nl))
            nt = snap(tm, beats, margin)
            if nt != tm:
                struct.pack_into(">I", d, t, nt)
                ch.append((tm, nt))
    return bytes(d), ch


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--margin", type=int)
    ap.add_argument("--db")
    ap.add_argument("--key")
    ap.add_argument("--share-dir")
    a = ap.parse_args()
    env = load_env(HERE / ".env")
    db_path = a.db or env.get("RB_DB")
    key = a.key or env.get("RB_KEY", "")
    margin = a.margin if a.margin is not None else int(env.get("SNAP_MS", "10"))
    if not db_path:
        sys.exit("RB_DB missing (.env or --db)")
    if not a.dry_run and rb.rekordbox_running():
        sys.exit("Rekordbox is running, close it first")

    db = rb.open_db(db_path, key)
    share = rb.share_dir_of(db, SimpleNamespace(share_dir=a.share_dir, db=db_path))
    rows = [r for r in db.query(DjmdContent).all() if not r.rb_local_deleted]
    st = Counter()
    report = []
    pending = {}                                    # ANLZ path -> patched bytes
    t0 = time.time()
    for n, r in enumerate(rows, 1):
        files = rb.anlz_files(share, r)
        if "DAT" not in files:
            st["tracks without ANLZ"] += 1
            continue
        beats = beats_from_dat(files["DAT"].read_bytes())
        if not beats:
            st["tracks without grid"] += 1
            continue
        st["tracks"] += 1
        label = r.FolderPath or str(r.ID)
        cid = str(r.ID)

        def fix(v):
            nv = snap(v, beats, margin)
            if nv == v:
                st["cue values left (off grid or already on it)"] += 1
            else:
                st["cue values snapped"] += 1
                report.append(f"{label} | {v} -> {nv} ({nv - v:+d} ms)")
            return nv

        for q in db.query(DjmdCue).filter(DjmdCue.ContentID == cid).all():
            if q.InMsec is not None and q.InMsec >= 0:
                nv = fix(q.InMsec)
                if nv != q.InMsec:
                    q.InMsec, q.InFrame = nv, rb.frame_of(nv)
            if q.OutMsec is not None and q.OutMsec >= 0:
                nv = fix(q.OutMsec)
                if nv != q.OutMsec:
                    q.OutMsec, q.OutFrame = nv, rb.frame_of(nv)
        for cc in db.query(ContentCue).filter(ContentCue.ContentID == cid).all():
            try:
                data = json.loads(cc.Cues)
            except Exception:
                continue
            changed = False
            for d in data:
                for f, fr in (("InMsec", "InFrame"), ("OutMsec", "OutFrame")):
                    v = d.get(f)
                    if v is not None and v >= 0:
                        nv = snap(v, beats, margin)
                        if nv != v:
                            d[f], d[fr], changed = nv, rb.frame_of(nv), True
            if changed:
                cc.Cues = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        for ext, p in files.items():
            if ext == "2EX":
                continue
            raw = p.read_bytes()
            patched, ch = snap_anlz(raw, beats, margin)
            if ch:
                pending[p] = patched
        if n % 200 == 0:
            print(f"  {n}/{len(rows)} tracks scanned...", flush=True)

    if not a.dry_run:
        db.commit()
        for p, data in pending.items():
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
    db.close()
    out = HERE / f"snap_report_{time.strftime('%Y%m%d_%H%M%S')}{'_dryrun' if a.dry_run else ''}.txt"
    out.write_text(f"# margin {margin} ms, {'DRY RUN' if a.dry_run else 'applied'}\n" + "\n".join(report) + "\n",
                   encoding="utf-8")
    print(f"\n{'DRY RUN - nothing written' if a.dry_run else 'Done'} in {time.time() - t0:.0f}s, margin {margin} ms")
    for k, v in st.items():
        print(f"  {k}: {v}")
    print(f"  ANLZ files {'that would be ' if a.dry_run else ''}rewritten: {len(pending)}")
    print(f"  details: {out}")


if __name__ == "__main__":
    main()