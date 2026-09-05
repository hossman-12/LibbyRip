#!/usr/bin/env python3
"""Shared logging helper for LibbyRip scripts.

Each script uses ``log(script_name, message)`` to write a single line to the
shared log file in the format::

    [YYYY-MM-DD HH:MM:SS] [script_name] message

The destination file is taken from the ``LIBBYRIP_LOG_FILE`` environment
variable (set by ``auto_built-m4b.ps1`` before it invokes any Python script).
If that variable is not set, output falls back to ``stderr`` with the same
format so that running the script directly still produces useful diagnostics.

Multi-line messages are split so that every line in the log file gets its own
``[date stamp] [script]`` header. This is required because the user wants to
be able to grep / filter the log by line.
"""
import logging
import os
import sys


_FORMAT = "[%(asctime)s] [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _build_formatter():
    return logging.Formatter(_FORMAT, datefmt=_DATEFMT)


def get_logger(script_name):
    """Return a logger configured to write to ``LIBBYRIP_LOG_FILE`` (or stderr).

    The same logger is returned on every call for a given ``script_name`` so
    that we do not pile up duplicate handlers if the script imports this
    module more than once or logs repeatedly.
    """
    logger = logging.getLogger(script_name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = _build_formatter()

    log_file = os.environ.get("LIBBYRIP_LOG_FILE")
    if log_file:
        try:
            handler = logging.FileHandler(log_file, encoding="utf-8")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except Exception:
            # If the file cannot be opened (locked, bad path, etc.) fall
            # through to the stderr handler so the message is not lost.
            log_file = None

    if not log_file:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    # Prevent records from bubbling up to the root logger and being emitted
    # again with the default "levelname: message" format.
    logger.propagate = False
    return logger


def log(script_name, message):
    """Log a single message under the given script name.

    Multi-line messages are split so that every line in the log file carries
    the ``[date stamp] [script]`` header. Blank lines are skipped.
    """
    if message is None:
        return
    logger = get_logger(script_name)
    for line in str(message).splitlines() or [""]:
        logger.info(line)


def attach_external_logger(external_logger, display_name, level=logging.INFO):
    """Configure an external library logger (e.g. ``eyed3.log``) to also write
    to ``LIBBYRIP_LOG_FILE`` using the same format and a custom script tag.

    This is used by ``bakeMetadata.py`` so that warnings from ``eyed3`` (such
    as the "Lame tag CRC check failed" message) end up in the shared log file
    tagged with the calling script's name.
    """
    log_file = os.environ.get("LIBBYRIP_LOG_FILE")
    if not log_file:
        return

    class _NameOverrideFormatter(logging.Formatter):
        def __init__(self, override_name, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._override_name = override_name

        def format(self, record):
            record.name = self._override_name
            return super().format(record)

    try:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(_NameOverrideFormatter(
            display_name, _FORMAT, datefmt=_DATEFMT
        ))
        external_logger.addHandler(handler)
        if external_logger.level == logging.NOTSET or external_logger.level > level:
            external_logger.setLevel(level)
    except Exception:
        pass
