from dataclasses import dataclass
from datetime import timedelta
import json
import re
import sys
from typing import Any, List

# Shared logging helper. The shared log file is configured by setting the
# LIBBYRIP_LOG_FILE environment variable (normally done by
# auto_built-m4b.ps1). Without that variable, log lines go to stderr in the
# same format so this script remains useful when run by hand.
from log_helper import log as _log


@dataclass(frozen=True)
class Chapter:
    title: str
    "The title of the chapter"

    total_offset: timedelta
    "The offset from the start of the audiobook at which the chapter begins"


@dataclass(frozen=True)
class Metadata:
    title: str
    "The title of the audiobook"

    author: str | None
    "The author of the audiobook, if available"

    narrator: str | None
    "The narrator of the audiobook, if available"

    total_duration: timedelta
    "The total length of the audiobook"

    chapters: List[Chapter]
    "A list of all the chapters in the audiobook"

    def from_json(metadata: Any) -> 'Metadata':
        """Extracts a list of chapters from raw Libby metadata

        Given libby metadata in the form of a JSON object parsed from
        metadata.json, produce a list of Chapters.
        """
        spines = metadata[
            "spine"
        ]  # Array of objects with the properties duration, type, bitrate
        chapters = metadata[
            "chapters"
        ]  # Array of objects with the properties title, spine, offset

        spine_offsets = [
            sum(spine["duration"] for spine in spines[:spine_index])
            for spine_index in range(len(spines))
        ]

        # Sort chapters by (spine, offset) before building the global chapter list.
        # Some Libby exports deliver chapters in an order that does not match their
        # offsets (e.g. a track that is actually chapter N+1 may appear with the
        # offset of chapter N). Without this sort, the next-chapter / end-time
        # pairing below can produce an END that is before START, which FFmpeg
        # rejects when loading the ffmetadata file.
        chapters = sorted(chapters, key=lambda c: (c["spine"], c["offset"]))

        # Nudge chapters that share an exact (spine, offset) with an earlier one
        # forward by 1 ms so the generated list is strictly monotonic in time.
        # This matches the same correction done by bakeMetadata.py for the
        # per-part ID3 chapter tags.
        prev_total_ms: float = -1.0
        for chapter in chapters:
            total_ms = (chapter["offset"] + spine_offsets[chapter["spine"]]) * 1000.0
            if total_ms <= prev_total_ms:
                prev_total_ms += 1.0
                chapter["offset"] = (prev_total_ms / 1000.0) - spine_offsets[chapter["spine"]]
            else:
                prev_total_ms = total_ms

        chapters = [
            Chapter(
                chapter["title"],
                timedelta(
                    seconds=chapter["offset"] + spine_offsets[chapter["spine"]]
                ),
            )
            for chapter in chapters
        ]

        contributors = {
            creator["role"]: creator["name"]
            for creator in metadata["creator"]
        }
        
        author = contributors.get("author")
        narrator = contributors.get("narrator")

        # New combined key
        both = contributors.get("author and narrator")

        if both:
            # Only fill missing old fields, don't overwrite explicit values
            if not author:
                author = both
            if not narrator:
                narrator = both


        return Metadata(
            title=metadata["title"],
            narrator=narrator,
            author=author,
            total_duration=timedelta(
                seconds=sum(spine["duration"] for spine in spines)
            ),
            chapters=chapters,
        )


def format_timedelta(d):
    "Convert a timedelta into a string in the format used by chapters.txt"
    millis = int(d.microseconds // 1000)
    seconds = int(d.total_seconds() % 60)
    minutes = int((d.total_seconds() // 60) % 60)
    hours = int((d.total_seconds() // 3600))
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


def metadata_to_chapters_txt(metadata: Metadata) -> str:
    "Convert metadata into a string formatted for chapters.txt"
    return "\n".join(
        f"{format_timedelta(chapter.total_offset)} {chapter.title}"
        for chapter in metadata.chapters
    )


ffmetadata_special_characters = re.compile(r"(=|;|#|\\|\n)")


def escape_for_ffmetadata(input: str) -> str:
    "Return a string escaped for use in an ffmetadata file"
    return ffmetadata_special_characters.sub(r"\\\1", input)


def metadata_to_ffmpeg(metadata: Metadata) -> str:
    "Convert metadata into FFMPEG's ffmetadata format"
    def format_chapter_timestamp(chapter):
        return int(
            chapter.total_offset.total_seconds() * 1000
        )
    title_line = f"title={escape_for_ffmetadata(metadata.title)}\n"
    author_line = (
        f"artist={escape_for_ffmetadata(metadata.author)}\n"
        if metadata.author else ""
    )
    chapters_part = "\n".join(
        (
            "[CHAPTER]\n"
            "TIMEBASE=1/1000\n"
            f"START={format_chapter_timestamp(chapter)}\n"
            f"END={format_chapter_timestamp(next_chapter)}\n"
            f"title={escape_for_ffmetadata(chapter.title)}\n"
        )
        for (chapter, next_chapter)
        in zip(
           metadata.chapters,
           metadata.chapters[1:] + [Chapter("END", metadata.total_duration)]
        )
    )
    return f";FFMETADATA1\n{title_line}{author_line}\n{chapters_part}"


if __name__ == "__main__":
    SCRIPT_NAME = "buildChapters.py"
    if len(sys.argv) == 1 or sys.argv[1] == "--chapters":
        format = metadata_to_chapters_txt
        mode_label = "chapters"
    elif sys.argv[1] == "--ffmpeg":
        format = metadata_to_ffmpeg
        mode_label = "ffmpeg"
    else:
        msg = (
            f"Usage: {sys.argv[0]} [--chapters | --ffmpeg]"
            " < metadata.json > chapters.txt"
        )
        _log(SCRIPT_NAME, msg)
        print(msg)
        exit(1)

    _log(SCRIPT_NAME, f"mode={mode_label}; reading metadata.json from stdin")
    raw_metadata = json.load(sys.stdin)
    _log(SCRIPT_NAME, f"loaded metadata: title={raw_metadata.get('title', '<none>')}; "
                       f"spines={len(raw_metadata.get('spine', []))}; "
                       f"chapters={len(raw_metadata.get('chapters', []))}")

    metadata = Metadata.from_json(raw_metadata)
    _log(SCRIPT_NAME, f"parsed {len(metadata.chapters)} chapters; "
                       f"author={metadata.author}; narrator={metadata.narrator}; "
                       f"total_duration={metadata.total_duration}")

    output = format(metadata)
    print(output)
    _log(SCRIPT_NAME, f"wrote {mode_label} output ({len(output)} chars)")
