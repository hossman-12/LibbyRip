"""Re-save the Python scripts in this directory with Unix LF line endings.

The scripts were created on Windows and inherited CRLF line endings, which
causes the shebang to look like ``#!/usr/bin/env python3\\r`` on Linux. The
kernel then tries to exec ``/usr/bin/env\\r`` and reports
``bad interpreter: Permission denied`` even though the shebang is correct.

Re-saving as LF fixes the shebang and matches the convention on Linux /
Debian. Python source code is line-ending agnostic, so this is safe on
Windows too.

Run this once after cloning or pulling the repo on a Linux box:

    python3 fix_line_endings.py

Or run it from any platform that has Python 3 installed. It is a no-op on
files that are already LF-only.
"""
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TARGETS = [
    "auto_build_m4b.py",
    "bakeMetadata.py",
    "buildChapters.py",
    "convertToM4b.py",
    "log_helper.py",
]


def main() -> int:
    converted = 0
    for name in TARGETS:
        path = SCRIPT_DIR / name
        if not path.is_file():
            print(f"  skip (not found): {name}")
            continue
        raw = path.read_bytes()
        if b"\r\n" not in raw:
            print(f"  already LF:       {name}")
            continue
        new = raw.replace(b"\r\n", b"\n")
        path.write_bytes(new)
        converted += 1
        print(f"  converted CRLF->LF: {name}")
    print(f"\n{converted} file(s) converted to LF line endings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
