#!/usr/bin/env python3
"""Interactive wrapper around rb_wav_to_m4a.py. Reads settings from .env next to this file.
Run:  python run_migration.py
Enter = continue to the next step, anything else (or Ctrl+C) = stop. `install` needs you to type yes."""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "rb_wav_to_m4a.py"


def load_env(path):
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def ask(msg):
    return input(f"\n>>> {msg} ").strip()


def step_gate(msg):
    if ask(f"{msg} [Enter = go, anything else = stop]") != "":
        print("stopped.")
        sys.exit(0)


def main():
    env = HERE / ".env"
    if not env.exists():
        sys.exit(f".env not found at {env}")
    c = load_env(env)
    for k in ("RB_DB", "RB_KEY", "QAAC"):
        if not c.get(k):
            sys.exit(f"{k} missing in .env")
    work = c.get("WORK_DIR", "work")
    base = [sys.executable, str(SCRIPT), "--db", c["RB_DB"], "--key", c["RB_KEY"], "--qaac", c["QAAC"],
            "--work", work, "--xcorr", c.get("XCORR", "3"), "--tvbr", c.get("QUALITY", "109"), "--jobs", c.get("JOBS", "1")]
    if c.get("BATCH_SIZE", "0") not in ("", "0"):
        base += ["--limit", c["BATCH_SIZE"]]
    if c.get("MATCH"):
        base += ["--match", c["MATCH"]]

    def run(*extra):
        return subprocess.run(base + list(extra)).returncode

    print("Rekordbox wav -> m4a migration")
    print(f"  master.db : {c['RB_DB']}\n  work dir  : {work}\n  batch size: {c.get('BATCH_SIZE', '0')}\n  AAC quality: TVBR {c.get('QUALITY', '109')}")
    if ask("Is Rekordbox CLOSED and master.db backed up? Type yes:").lower() != "yes":
        sys.exit("aborted.")

    while True:
        print("\n=== 1/4 DRY RUN (no changes) ===")
        if run("dry-run") != 0:
            sys.exit("dry-run failed")
        step_gate("Looks right? Encode these now?")

        print("\n=== 2/4 ENCODE (writes .m4a next to the wavs, wavs stay) ===")
        if run("encode") != 0:
            sys.exit("encode failed")
        step_gate("Encoding done. Apply to a COPY of master.db + staged ANLZ files?")

        print("\n=== 3/4 APPLY (still only on copies in the work dir) ===")
        if run("apply") != 0:
            sys.exit("apply failed")
        rep = Path(work) / "reports"
        for name in ("problems.txt", "beatgrid_first_sample.txt", "beatgrid_one_bar_offset.txt"):
            f = rep / name
            if f.exists():
                print(f"\n--- {name} ---\n{f.read_text(encoding='utf-8').strip() or '(empty)'}")

        print("\n=== 4/4 INSTALL (overwrites live master.db + ANLZ files, backup is made first) ===")
        if ask("Make sure Rekordbox is closed. Type yes to install:").lower() != "yes":
            print("not installed. Staged files stay in the work dir.")
            sys.exit(0)
        if run("--yes", "install") != 0:
            sys.exit("install failed")
        print("\nInstalled. Open Rekordbox and check a few converted tracks (grid + cues, start AND end).")
        if ask("Run another batch? [y = yes, Enter = quit]").lower() != "y":
            break
        ask("Close Rekordbox again if you opened it, then press Enter.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted.")