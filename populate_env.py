#!/usr/bin/env python3
"""
populate_env.py - find the paths/key the migration scripts need and write them into .env.

Detects:
  REKORDBOX_DATABASE_PATH   path of the live master.db   (pyrekordbox config, then well-known locations)
  REKORDBOX_ENCRYPTION_KEY  SQLCipher key                (existing .env value if it works, else captured from
                                        pyrekordbox's own DEBUG log / its built-in blob)
  QAAC_PATH    path of qaac64.exe           (PATH, next to this script, common folders, shallow search)
Also checks ffmpeg / ffprobe on PATH and the share folder next to master.db.

.env handling: values you already set and that still work are KEPT. Empty/missing/broken ones
are filled. Comments and unknown lines are preserved. Missing optional settings get the defaults.
If no .env exists, `_env` (the template) next to this script is used as the starting point.
Every path that could not be found is listed at the end so you know what to fill in by hand.

Run:  python populate_env.py            (add --force to overwrite working values too,
                                              --no-search to skip the disk search for qaac)
"""
import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
TEMPLATE_PATH = HERE / "_env"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---- pyrekordbox imports (same layout fallbacks as convert_library_to_m4a.py) --------------------
try:
    from pyrekordbox.masterdb import MasterDatabase as RekordboxDatabase
except ImportError:
    try:
        from pyrekordbox import Rekordbox6Database as RekordboxDatabase
    except ImportError:
        RekordboxDatabase = None

DEFAULTS = [
    ("WORK_DIRECTORY", "work", "# Settings for run_m4a_conversion.py (no quotes needed, backslashes are fine)"),
    ("BATCH_SIZE", "10", "# Tracks per batch. 0 = all remaining wavs. Converted tracks drop out of the next batch automatically."),
    ("AAC_TRUE_VBR_QUALITY", "109", "# AAC quality for qaac --tvbr, 0 (lowest) to 127 (highest). 109 = what you tested."),
    ("CROSS_CORRELATION_CHECKS", "3", "# Cross-correlate the first N encodes of each batch against the wav (0 = off)"),
    ("PARALLEL_WORKERS", "1", "# Parallel qaac jobs"),
    ("PATH_MATCH_TEXT", "", "# Optional: only tracks whose path contains this text, e.g. an artist or folder name"),
    ("CUE_SNAP_MARGIN_MS", "10", "# --- snap_cues_to_grid.py ---\n# Cues/hot cues/loop ends closer than this (ms) to a beat get moved onto it; farther ones stay as they are"),
]
REQUIRED = ("REKORDBOX_DATABASE_PATH", "REKORDBOX_ENCRYPTION_KEY", "QAAC_PATH")


# ---------------------------------------------------------------------------------------
# .env read / write
# ---------------------------------------------------------------------------------------
def parse_env(text):
    configuration = {}
    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line and not stripped_line.startswith("#") and "=" in stripped_line:
            key, value = stripped_line.split("=", 1)
            configuration[key.strip()] = value.strip().strip('"').strip("'")
    return configuration


def set_value(lines, key, value):
    """Replace KEY=... in place, or append. Returns the new list of lines."""
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


# ---------------------------------------------------------------------------------------
# REKORDBOX_DATABASE_PATH
# ---------------------------------------------------------------------------------------
def pyrekordbox_database_path():
    try:
        from pyrekordbox.config import get_config
    except ImportError:
        return None, "pyrekordbox not installed"
    for section in ("rekordbox7", "rekordbox6"):      # same preference order pyrekordbox uses
        try:
            path = get_config(section).get("db_path")
        except Exception:
            path = None
        if path and Path(path).exists():
            return Path(path), f"pyrekordbox config ({section})"
    return None, "pyrekordbox found no Rekordbox 6/7 install"


def known_database_locations():
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates += [Path(appdata) / "Pioneer" / directory_name / "master.db" for directory_name in ("rekordbox", "rekordbox6")]
    home = Path.home()
    candidates += [home / "Library" / "Pioneer" / directory_name / "master.db" for directory_name in ("rekordbox", "rekordbox6")]
    candidates += [home / "AppData" / "Roaming" / "Pioneer" / directory_name / "master.db" for directory_name in ("rekordbox", "rekordbox6")]
    return candidates


def find_database(existing, force):
    if existing and not force and Path(existing).is_file():
        return Path(existing), "kept from .env"
    path, how = pyrekordbox_database_path()
    if path:
        return path, how
    for candidate in known_database_locations():
        if candidate.is_file():
            return candidate, "well-known location"
    return None, how


# ---------------------------------------------------------------------------------------
# REKORDBOX_ENCRYPTION_KEY
# ---------------------------------------------------------------------------------------
class _KeyCatcher(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.key = None

    def emit(self, record):
        match = re.match(r"^Key:\s*(\S+)", record.getMessage())
        if match:
            self.key = match.group(1)


def try_open(database_path, key=""):
    """Open the DB the way the other scripts do. Returns (ok, error)."""
    if RekordboxDatabase is None:
        return False, "pyrekordbox not installed"
    keyword_arguments = {"path": str(database_path)}
    if key:
        keyword_arguments["key"] = key
    try:
        database = RekordboxDatabase(**keyword_arguments)
    except Exception as error:                                  # wrong key, missing sqlcipher, ...
        return False, f"{type(error).__name__}: {error}"
    try:
        database.close()
    except Exception:
        pass
    return True, ""


def capture_key(database_path):
    """Open without a key and read the key pyrekordbox logs at DEBUG level."""
    logger = logging.getLogger("pyrekordbox")
    old_level = logger.level
    handler = _KeyCatcher()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        ok, error_text = try_open(database_path)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    return (handler.key if ok else None), error_text, handler.key


def builtin_key():
    """Fallback: pyrekordbox's own obfuscated key blob (pyrekordbox >= 0.4/0.5 layout)."""
    try:
        from pyrekordbox.utils import deobfuscate
        try:
            from pyrekordbox.masterdb.database import BLOB
        except ImportError:
            from pyrekordbox.db6.database import BLOB
        return deobfuscate(BLOB)
    except Exception:
        return None


def find_key(database_path, existing, force):
    """Returns (key or None, note)."""
    if existing and not force:
        ok, _ = try_open(database_path, existing)
        if ok:
            return existing, "kept from .env (verified: database opens with it)"
    key, error_text, logged = capture_key(database_path)
    if key:
        return key, "captured from pyrekordbox (verified: database opens with it)"
    for candidate, source_name in ((logged, "pyrekordbox debug log"), (builtin_key(), "pyrekordbox built-in key")):
        if candidate:
            ok, verification_error = try_open(database_path, candidate)
            if ok:
                return candidate, f"from {source_name} (verified)"
    # nothing verified: if the failure was only a missing sqlcipher, still use the key we have
    candidate = logged or builtin_key()
    if candidate and "sqlcipher" in (error_text or "").lower():
        return candidate, "from pyrekordbox but NOT verified (sqlcipher3 missing: pip install sqlcipher3-wheels)"
    return None, error_text or "unknown error"


# ---------------------------------------------------------------------------------------
# QAAC_PATH
# ---------------------------------------------------------------------------------------
QAAC_NAMES = ("qaac64.exe", "qaac.exe", "qaac64", "qaac")


def qaac_works(path):
    try:
        result = subprocess.run([str(path), "--check"], capture_output=True, text=True, errors="replace", timeout=20)
        return result.returncode == 0, (result.stdout + result.stderr).strip().splitlines()[:1]
    except Exception as error:
        return False, [str(error)]


def search_dirs():
    home = Path.home()
    directories = [HERE, HERE.parent, home / "Desktop", home / "Downloads", home / "Documents", home]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA", "APPDATA"):
        if os.environ.get(var):
            directories.append(Path(os.environ[var]))
    if os.name == "nt":
        directories += [Path("C:/"), Path("C:/tools"), Path("C:/Tools"), Path("D:/")]
    seen, unique_directories = set(), []
    for directory in directories:
        if directory.is_dir() and str(directory) not in seen:
            seen.add(str(directory))
            unique_directories.append(directory)
    return unique_directories


def walk_for_qaac(root, max_depth):
    skipped_names = {"windows", "$recycle.bin", "system volume information", "node_modules", ".git",
            "site-packages", "__pycache__", "appdata", "music", "pictures", "videos"}
    base_depth = len(root.parts)
    try:
        for current_directory, dirnames, files in os.walk(root, onerror=lambda e: None):
            depth = len(Path(current_directory).parts) - base_depth
            dirnames[:] = [directory_name for directory_name in dirnames if directory_name.lower() not in skipped_names] if depth < max_depth else []
            for file_name in files:
                if file_name.lower() in QAAC_NAMES:
                    yield Path(current_directory) / file_name
    except Exception:
        return


def find_qaac(existing, force, search):
    if existing and not force and Path(existing).is_file():
        return Path(existing), "kept from .env"
    for name in QAAC_NAMES:
        which_path = shutil.which(name)
        if which_path:
            return Path(which_path), "found on PATH"
    for directory in (HERE, HERE.parent):
        for name in QAAC_NAMES:
            if (directory / name).is_file():
                return directory / name, "next to this script"
    if not search:
        return None, "not found (disk search skipped)"
    # prefer 64 bit and the newest folder name (qaac_3.07_x64 > qaac_2.x)
    for directory in search_dirs():
        hits = sorted(walk_for_qaac(directory, 3 if directory in (Path.home(), Path("C:/"), Path("D:/")) else 4),
                      key=lambda path: ("64" not in path.name, str(path.parent).lower()), reverse=False)
        if hits:
            return hits[0], f"found by searching {directory}"
    return None, "not found on PATH or in common folders"


# ---------------------------------------------------------------------------------------
def main():
    argument_parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    argument_parser.add_argument("--force", action="store_true", help="re-detect and overwrite values that already work")
    argument_parser.add_argument("--no-search", action="store_true", help="skip the disk search for qaac")
    argument_parser.add_argument("--env", type=Path, default=ENV_PATH, help="target .env (default: next to this script)")
    arguments = argument_parser.parse_args()

    if arguments.env.exists():
        text, source_path = arguments.env.read_text(encoding="utf-8"), arguments.env
    elif TEMPLATE_PATH.exists():
        text, source_path = TEMPLATE_PATH.read_text(encoding="utf-8"), TEMPLATE_PATH
    else:
        text, source_path = "", None
    uses_crlf = "\r\n" in text
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    current_values = parse_env(text)
    print(f"Starting from: {source_path or 'built-in defaults (no .env or _env found)'}\n")

    found, missing = {}, []

    # --- REKORDBOX_DATABASE_PATH
    database, note = find_database(current_values.get("REKORDBOX_DATABASE_PATH"), arguments.force)
    if database:
        found["REKORDBOX_DATABASE_PATH"] = str(database)
        print(f"[ok]      REKORDBOX_DATABASE_PATH   = {database}\n          ({note})")
        share = database.parent / "share"
        print(f"          share folder {'exists' if share.is_dir() else 'NOT FOUND'}: {share}")
    else:
        missing.append(("REKORDBOX_DATABASE_PATH", f"path of master.db, usually %APPDATA%\\Pioneer\\rekordbox\\master.db ({note})"))
        print(f"[MISSING] REKORDBOX_DATABASE_PATH   ({note})")

    # --- REKORDBOX_ENCRYPTION_KEY
    if database:
        key, key_note = find_key(database, current_values.get("REKORDBOX_ENCRYPTION_KEY"), arguments.force)
        if key:
            found["REKORDBOX_ENCRYPTION_KEY"] = key
            flag = "ok" if "NOT verified" not in key_note else "WARN"
            print(f"[{flag:<7}] REKORDBOX_ENCRYPTION_KEY  = {key}\n          ({key_note})")
        else:
            missing.append(("REKORDBOX_ENCRYPTION_KEY", f"database would not open without/with the key: {key_note}"))
            print(f"[MISSING] REKORDBOX_ENCRYPTION_KEY  ({key_note})")
    else:
        if current_values.get("REKORDBOX_ENCRYPTION_KEY"):
            found["REKORDBOX_ENCRYPTION_KEY"] = current_values["REKORDBOX_ENCRYPTION_KEY"]
            print(f"[WARN]    REKORDBOX_ENCRYPTION_KEY  kept from .env but not verified (no master.db found)")
        else:
            missing.append(("REKORDBOX_ENCRYPTION_KEY", "needs REKORDBOX_DATABASE_PATH first; key is auto-detected once master.db is found"))
            print("[MISSING] REKORDBOX_ENCRYPTION_KEY  (needs REKORDBOX_DATABASE_PATH first)")

    # --- QAAC_PATH
    qaac_path, qaac_note = find_qaac(current_values.get("QAAC_PATH"), arguments.force, not arguments.no_search)
    if qaac_path:
        found["QAAC_PATH"] = str(qaac_path)
        ok, first_line = qaac_works(qaac_path)
        print(f"[ok]      QAAC_PATH    = {qaac_path}\n          ({qaac_note})")
        if not ok:
            print(f"[WARN]    `{qaac_path.name} --check` failed ({first_line[0] if first_line else 'no output'}). "
                  "qaac needs Apple's CoreAudioToolbox (iTunes/QuickTime files) installed or placed next to it.")
    else:
        missing.append(("QAAC_PATH", f"full path of qaac64.exe ({qaac_note}). Download: https://github.com/nu774/qaac/releases"))
        print(f"[MISSING] QAAC_PATH    ({qaac_note})")

    # --- tools on PATH (not in .env, but the scripts need them)
    for tool in ("ffmpeg", "ffprobe"):
        tool_path = shutil.which(tool)
        if tool_path:
            print(f"[ok]      {tool} on PATH: {tool_path}")
        else:
            missing.append((tool, "not on PATH. Install ffmpeg (includes ffprobe) and add its bin folder to PATH."))
            print(f"[MISSING] {tool} not on PATH")

    # --- write .env
    for env_key, value in found.items():
        lines = set_value(lines, env_key, value)
    for env_key, default, comment in DEFAULTS:
        if env_key not in current_values and not any(re.match(rf"^\s*{env_key}\s*=", existing_line) for existing_line in lines):
            lines += ["", *comment.split("\n"), f"{env_key}={default}"]
    for env_key in REQUIRED:                               # keep the key present (empty) for easy hand-editing
        if not any(re.match(rf"^\s*{env_key}\s*=", existing_line) for existing_line in lines):
            lines.insert(1 if lines else 0, f"{env_key}=")
    if arguments.env.exists():
        shutil.copy2(arguments.env, arguments.env.with_name(arguments.env.name + ".bak"))
    arguments.env.write_text(("\r\n" if uses_crlf or os.name == "nt" else "\n").join(lines) + ("\r\n" if uses_crlf or os.name == "nt" else "\n"),
                     encoding="utf-8", newline="")
    print(f"\nWrote {arguments.env}" + (f" (previous version saved as {arguments.env.name}.bak)" if arguments.env.with_name(arguments.env.name + '.bak').exists() else ""))

    # --- what is left
    if missing:
        print("\nSTILL TO FILL IN BY HAND:")
        for env_key, why in missing:
            tag_text = f"  {env_key}=" if env_key in REQUIRED else f"  {env_key}"
            print(f"{tag_text:<12} {why}")
        print(f"\nEdit {arguments.env.name}, then re-run this script to verify, or just run run_m4a_conversion.py.")
        sys.exit(1)
    print("\nNothing left to fill in: REKORDBOX_DATABASE_PATH, REKORDBOX_ENCRYPTION_KEY, QAAC_PATH and ffmpeg/ffprobe are all set. You can run run_m4a_conversion.py.")


if __name__ == "__main__":
    main()