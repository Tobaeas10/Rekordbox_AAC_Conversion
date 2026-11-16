# m4a_converter

Converts the wav tracks in a Rekordbox library to AAC (m4a) and keeps everything in Rekordbox intact: the track is relinked in `master.db`, and all cues and beat grids are shifted so they still line up.

## Why the shift is needed
qaac (Apple AAC) puts 2112 samples of silence at the start of every file. The file says so (iTunSMPB tag), but Rekordbox ignores it, so an m4a would play about 48 ms late (44.1 kHz) or 44 ms late (48 kHz) compared to the wav. Every cue, loop and beat time (in the database and in the ANLZ files) is therefore moved later by the same amount, rounded to whole milliseconds because that is what Rekordbox stores.

## What you need
- Python with `pyrekordbox` (plus `sqlcipher3-wheels`), `numpy` (only for the optional cross-correlation check)
- `ffmpeg` and `ffprobe` on your PATH
- `qaac64.exe` (qaac needs Apple's CoreAudioToolbox, i.e. iTunes/QuickTime files, installed or next to it)
- Windows is the main target (the "Rekordbox is running" check only works there)

## Quick start
1. `python populate_env.py`: finds `master.db`, the encryption key and qaac, and writes them into `.env`. It tells you what is still missing.
2. Close Rekordbox, back up `master.db`.
3. `python run_m4a_conversion.py`: walks you through four steps, one batch at a time. Enter continues, anything else stops.
   1. dry run: shows what would happen, changes nothing
   2. encode: writes the `.m4a` next to each wav (the wavs stay)
   3. apply: works on a copy of `master.db` and staged ANLZ files in the work folder, still nothing live
   4. install: backs up the live `master.db` and ANLZ files, then overwrites them (you have to type `yes`)
4. Open Rekordbox and check a few converted tracks, grid and cues at the start AND the end.

Start with a small `BATCH_SIZE` (default 10). Tracks that are already converted drop out of the next batch automatically.

## Settings (`.env`)
| Key | Meaning | Default |
|---|---|---|
| `REKORDBOX_DATABASE_PATH` | path of the live `master.db` | auto-detected |
| `REKORDBOX_ENCRYPTION_KEY` | SQLCipher key | auto-detected |
| `QAAC_PATH` | path of `qaac64.exe` | auto-detected |
| `WORK_DIRECTORY` | staging, manifest, reports, backups | `work` |
| `BATCH_SIZE` | tracks per batch, 0 = all remaining | `10` |
| `AAC_TRUE_VBR_QUALITY` | qaac `--tvbr`, 0 to 127 | `109` |
| `CROSS_CORRELATION_CHECKS` | compare the first N encodes against the wav (0 = off) | `3` |
| `PARALLEL_WORKERS` | parallel qaac jobs | `1` |
| `PATH_MATCH_TEXT` | only tracks whose path contains this text | empty |
| `CUE_SNAP_MARGIN_MS` | used by `snap_cues_to_grid.py` | `10` |

## What encode checks
Each m4a must be AAC, keep the sample rate and channel count, carry the iTunSMPB tag with priming 2112, and have a valid length equal to the wav's length. With cross-correlation on, the real audio offset is measured too. A file that fails is deleted and reported, the wav is left alone.

## Reports (in `<work>/reports`)
- `problems.txt`: anything that was skipped or looked wrong (empty means fine)
- `beatgrid_first_sample.txt`: tracks whose first downbeat sat on the very first sample
- `beatgrid_one_bar_offset.txt`: tracks whose first downbeat was about one bar from the start

These two are worth a look in Rekordbox, because a grid that starts at the very beginning is easy to get wrong.
`manifest.json` in the work folder records every track's state, so an interrupted run can simply be started again.

## Running the steps by hand
`python convert_library_to_m4a.py --database-path <master.db> --encryption-key <key> --qaac-path <qaac64.exe> --work-directory work --limit 10 dry-run`
then the same with `encode`, `apply`, and `--yes install`. `--help` lists everything (`--path-prefix-map`, `--track-ids`, `--adopt-existing`, `--parallel-workers`, tolerances for the two grid reports).

## snap_cues_to_grid.py (separate tool)
Moves every memory cue, hot cue and loop end that is within `CUE_SNAP_MARGIN_MS` of a beat exactly onto that beat, for the whole collection (wav, m4a, mp3, ...). Cues farther away stay as they are. It edits the database and the ANLZ files in place and **makes no backup**, so back up first and close Rekordbox.
`python snap_cues_to_grid.py --dry-run` only writes a report (`snap_report_<time>.txt` next to the script); without `--dry-run` it applies it.

## Files
- `populate_env.py`: fills in `.env`
- `run_m4a_conversion.py`: the guided four-step run
- `convert_library_to_m4a.py`: the actual work (`dry-run`, `encode`, `apply`, `install`)
- `snap_cues_to_grid.py`: grid snapping

## If you upgrade from the old names
Old `.env` keys and flags were renamed (`RB_DB` is now `REKORDBOX_DATABASE_PATH`, `--db` is now `--database-path`, ...). Run `python populate_env.py` once, or rename the keys by hand. Work folders from earlier runs still work, the `manifest.json` format did not change.