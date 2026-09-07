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
from difflib import SequenceMatcher
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Optional

from buildChapters import Metadata as ChapterMetadata
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


def read_metadata_object(metadata_json_path: Path) -> Optional[dict]:
    """Read and return a Libby metadata object, or ``None`` if invalid."""
    if not metadata_json_path.is_file():
        return None
    try:
        value = json.loads(metadata_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _metadata_creators(metadata: dict) -> dict[str, str]:
    """Return the author and narrator values used by output validation."""
    result = {"author": "", "narrator": ""}
    creators = metadata.get("creator") or []
    if not isinstance(creators, list):
        creators = [creators]
    for entry in creators:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", ""))
        name = str(entry.get("name", ""))
        if role == "author" and name and not result["author"]:
            result["author"] = name
        elif role == "narrator" and name:
            result["narrator"] = ", ".join(
                filter(None, [result["narrator"], name])
            )
        elif role == "author and narrator" and name:
            result["author"] = result["author"] or name
            result["narrator"] = result["narrator"] or name
    return result


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


def _expected_m4b_metadata(metadata: dict) -> dict:
    """Build normalized validation expectations from Libby metadata."""
    parsed = ChapterMetadata.from_json(deepcopy(metadata))
    creators = _metadata_creators(metadata)
    chapters = []
    for index, chapter in enumerate(parsed.chapters):
        end = (
            parsed.chapters[index + 1].total_offset
            if index + 1 < len(parsed.chapters)
            else parsed.total_duration
        )
        chapters.append(
            {
                "title": chapter.title,
                "start": chapter.total_offset.total_seconds(),
                "end": end.total_seconds(),
            }
        )
    return {
        "title": str(metadata.get("title", "")),
        "author": creators["author"],
        "narrator": creators["narrator"],
        "duration": parsed.total_duration.total_seconds(),
        "chapters": chapters,
    }


def _probe_m4b(path: Path) -> tuple[Optional[dict], list[str]]:
    """Return ffprobe JSON and validation errors for basic readability."""
    try:
        result = subprocess.run(
            [
                ensure_command("ffprobe"),
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, RuntimeError) as exc:
        return None, [f"ffprobe could not run: {exc}"]
    if result.returncode != 0:
        return None, [f"ffprobe exit code {result.returncode}: {result.stderr.strip()}"]
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, [f"ffprobe returned invalid JSON: {exc}"]
    if not isinstance(value, dict):
        return None, ["ffprobe returned a non-object JSON result"]
    return value, []


def _probe_chapter_seconds(chapter: dict, field: str) -> Optional[float]:
    """Convert an ffprobe chapter timestamp into seconds."""
    value = chapter.get(f"{field}_time")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    value = chapter.get(field)
    time_base = chapter.get("time_base", "1/1")
    try:
        numerator, denominator = (int(part) for part in str(time_base).split("/", 1))
        return float(value) * numerator / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _normalized_chapter_title(value: str) -> str:
    """Normalize punctuation/encoding differences in chapter titles."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _chapter_titles_match(actual: str, expected: str) -> bool:
    """Compare titles while tolerating lossy punctuation/diacritic encoding."""
    actual_normalized = _normalized_chapter_title(actual)
    expected_normalized = _normalized_chapter_title(expected)
    if actual_normalized == expected_normalized:
        return True
    return (
        SequenceMatcher(None, actual_normalized, expected_normalized).ratio()
        >= 0.85
    )


def validate_m4b(path: Path, metadata: dict) -> tuple[bool, list[str]]:
    """Validate an M4B against its source Libby metadata.

    Duration and chapter boundaries use a two-second tolerance because AAC
    encoding and MP4 muxing can introduce small timestamp differences.
    """
    errors: list[str] = []
    if not path.is_file():
        return False, ["file does not exist"]
    try:
        if path.stat().st_size < 1024:
            errors.append("file is smaller than 1 KiB")
    except OSError as exc:
        return False, [f"cannot stat file: {exc}"]

    probe, probe_errors = _probe_m4b(path)
    errors.extend(probe_errors)
    if probe is None:
        return False, errors

    expected = _expected_m4b_metadata(metadata)
    format_info = probe.get("format") or {}
    tags = {str(k).lower(): str(v) for k, v in (format_info.get("tags") or {}).items()}
    streams = probe.get("streams") or []
    chapters = probe.get("chapters") or []
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    cover_streams = [
        s for s in streams
        if s.get("codec_type") == "video"
        and int((s.get("disposition") or {}).get("attached_pic", 0)) == 1
    ]

    if not audio_streams:
        errors.append("no audio stream")
    elif audio_streams[0].get("codec_name") != "aac":
        errors.append(f"audio codec is {audio_streams[0].get('codec_name')}, expected aac")
    if not cover_streams:
        errors.append("no attached cover artwork")

    try:
        actual_duration = float(format_info.get("duration", 0))
    except (TypeError, ValueError):
        actual_duration = 0.0
    if actual_duration <= 0:
        errors.append("missing or non-positive duration")
    elif abs(actual_duration - expected["duration"]) > 2.0:
        errors.append(
            f"duration {actual_duration:.3f}s differs from expected "
            f"{expected['duration']:.3f}s"
        )

    if len(chapters) != len(expected["chapters"]):
        errors.append(
            f"chapter count {len(chapters)} differs from expected "
            f"{len(expected['chapters'])}"
        )
    for index, (actual, expected_chapter) in enumerate(
        zip(chapters, expected["chapters"])
    ):
        actual_start = _probe_chapter_seconds(actual, "start")
        actual_end = _probe_chapter_seconds(actual, "end")
        if actual_start is None or actual_end is None:
            errors.append(f"chapter {index + 1} has invalid timestamps")
            continue
        if abs(actual_start - expected_chapter["start"]) > 2.0:
            errors.append(f"chapter {index + 1} start timestamp differs")
        if abs(actual_end - expected_chapter["end"]) > 2.0:
            errors.append(f"chapter {index + 1} end timestamp differs")
        actual_title = str((actual.get("tags") or {}).get("title", ""))
        if not _chapter_titles_match(actual_title, expected_chapter["title"]):
            errors.append(f"chapter {index + 1} title differs")

    expected_title = expected["title"].strip()
    if expected_title and tags.get("title", "").strip() != expected_title:
        errors.append("title metadata differs")
    if expected_title and tags.get("album", "").strip() != expected_title:
        errors.append("album metadata differs")
    if expected["author"]:
        author = expected["author"].strip().casefold()
        if author not in {
            tags.get("artist", "").strip().casefold(),
            tags.get("album_artist", "").strip().casefold(),
        }:
            errors.append("author metadata differs")
    if expected["narrator"]:
        narrator = expected["narrator"].strip().casefold()
        narrator_tags = " ".join(
            [tags.get("comment", ""), tags.get("composer", "")]
        ).casefold()
        if narrator not in narrator_tags:
            errors.append("narrator metadata differs")

    return not errors, errors


def _read_zip_metadata(zip_path: Path) -> Optional[dict]:
    """Read metadata.json from a zip without extracting its audio files."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            with zf.open("metadata/metadata.json") as fh:
                value = json.load(fh)
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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


def _is_transient_extract_error(exc: BaseException) -> bool:
    """True if ``exc`` is a flaky-mount error we should retry.

    The repo lives on a rclone/OneDrive FUSE mount that occasionally
    returns ``[Errno 5] Input/output error`` (EIO) or surfaces a
    ``BadZipFile`` when rclone hasn't fully cached the file. These are
    transient and clear on retry; other errors (FileNotFoundError,
    PermissionError, real zip corruption, etc.) propagate immediately.
    """
    if isinstance(exc, zipfile.BadZipFile):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (5, 11):
        # errno 5 = EIO, errno 11 = EAGAIN ("Resource temporarily
        # unavailable"). Both show up on rclone FUSE under load.
        return True
    return False


def _sleep_with_spinner(step: "_StepLine", seconds: float) -> None:
    """Sleep ``seconds`` while the spinner keeps refreshing.

    The Python-level sleep is broken into 200 ms slices so the spinner's
    background thread can keep painting updated status (e.g. elapsed
    time) instead of freezing for the full duration.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(0.2, end - time.monotonic()))


def _extract_from_local_copy(
    zip_path: Path,
    dest_dir: Path,
    log_path: Path,
    step: "_StepLine",
) -> None:
    """Copy ``zip_path`` to a local tempfile and extract from there.

    Once the zip is on a local filesystem (not the rclone FUSE mount),
    all reads are guaranteed and ``zf.extractall`` will not see EIO.
    The local copy is removed on exit.
    """
    local_zip = Path(tempfile.gettempdir()) / (
        f"libbyrip-{zip_path.stem}-{os.getpid()}.zip"
    )
    try:
        step.set_message_override(
            f"copying {zip_path.name} to local disk ({local_zip})"
        )
        _write_log(
            f"Local-copy fallback: copying {zip_path.name} -> {local_zip}",
            log_path,
        )
        shutil.copy2(zip_path, local_zip)
        step.set_message_override("extracting from local copy")
        with zipfile.ZipFile(local_zip, "r") as zf:
            zf.extractall(dest_dir)
        _write_log(
            f"Local-copy extraction succeeded for {zip_path.name}",
            log_path,
        )
    finally:
        try:
            local_zip.unlink(missing_ok=True)
        except OSError:
            pass


def _extract_zip_with_retry(
    zip_path: Path,
    dest_dir: Path,
    log_path: Path,
    step: "_StepLine",
    *,
    max_attempts: int = 3,
) -> None:
    """Extract ``zip_path`` to ``dest_dir`` with retry on FUSE transients.

    Tries up to ``max_attempts`` times against the original FUSE-backed
    path with exponential backoff (2 s, 4 s between attempts). If all
    attempts fail with a transient error, falls back to copying the zip
    to a local tempfile and extracting from there. Non-transient errors
    propagate immediately so the user sees a clear failure.

    ``step`` is updated via ``set_message_override`` during retry
    sleeps so the spinner shows ``retrying in 2s (attempt 1/3)`` etc.
    """
    delays = (2.0, 4.0, 8.0)[: max(0, max_attempts - 1)]
    last_error: Optional[BaseException] = None

    for attempt in range(1, max_attempts + 1):
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest_dir)
            if attempt > 1:
                _write_log(
                    f"Extract succeeded on attempt {attempt} for {zip_path.name}",
                    log_path,
                )
            return
        except Exception as exc:
            if not _is_transient_extract_error(exc):
                # Not a flaky-mount error: surface it immediately.
                raise
            last_error = exc
            kind = (
                f"EIO (errno {exc.errno})"
                if isinstance(exc, OSError)
                else type(exc).__name__
            )
            if attempt < max_attempts:
                delay = delays[attempt - 1]
                msg = (
                    f"{kind} on attempt {attempt}/{max_attempts}; "
                    f"retrying in {delay:.0f}s"
                )
                _write_log(
                    f"Transient extract error ({msg}): {exc}",
                    log_path,
                )
                step.set_message_override(f"⚠ {msg}")
                _sleep_with_spinner(step, delay)
            else:
                _write_log(
                    f"All {max_attempts} attempts failed with {kind}; "
                    f"falling back to local-copy strategy",
                    log_path,
                )
                step.set_message_override(None)

    # All retries exhausted -- try the local-copy fallback. If that
    # also fails, propagate the original FUSE error so the caller
    # sees something descriptive in the log.
    try:
        _extract_from_local_copy(zip_path, dest_dir, log_path, step)
    except Exception:
        if last_error is not None:
            raise last_error from None
        raise


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
        self._message_override: Optional[str] = None

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

    def set_message_override(self, override: Optional[str]) -> None:
        """Temporarily replace the message shown next to the spinner.

        Used by retry loops to surface transient-error context like
        ``retrying in 4s (attempt 2/3)``. Pass ``None`` to clear and
        restore the normal ``get_message`` output.
        """
        with self._lock:
            self._message_override = override

    def _current_message(self) -> str:
        with self._lock:
            if self._message_override is not None:
                return self._message_override
        return self.get_message()

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
                msg = self._current_message()
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
                msg = self._current_message()
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
        # so logs are self-explanatory. ``_extract_zip_with_retry``
        # handles transient rclone/OneDrive FUSE I/O errors with
        # exponential backoff and a local-copy fallback so a brief
        # rclone blip no longer fails the whole book.
        with _StepLine(f"Extracting {zip_path.name}") as step:
            _extract_zip_with_retry(zip_path, temp_extract, log_path, step)

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
        metadata_object = json.loads(meta_text)
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

        if ffmpeg.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed with exit code {ffmpeg.returncode}"
            )

        valid, validation_errors = validate_m4b(output_file, metadata_object)
        if not valid:
            reason = "; ".join(validation_errors)
            _write_log(f"Validation failed for {output_file}: {reason}", log_path)
            raise RuntimeError(f"Output validation failed: {reason}")
        file_size = output_file.stat().st_size
        _write_log(f"Validated {output_file}", log_path)

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
    log_path = Path(os.environ["LIBBYRIP_LOG_FILE"])
    # Walk AudioBooks once. Repeating rglob once per zip is prohibitively
    # slow on the rclone/FUSE mount, especially as the library grows.
    existing_by_name = {
        candidate.name: candidate
        for candidate in AUDIOBOOKS_DIR.rglob("*.m4b")
        if candidate.is_file()
    }

    def indexed_candidate(basename: str) -> Optional[Path]:
        candidate = existing_by_name.get(f"{basename}.m4b")
        if candidate is not None:
            return candidate
        match = _AUTHOR_TITLE_RE.match(basename)
        if not match:
            return None
        author = clean_name(match.group(1).strip())
        title = clean_name(match.group(2).strip())
        candidate = existing_by_name.get(f"{title}.m4b")
        if candidate is not None and candidate.parent.name == author:
            return candidate
        return None

    for zip_path in zips:
        metadata = _read_zip_metadata(zip_path)
        existing = indexed_candidate(zip_path.stem)
        if metadata is not None:
            creators = _metadata_creators(metadata)
            metadata_output = resolve_output_path(
                SCRIPT_DIR,
                creators["author"] or "Unknown Author",
                str(metadata.get("title", "")) or zip_path.stem,
            )
            if metadata_output.is_file():
                existing = metadata_output
        if existing is None:
            queue.append(zip_path)
            continue

        if metadata is None:
            _write_log(
                f"Requeueing {zip_path.name} - source metadata could not be read",
                log_path,
            )
            queue.append(zip_path)
            continue

        try:
            valid, errors = validate_m4b(existing, metadata)
        except (KeyError, TypeError, ValueError) as exc:
            valid, errors = False, [f"source metadata is malformed: {exc}"]
        if valid:
            _write_log(
                f"Skipping {zip_path.name} - validated existing file at {existing}",
                log_path,
            )
        else:
            _write_log(
                f"Requeueing {zip_path.name} - invalid existing file at {existing}: "
                f"{'; '.join(errors)}",
                log_path,
            )
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
