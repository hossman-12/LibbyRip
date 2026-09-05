# LibbyRip on Debian 13 (Linux)

This directory contains a Python port of the original PowerShell
`auto_built-m4b.ps1` script so the same audiobook conversion pipeline runs
on Debian 13 / Linux.

The Python port is `auto_build_m4b.py` and produces the same output as the
PowerShell version, using the same `bakeMetadata.py`, `buildChapters.py`,
`ffmpeg` and `ffprobe` steps in the same order, with the same log file
format.

## Files

| File | Purpose |
|---|---|
| `auto_build_m4b.py` | Python orchestrator. Scans `Books/`, skips already-converted zips, runs the full pipeline for the rest. |
| `auto_built-m4b.ps1` | Original PowerShell orchestrator (kept for Windows). |
| `bakeMetadata.py` | Bakes ID3 tags + chapters into each per-part MP3 via `eyed3`. |
| `buildChapters.py` | Generates `metadata.txt` (FFmetadata) and `chapters.txt` from `metadata.json`. |
| `convertToM4b.py` | Single-file MP3 → M4B converter. |
| `log_helper.py` | Shared logging helper. All scripts write to the same log file via `LIBBYRIP_LOG_FILE`. |
| `requirements.txt` | Python dependencies for the headless / CLI pipeline. |

## One-time setup on Debian 13

### 1. System packages

```sh
sudo apt update
sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    ffmpeg
```

`ffprobe` ships with the `ffmpeg` package on Debian.

### 2. Python dependencies

From inside the `LibbyRip` directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

That installs `eyed3` (the only Python package the CLI pipeline needs).
`PyQt5` is **not** required for headless / CLI use — `bakeMetadata.py`
imports it lazily and only when `--gui` is passed.

## Usage

### Convert every `Books/*.zip` that has not yet been converted

```sh
python auto_build_m4b.py
```

### Preview what would be converted (no files written)

```sh
python auto_build_m4b.py --dry-run
```

### Show queued paths and other diagnostic detail

```sh
python auto_build_m4b.py --detail
```

## Directory layout expected

```
LibbyRip/
├── Books/                 # drop zip files here
│   ├── James S. A. Corey - Drive.zip
│   └── Mitch Albom - The Next Person You Meet in Heaven.zip
├── AudioBooks/            # converted m4b files are written here
│   ├── James S. A. Corey/
│   │   └── Drive.m4b
│   └── Mitch Albom/
│       └── The Next Person You Meet in Heaven.m4b
├── logs/                  # one log file per run
│   └── auto-build-YYYYMMDD-HHMMSS.log
├── temp_extracted/        # transient working area (auto-created / cleaned)
├── auto_build_m4b.py
├── bakeMetadata.py
├── buildChapters.py
├── convertToM4b.py
├── log_helper.py
└── requirements.txt
```

The script is idempotent: any zip whose output m4b already exists under
`AudioBooks/` is skipped, so you can drop new zips into `Books/` and rerun.

## Logging

Every line in the log file under `logs/` uses this format:

```
[YYYY-MM-DD HH:MM:SS] [script_name] message
```

`[script_name]` is one of:

- `auto_build_m4b.py` — the orchestrator's own status lines
- `bakeMetadata.py` — the metadata baker
- `buildChapters.py` — the FFmetadata / chapter builder
- `ffmpeg` — the converter
- `ffprobe` — the post-conversion chapter lister

All subprocess output (stdout and stderr) is captured and re-emitted into the
same log file with the appropriate tag, so a single `grep` over the log gives
you the full picture of a run. The log file path is exposed to the Python
subprocesses via the `LIBBYRIP_LOG_FILE` environment variable.

## Differences from the Windows / PowerShell version

| Behaviour | Windows (`auto_built-m4b.ps1`) | Linux (`auto_build_m4b.py`) |
|---|---|---|
| Archive extraction | `Expand-Archive` | `zipfile` stdlib |
| Subprocess capture | `1> file 2> file` with manual `Out-String` | `subprocess.Popen` with `communicate()` |
| External tool on PATH check | `Get-Command` | `shutil.which` |
| Cover filename | `cover.JPG` (case-insensitive lookup) | `cover.jpg` (or `.JPG` fallback) |
| Path joining | `Join-Path` (Windows separators) | `os.path.join` / `pathlib` (POSIX separators) |
| Output paths | `C:\…\AudioBooks\<Author>\<Title>.m4b` | `/…/AudioBooks/<Author>/<Title>.m4b` |
| GUI mode for `bakeMetadata.py` | yes (PyQt5) | yes (PyQt5) — install separately, not required for the CLI |

The output `m4b` files are byte-compatible across the two scripts: same
codec (`aac` 128 kbps), same container (`ipod`), same chapter metadata
format, same attached cover art.

## Troubleshooting

### "ffmpeg was not found on PATH."

Install it: `sudo apt install ffmpeg`. The script calls
`shutil.which("ffmpeg")` at startup; the same check is performed for
`ffprobe` and `python3` (the script uses `sys.executable` so the
interpreter that runs the script is what gets used for the subprocesses).

### eyed3 fails with `Lame tag CRC check failed`

This warning is harmless: the source MP3 was encoded with LAME and has a
CRC value eyed3 cannot verify. The script logs the warning and continues.
Output m4bs are unaffected.

### `ModuleNotFoundError: No module named 'PyQt5'`

You tried to run `bakeMetadata.py --gui` on a headless box. Either install
PyQt5 (`pip install PyQt5`) or run the orchestrator without `--gui`. The
Python port does not need PyQt5 at all.
