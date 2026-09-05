#!/usr/bin/env python3
"""Auto M4B conversion script (cross-platform Python port).

Scans ``./Books`` for ``.zip`` files, skips any that have already been
converted to ``.m4b`` in ``./AudioBooks``, then extracts the remaining books
and converts each one to an iPod-format ``.m4b`` audiobook using the existing
``bakeMetadata.py``, ``buildChapters.py`` and ``ffmpeg`` pipeline.

This is a Python port of the original PowerShell script
``auto_built-m4b.ps1`` so the same workflow runs on Debian 13 / Linux. Both
scripts share the same log file format and the same set of helper functions
where possible.

Usage:
    python3 auto_build_m4b.py [--dry-run] [--detail]

Note:
    On this Debian 13 machine the repo lives on a rclone/FUSE OneDrive
    mount, which silently strips the execute bit on every file. Running
    ``./auto_build_m4b.py`` therefore fails with
    ``/usr/bin/env: bad interpreter: Permission denied`` even after
    ``chmod +x``. Always invoke the script through ``python3`` (or via
    the launcher ``libbyrip-build`` installed in ``~/.local/bin``) so
    the script file does not need to be executable.

Options:
    --dry-run    Preview what would be converted without producing any files.
    --detail     Print queued zip paths and other diagnostic detail.

All stdout and stderr from subprocesses (bakeMetadata.py, buildChapters.py,
ffmpeg, ffprobe) is forwarded to a shared log file in ``./logs`` using the
same ``[date stamp] [script] message`` header used by the rest of the
LibbyRip scripts. The log file path is exported as the ``LIBBYRIP_LOG_FILE``
environment variable so any child process that imports ``log_helper.py`` can
append to the same file.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from log_helper import log as _log


SCRIPT_NAME = "auto_build_m4b.py"
SCRIPT_DIR = Path(__file__).resolve().parent
BOOKS_DIR = SCRIPT_DIR / "Books"
AUDIOBOOKS_DIR = SCRIPT_DIR / "AudioBooks"
LOGS_DIR = SCRIPT_DIR / "logs"
TEMP_EXTRACT_ROOT = SCRIPT_DIR / "temp_extracted"

# Same character class as the PowerShell ``Clean-Name`` function. We also add
# the NUL byte and other control characters that PowerShell's NTFS forbids.
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Match the "Author - Title" pattern with optional whitespace around the dash.
_AUTHOR_TITLE_RE = re.compile(r"^(.*?)\s+-\s+(.+)$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_log(message: str, log_path: Path) -> None:
    """Write a line to the shared log file with the script's own tag stripped.

    The PowerShell version's ``Write-Log`` only writes lines without a
    ``[script]`` header. Lines emitted through ``_write_subprocess_log`` carry
    the ``[script]`` tag instead. Keeping the two functions separate mirrors
    the PowerShell behaviour exactly.
    """
    entry = f"[{_timestamp()}] {message}"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except OSError:
        print(entry)


def _write_subprocess_log(script_name: str, text: str, log_path: Path) -> None:
    """Append ``text`` to the log file, one entry per non-blank line.

    Mirrors the PowerShell ``Write-SubprocessLog`` helper. Multi-line input
    is split so every line in the log carries its own
    ``[date stamp] [script]`` header.
    """
    if not text:
        return
    ts = _timestamp()
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            for line in text.splitlines():
                if not line.strip():
                    continue
                fh.write(f"[{ts}] [{script_name}] {line}\n")
    except OSError:
        # Fall back to stderr if the log file cannot be written.
        for line in text.splitlines():
            if line.strip():
                print(f"[{ts}] [{script_name}] {line}", file=sys.stderr)


def clean_name(name: str) -> str:
    """Strip characters that are illegal in NTFS / ext4 filenames."""
    return _INVALID_NAME_CHARS.sub("", name).strip()


def ensure_command(name: str) -> str:
    """Return the absolute path to an executable on PATH, or raise."""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} was not found on PATH.")
    return path


def read_metadata_creators(metadata_json_path: Path) -> dict:
    """Return ``{"author": str, "narrator": str}`` from a Libby metadata.json."""
    result = {"author": "", "narrator": ""}
    if not metadata_json_path.is_file():
        return result
    try:
        meta = json.loads(metadata_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    creators = meta.get("creator") or []
    if not isinstance(creators, list):
        creators = [creators]
    for entry in creators:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role", "")
        name = entry.get("name", "")
        if role == "author" and name and not result["author"]:
            result["author"] = str(name)
        elif role == "narrator" and name:
            # Narrators may be multiple; join them as the PowerShell version did.
            existing = result["narrator"]
            result["narrator"] = ", ".join(filter(None, [existing, str(name)])) if existing else str(name)
    return result


def read_metadata_title(metadata_json_path: Path) -> str:
    """Return the audiobook title from ``metadata.json`` (or empty string)."""
    if not metadata_json_path.is_file():
        return ""
    try:
        meta = json.loads(metadata_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(meta.get("title", ""))


def write_utf8_no_bom(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8 without a byte-order mark."""
    path.write_text(text, encoding="utf-8")


def inject_author_narrator_tags(
    ffmeta_path: Path, author: str, narrator: str, title: str
) -> None:
    """Inject ``artist``, ``album_artist``, ``comment``, ``album``, ``title``
    and ``composer`` tags into the FFmetadata file produced by
    ``buildChapters.py`` if they are not already present.

    The PowerShell version (and the resulting ``ffmpeg`` invocation) require
    these tags so the resulting m4b has the proper artist / chapter metadata.
    """
    if not ffmeta_path.is_file():
        raise RuntimeError(f"FFmetadata file not found: {ffmeta_path}")
    text = ffmeta_path.read_text(encoding="utf-8")
    if not re.match(r"^\s*;FFMETADATA1", text):
        raise RuntimeError("metadata.txt does not start with ;FFMETADATA1")

    def _has(field: str) -> bool:
        return re.search(rf"(?m)^\s*{re.escape(field)}=", text) is not None

    composer_name = ""
    comment_match = re.search(r"(?m)^\s*comment=(.+)$", text)
    if comment_match:
        comment_value = comment_match.group(1).strip()
        narrated = re.search(r"(?i)Narrated by\s+(.+)", comment_value)
        if narrated:
            composer_name = narrated.group(1).strip()
    if not composer_name and narrator:
        composer_name = narrator

    adds = []
    if author and not _has("artist"):
        adds.append(f"artist={author}")
    if author and not _has("album_artist"):
        adds.append(f"album_artist={author}")
    if narrator and not _has("comment"):
        adds.append(f"comment=Narrated by {narrator}")
    if title and not _has("album"):
        adds.append(f"album={title}")
    if title and not _has("title"):
        adds.append(f"title={title}")
    if composer_name and not _has("composer"):
        adds.append(f"composer={composer_name}")

    if not adds:
        return

    # Insert the new lines immediately after the first line (;FFMETADATA1).
    parts = text.split("\n", 1)
    new_text = parts[0] + "\n" + "\n".join(adds) + "\n" + (parts[1] if len(parts) > 1 else "")
    write_utf8_no_bom(ffmeta_path, new_text)


def find_existing_m4b(basename: str, audio_books_dir: Path) -> Optional[Path]:
    """Return an existing .m4b for ``basename`` if one can be found.

    Matches the PowerShell version's two-tier lookup:
        1) ``<basename>.m4b`` anywhere under ``AudioBooks``.
        2) If the basename is "Author - Title", ``<Author>/<Title>.m4b``.
    """
    if not audio_books_dir.is_dir():
        return None
    for candidate in audio_books_dir.rglob(f"{basename}.m4b"):
        if candidate.is_file():
            return candidate
    m = _AUTHOR_TITLE_RE.match(basename)
    if m:
        clean_author = clean_name(m.group(1).strip())
        clean_title = clean_name(m.group(2).strip())
        candidate = audio_books_dir / clean_author / f"{clean_title}.m4b"
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Subprocess helpers (streaming + single-line progress)
# ---------------------------------------------------------------------------


def _format_hms(seconds: float) -> str:
    """Format ``seconds`` as ``H:MM:SS`` (drops the hour field if zero)."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _write_subprocess_log_line(
    script_name: str, line: str, log_path: Path
) -> None:
    """Write a single non-blank line to the log with the standard header.

    Mirrors ``_write_subprocess_log`` for the streaming path: one log line
    per child stdout/stderr line, each carrying its own ``[date stamp]
    [script]`` prefix so the log can be grepped / filtered line-by-line.
    """
    if not line.strip():
        return
    entry = f"[{_timestamp()}] [{script_name}] {line}\n"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError:
        print(entry.rstrip("\n"), file=sys.stderr)


def _ffmeta_total_seconds(ffmeta_path: Path) -> Optional[float]:
    """Return the total audiobook duration in seconds from the ffmetadata.

    The ``ffmetadata`` file produced by ``buildChapters.py --ffmpeg`` uses
    ``TIMEBASE=1/1000`` so the ``END=...`` value of the last ``[CHAPTER]``
    block is the total duration in milliseconds. Returns ``None`` if the
    file is missing or has no chapter blocks (which would mean
    ``buildChapters`` produced something unexpected).
    """
    if not ffmeta_path.is_file():
        return None
    try:
        text = ffmeta_path.read_text(encoding="utf-8")
    except OSError:
        return None
    ends = re.findall(r"(?m)^\s*END=(\d+)\s*$", text)
    if not ends:
        return None
    try:
        return int(ends[-1]) / 1000.0
    except ValueError:
        return None


def _stream_subprocess_lines(
    stream,
    label: str,
    log_path: Path,
    sink: list,
    progress_state: Optional[dict],
    line_lock: threading.Lock,
) -> None:
    """Read ``stream`` line by line until EOF.

    Each non-blank line is appended to ``sink`` (under ``line_lock`` so the
    parent can read it concurrently) and forwarded to the shared log file.
    When ``progress_state`` is provided, the latest
    ``time=HH:MM:SS.FF`` value from ffmpeg's stderr is recorded so the
    spinner can show progress against the total duration.
    """
    for line in iter(stream.readline, ""):
        stripped = line.rstrip("\n")
        if stripped.strip():
            _write_subprocess_log_line(label, stripped, log_path)
        with line_lock:
            sink.append(line)
        if progress_state is not None:
            m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
            if m:
                h, mm, s = m.groups()
                progress_state["time_seconds"] = (
                    int(h) * 3600 + int(mm) * 60 + float(s)
                )


def _spin_until(stop_event: threading.Event, get_message) -> None:
    """Background thread target that refreshes a single line in place.

    Updates ``get_message()`` every 200 ms until ``stop_event`` is set,
    using ``\\r`` so the line stays on the same row. Used by ``_StepLine``.
    """
    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    i = 0
    while not stop_event.wait(0.2):
        glyph = frames[i % len(frames)]
        try:
            msg = get_message()
        except Exception:
            msg = ""
        sys.stdout.write(f"\r{glyph} {msg}   ")
        sys.stdout.flush()
        i += 1


class _StepLine:
    """A self-erasing single-line progress display for one step of work.

    While the step is running, the line refreshes in place via ``\\r``:
    ``⠋ Extracting foo.zip ... 0:06`` -- the spinner glyph rotates and
    the status string (default ``elapsed M:SS``) is updated every 200 ms.

    On ``finish()`` (or context-manager exit) the spinner glyph is
    replaced with ``✓`` (success) or ``✗`` (failure) and the line is
    committed to its own row with ``\\n``, so the next step starts on a
    fresh line.

    In a non-TTY context the class falls back to plain prints so logs
    remain self-explanatory.

    Usage::

        with _StepLine("Extracting foo.zip"):
            do_slow_work()

        step = _StepLine("ffmpeg", get_message=progress_message)
        try:
            rc = run_thing()
        finally:
            step.finish(rc)
    """

    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _OK = "✓"
    _FAIL = "✗"

    def __init__(self, label: str, get_message=None):
        self.label = label
        self.start = time.monotonic()
        self.use_spinner = sys.stdout.isatty()
        self._stop = threading.Event()
        self._done = False
        self._lock = threading.Lock()
        self._final_msg = ""

        if get_message is None:
            get_message = self._default_message
        self.get_message = get_message

        if self.use_spinner:
            self._thread = threading.Thread(target=self._spin_loop, daemon=True)
            self._render(f"{self._SPINNER[0]} {self.label} ... ?")
            self._thread.start()
        else:
            print(f"... {self.label}", flush=True)

    def _default_message(self) -> str:
        return _format_hms(time.monotonic() - self.start)

    def _render(self, text: str) -> None:
        with self._lock:
            self._final_msg = text
            sys.stdout.write("\r" + text + "   ")
            sys.stdout.flush()

    def _clear(self) -> None:
        with self._lock:
            # Erase whatever we last drew on this row.
            sys.stdout.write("\r" + " " * (len(self._final_msg) + 4) + "\r")
            sys.stdout.flush()

    def _spin_loop(self) -> None:
        i = 0
        while not self._stop.wait(0.2):
            glyph = self._SPINNER[i % len(self._SPINNER)]
            try:
                msg = self.get_message()
            except Exception:
                msg = "?"
            self._render(f"{glyph} {self.label} ... {msg}")
            i += 1

    def finish(self, exit_code: int = 0) -> None:
        """Commit the final line. Safe to call multiple times."""
        with self._lock:
            if self._done:
                return
            self._done = True
        if self.use_spinner:
            self._stop.set()
            self._thread.join(timeout=1.0)
            try:
                msg = self.get_message()
            except Exception:
                msg = _format_hms(time.monotonic() - self.start)
            glyph = self._OK if exit_code == 0 else self._FAIL
            self._clear()
            sys.stdout.write(f"{glyph} {self.label} ... {msg}\n")
            sys.stdout.flush()
        else:
            elapsed = _format_hms(time.monotonic() - self.start)
            status = "OK" if exit_code == 0 else f"FAIL ({exit_code})"
            print(
                f"    {self.label} ... {elapsed}  [{status}]",
                flush=True,
            )

    def __enter__(self) -> "_StepLine":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._done:
            self.finish(0 if exc_type is None else 1)


def _run_with_progress(
    argv,
    *,
    label: str,
    log_path: Path,
    input_text: Optional[str] = None,
    cwd: Optional[Path] = None,
    total_duration_seconds: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess with real-time log streaming and a single-line status.

    Streams child stdout/stderr line-by-line into the shared log file (one
    ``[date stamp] [script]`` header per line) while drawing a single-line
    ``⠋ <label> ... <status>`` indicator on stdout. For ffmpeg we parse
    the ``time=`` value from stderr and show audio progress against
    ``total_duration_seconds``; for other tools the status is just the
    elapsed time.

    Returns a ``CompletedProcess`` whose stdout/stderr are the accumulated
    text written by the child. Callers that inspect those streams continue
    to work unchanged.

    The spinner glyphs are only drawn when stdout is a TTY; in non-TTY
    contexts the step line falls back to plain prints.
    """
    _write_log(f"Running {label}", log_path)

    progress_state: Optional[dict] = (
        {"time_seconds": 0.0} if total_duration_seconds else None
    )
    line_lock = threading.Lock()
    stdout_sink: list[str] = []
    stderr_sink: list[str] = []
    start_time = time.monotonic()

    def get_message() -> str:
        elapsed = time.monotonic() - start_time
        if progress_state is not None and progress_state["time_seconds"] > 0:
            current = progress_state["time_seconds"]
            pct = min(100.0, current / total_duration_seconds * 100)
            return (
                f"{_format_hms(current)} / {_format_hms(total_duration_seconds)}"
                f" ({pct:.0f}%)  elapsed {_format_hms(elapsed)}"
            )
        return f"elapsed {_format_hms(elapsed)}"

    step = _StepLine(f"Running {label}", get_message=get_message)

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )

    try:
        if input_text is not None:
            try:
                process.stdin.write(input_text)
                process.stdin.close()
            except BrokenPipeError:
                pass

        reader_threads = [
            threading.Thread(
                target=_stream_subprocess_lines,
                args=(
                    process.stdout,
                    label,
                    log_path,
                    stdout_sink,
                    None,
                    line_lock,
                ),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_subprocess_lines,
                args=(
                    process.stderr,
                    label,
                    log_path,
                    stderr_sink,
                    progress_state,
                    line_lock,
                ),
                daemon=True,
            ),
        ]
        for t in reader_threads:
            t.start()

        try:
            process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise

        for t in reader_threads:
            t.join(timeout=2.0)
    finally:
        step.finish(getattr(process, "returncode", 1) or 0)

    _write_log(f"{label} exit code: {process.returncode}", log_path)
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        "".join(stdout_sink),
        "".join(stderr_sink),
    )


# ---------------------------------------------------------------------------
# Conversion pipeline
# ---------------------------------------------------------------------------

def resolve_output_path(
    script_dir: Path, author: str, title: str
) -> Path:
    """Build the destination ``<AudioBooks>/<Author>/<Title>.m4b`` path.

    Mirrors the PowerShell variable substitution:
        %LaunchDir%/AudioBooks/%AUTHOR%/%TITLE%.m4b
    """
    safe_author = clean_name(author) or "Unknown Author"
    safe_title = clean_name(title) or "Unknown Title"
    return (script_dir / "AudioBooks" / safe_author / f"{safe_title}.m4b").resolve()


def process_one_zip(
    zip_path: Path,
    *,
    script_dir: Path,
    log_path: Path,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Convert a single ``.zip`` into a ``.m4b``.

    Returns ``(success, output_path_or_none)`` for the caller to record.
    """
    book_base = zip_path.stem
    temp_extract = TEMP_EXTRACT_ROOT / book_base

    if dry_run:
        # Skip actual work in dry-run mode. The PowerShell version returns
        # the would-be output path so the user can see where the file would
        # be written; we do the same.
        existing = find_existing_m4b(book_base, AUDIOBOOKS_DIR)
        if existing is not None:
            return True, str(existing)
        m = _AUTHOR_TITLE_RE.match(book_base)
        author = clean_name(m.group(1).strip()) if m else "<Author>"
        title = clean_name(m.group(2).strip()) if m else "<Title>"
        preview_meta = temp_extract / "metadata" / "metadata.json"
        if preview_meta.is_file():
            creds = read_metadata_creators(preview_meta)
            meta_title = read_metadata_title(preview_meta)
            if creds["author"]:
                author = clean_name(creds["author"])
            if meta_title:
                title = clean_name(meta_title)
        out = script_dir / "AudioBooks" / author / f"{title}.m4b"
        return True, str(out)

    # Make sure the temp directory is empty.
    if temp_extract.exists():
        shutil.rmtree(temp_extract)
    temp_extract.mkdir(parents=True, exist_ok=True)

    try:
        _write_log(f"Extracting {zip_path.name}", log_path)
        # Show a single-line progress indicator while the silent zip
        # extraction runs. On a TTY the spinner refreshes in place; in
        # non-TTY contexts we still bracket the work with a "..." line
        # so logs are self-explanatory.
        with _StepLine(f"Extracting {zip_path.name}"):
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(temp_extract)

        metadata_dir = temp_extract / "metadata"
        metadata_json = metadata_dir / "metadata.json"
        cover_jpg = metadata_dir / "cover.jpg"  # case-insensitive on Windows
        chapters_txt = metadata_dir / "chapters.txt"
        ffmeta_txt = metadata_dir / "metadata.txt"
        files_txt = temp_extract / "files.txt"

        if not metadata_json.is_file():
            raise RuntimeError("Missing metadata.json")
        if not cover_jpg.is_file():
            # PowerShell version used "cover.JPG" (uppercase). Try the
            # alternative casing explicitly so we still find the cover on
            # case-sensitive filesystems.
            alt = metadata_dir / "cover.JPG"
            if alt.is_file():
                cover_jpg = alt
            else:
                raise RuntimeError("Missing cover image")

        creds = read_metadata_creators(metadata_json)
        author = creds["author"] or "Unknown Author"
        narrator = creds["narrator"]
        meta_title = read_metadata_title(metadata_json) or book_base

        output_file = resolve_output_path(script_dir, author, meta_title)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Step 1: bake metadata into the per-part MP3s.
        bake = _run_with_progress(
            [sys.executable, str(script_dir / "bakeMetadata.py"), str(temp_extract)],
            label="bakeMetadata.py",
            log_path=log_path,
        )
        if bake.returncode != 0:
            tail = "\n".join((bake.stderr or "").splitlines()[-5:])
            _write_log(f"bakeMetadata.py stderr tail: {tail}", log_path)
            if not cover_jpg.is_file():
                raise RuntimeError(
                    f"bakeMetadata.py failed (exit {bake.returncode}) and cover image is missing"
                )
            _write_log(
                f"bakeMetadata.py exited {bake.returncode} but cover image is present; continuing",
                log_path,
            )

        # Step 2: build ffmetadata and chapters.txt.
        meta_text = metadata_json.read_text(encoding="utf-8")
        bc_ffmpeg = _run_with_progress(
            [sys.executable, str(script_dir / "buildChapters.py"), "--ffmpeg"],
            label="buildChapters.py",
            log_path=log_path,
            input_text=meta_text,
        )
        if bc_ffmpeg.returncode != 0:
            tail = "\n".join((bc_ffmpeg.stderr or "").splitlines()[-5:])
            _write_log(f"buildChapters.py --ffmpeg stderr tail: {tail}", log_path)
        write_utf8_no_bom(ffmeta_txt, (bc_ffmpeg.stdout or "").rstrip())

        bc_chapters = _run_with_progress(
            [sys.executable, str(script_dir / "buildChapters.py"), "--chapters"],
            label="buildChapters.py",
            log_path=log_path,
            input_text=meta_text,
        )
        if bc_chapters.returncode != 0:
            tail = "\n".join((bc_chapters.stderr or "").splitlines()[-5:])
            _write_log(f"buildChapters.py --chapters stderr tail: {tail}", log_path)
        write_utf8_no_bom(chapters_txt, (bc_chapters.stdout or "").rstrip())

        inject_author_narrator_tags(ffmeta_txt, author, narrator, meta_title)

        # Step 3: build the concat list and run ffmpeg.
        parts = sorted(temp_extract.glob("Part *.mp3"))
        files_txt.write_text(
            "\n".join(f"file '{p.name}'" for p in parts) + "\n",
            encoding="ascii",
        )

        ffmpeg = _run_with_progress(
            [
                ensure_command("ffmpeg"),
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(files_txt),
                "-f", "ffmetadata",
                "-i", str(ffmeta_txt),
                "-i", str(cover_jpg),
                "-map", "0:a",
                "-map_metadata", "1",
                "-map_chapters", "1",
                "-map", "2:v",
                "-c:a", "aac",
                "-b:a", "128k",
                "-c:v", "mjpeg",
                "-disposition:v", "attached_pic",
                "-f", "ipod",
                str(output_file),
            ],
            label="ffmpeg",
            log_path=log_path,
            cwd=temp_extract,
            total_duration_seconds=_ffmeta_total_seconds(ffmeta_txt),
        )

        if not output_file.is_file():
            raise RuntimeError(
                f"Output not created (ffmpeg exit {ffmpeg.returncode})"
            )
        file_size = output_file.stat().st_size
        if file_size < 1024:
            output_file.unlink(missing_ok=True)
            raise RuntimeError(
                f"Output file is too small ({file_size} bytes); ffmpeg exit {ffmpeg.returncode}"
            )
        if ffmpeg.returncode != 0:
            _write_log(
                f"ffmpeg exited {ffmpeg.returncode} but produced valid file of size {file_size}; treating as success",
                log_path,
            )

        # Step 4: ffprobe the result for diagnostic logging.
        _run_with_progress(
            [
                ensure_command("ffprobe"),
                "-hide_banner",
                "-show_chapters",
                str(output_file),
            ],
            label="ffprobe",
            log_path=log_path,
        )

        _write_log(f"Created {output_file} (size {file_size})", log_path)
        return True, str(output_file)
    except Exception as exc:
        _write_log(f"Failed processing {zip_path}: {exc}", log_path)
        return False, None
    finally:
        if temp_extract.exists():
            shutil.rmtree(temp_extract, ignore_errors=True)


# ---------------------------------------------------------------------------
# Top-level workflow
# ---------------------------------------------------------------------------

def setup_logging(script_dir: Path) -> Path:
    """Prepare the ``logs`` directory, return the path to a fresh log file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIOBOOKS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    log_name = f"auto-build-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log_path = script_dir / "logs" / log_name
    log_path.touch()
    # Export the log file path so subprocess scripts (bakeMetadata.py,
    # buildChapters.py, convertToM4b.py) can append to the same file via
    # log_helper.py.
    os.environ["LIBBYRIP_LOG_FILE"] = str(log_path)
    return log_path


def build_queue() -> list[Path]:
    """Return the list of ``Books/*.zip`` files that still need conversion."""
    if not BOOKS_DIR.is_dir():
        return []
    zips = sorted(BOOKS_DIR.glob("*.zip"))
    queue: list[Path] = []
    for zip_path in zips:
        existing = find_existing_m4b(zip_path.stem, AUDIOBOOKS_DIR)
        if existing is not None:
            _write_log(
                f"Skipping {zip_path.name} - already converted at {existing}",
                Path(os.environ["LIBBYRIP_LOG_FILE"]),
            )
            continue
        queue.append(zip_path)
    return queue


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be converted without producing any files.",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print queued zip paths and other diagnostic detail.",
    )
    args = parser.parse_args(argv)

    log_path = setup_logging(SCRIPT_DIR)
    _write_log(f"Script started (DryRun={args.dry_run})", log_path)
    _write_log(f"AudioBooksDir resolved to: {AUDIOBOOKS_DIR}", log_path)
    _write_log(f"BooksDir resolved to: {BOOKS_DIR}", log_path)

    try:
        # Verify the interpreter that's actually running this script. On
        # Debian there is no ``python`` binary (only ``python3``), so we
        # accept either name and resolve via PATH if needed.
        python_path = shutil.which(sys.executable) or ensure_command("python3")
        if not python_path:
            raise RuntimeError("Python interpreter was not found on PATH.")
        ensure_command("ffmpeg")
        ensure_command("ffprobe")
    except RuntimeError as exc:
        _write_log(str(exc), log_path)
        print(exc, file=sys.stderr)
        return 1

    if not BOOKS_DIR.is_dir():
        _write_log(f"BooksDir not found: {BOOKS_DIR}", log_path)
        print(f"Books directory not found: {BOOKS_DIR}", file=sys.stderr)
        return 1

    zips = sorted(BOOKS_DIR.glob("*.zip"))
    _write_log(f"Found {len(zips)} zip files", log_path)
    print(f"Found {len(zips)} zip file(s) in {BOOKS_DIR}")

    # ``build_queue`` walks every zip through ``find_existing_m4b`` which
    # does ``AudioBooks.rglob`` -- silent but potentially slow on a FUSE
    # mount. Show a single-line indicator so the user knows the script
    # hasn't hung between "Found N zips" and the conversion report.
    with _StepLine("Scanning books for already-converted audiobooks"):
        to_convert = build_queue()
    print(f"Files queued for conversion: {len(to_convert)}")
    _write_log(f"Files queued for conversion count: {len(to_convert)}", log_path)
    if args.detail:
        print("--- Files to be converted (detail) ---")
        for p in to_convert:
            print(p)
            _write_log(f"Queued path: {p}", log_path)

    successes: list[str] = []
    failures: list[str] = []
    for zip_path in to_convert:
        _write_log(f"Raw zipPath value: '{zip_path}'", log_path)
        if args.dry_run:
            ok, out = process_one_zip(
                zip_path, script_dir=SCRIPT_DIR, log_path=log_path, dry_run=True
            )
            label = f"[DryRun] {zip_path} -> {out}" if out else f"[DryRun] {zip_path}"
            _write_log(label, log_path)
            if ok:
                successes.append(label)
            else:
                failures.append(label)
            continue
        _write_log(f"Processing zipPath: {zip_path}", log_path)
        ok, out = process_one_zip(
            zip_path, script_dir=SCRIPT_DIR, log_path=log_path, dry_run=False
        )
        if ok and out:
            successes.append(out)
        elif not ok:
            failures.append(f"Failed processing {zip_path}")

    print()
    print("=== Conversion Report ===")
    print(f"Successful conversions: {len(successes)}")
    for s in successes:
        print(f"  {s}")
    print(f"Failed conversions: {len(failures)}")
    for f in failures:
        print(f"  {f}")
    _write_log(
        f"Report - Success: {len(successes)} Failure: {len(failures)}",
        log_path,
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
