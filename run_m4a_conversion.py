#!/usr/bin/env python3
"""Interactive wrapper around convert_library_to_m4a.py. Reads settings from .env next to this file.
Run:  python run_m4a_conversion.py
Enter = continue to the next step, anything else (or Ctrl+C) = stop. `install` needs you to type yes."""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "convert_library_to_m4a.py"


def load_env(path):
    configuration = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        configuration[key.strip()] = value.strip().strip('"').strip("'")
    return configuration


def ask(message):
    return input(f"\n>>> {message} ").strip()


def step_gate(message):
    if ask(f"{message} [Enter = go, anything else = stop]") != "":
        print("stopped.")
        sys.exit(0)


def main():
    env_path = HERE / ".env"
    if not env_path.exists():
        sys.exit(f".env not found at {env_path}")
    configuration = load_env(env_path)
    for env_key in ("REKORDBOX_DATABASE_PATH", "REKORDBOX_ENCRYPTION_KEY", "QAAC_PATH"):
        if not configuration.get(env_key):
            sys.exit(f"{env_key} missing in .env")
    work = configuration.get("WORK_DIRECTORY", "work")
    base = [sys.executable, str(SCRIPT), "--database-path", configuration["REKORDBOX_DATABASE_PATH"], "--encryption-key", configuration["REKORDBOX_ENCRYPTION_KEY"], "--qaac-path", configuration["QAAC_PATH"],
            "--work-directory", work, "--cross-correlation-checks", configuration.get("CROSS_CORRELATION_CHECKS", "3"), "--true-vbr-quality", configuration.get("AAC_TRUE_VBR_QUALITY", "109"), "--parallel-workers", configuration.get("PARALLEL_WORKERS", "1")]
    if configuration.get("BATCH_SIZE", "0") not in ("", "0"):
        base += ["--limit", configuration["BATCH_SIZE"]]
    if configuration.get("PATH_MATCH_TEXT"):
        base += ["--match", configuration["PATH_MATCH_TEXT"]]

    def run(*extra):
        return subprocess.run(base + list(extra)).returncode

    print("Rekordbox wav -> m4a migration")
    print(f"  master.db : {configuration['REKORDBOX_DATABASE_PATH']}\n  work dir  : {work}\n  batch size: {configuration.get('BATCH_SIZE', '0')}\n  AAC quality: TVBR {configuration.get('AAC_TRUE_VBR_QUALITY', '109')}")
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
        reports_directory = Path(work) / "reports"
        for name in ("problems.txt", "beatgrid_first_sample.txt", "beatgrid_one_bar_offset.txt"):
            report_path = reports_directory / name
            if report_path.exists():
                print(f"\n--- {name} ---\n{report_path.read_text(encoding='utf-8').strip() or '(empty)'}")

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