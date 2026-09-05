#!/usr/bin/env python3
import os, subprocess, sys

# Shared logging helper. See log_helper.py for details.
from log_helper import log as _log

SCRIPT_NAME = "convertToM4b.py"

try:
    subprocess.run(("ffmpeg", "-version"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except FileNotFoundError as _:
    msg = "Error: FFmpeg not found, please install it on your system to continue: https://www.ffmpeg.org/download.html"
    _log(SCRIPT_NAME, msg)
    print(msg)
    exit(1)

path = input("MP3 path: ")
_log(SCRIPT_NAME, f"input MP3 path: {path}")

if not os.path.exists(path):
    msg = "File not found"
    _log(SCRIPT_NAME, msg)
    print(msg)
    exit(1)
if not(path.endswith(".mp3") or path.endswith(".MP3")):
    msg = "File MUST be an mp3 file to continue"
    _log(SCRIPT_NAME, msg)
    print(msg)
    exit(1)

outPath = path[:-4] + ".m4b"
_log(SCRIPT_NAME, f"output M4B path: {outPath}")
_log(SCRIPT_NAME, "invoking ffmpeg for single-file conversion")

# Pipe ffmpeg's stdout/stderr through tee-style logging: every line emitted by
# ffmpeg is captured and re-logged under convertToM4b.py so it lands in the
# shared log file with the proper [date stamp] [script] header.
proc = subprocess.Popen([
    "ffmpeg", "-i", path, "-c:a", "aac", "-b:a", "128k", "-vn",
    "-map_metadata", "0", "-map_chapters", "0", "-f", "ipod", outPath
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
_stdout, stderr = proc.communicate()
if _stdout:
    for line in _stdout.splitlines():
        if line:
            _log("ffmpeg", line)
if stderr:
    for line in stderr.splitlines():
        if line:
            _log("ffmpeg", line)
_log(SCRIPT_NAME, f"ffmpeg exit code: {proc.returncode}")
sys.exit(proc.returncode)

