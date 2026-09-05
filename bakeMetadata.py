#!/usr/bin/env python3
import sys
import os
import json
import eyed3
import mimetypes
import argparse
import logging
from eyed3.id3 import ID3_V2_4

# PyQt5 is only needed for the optional GUI mode. The CLI path (used by
# auto_build_m4b.py / auto_built-m4b.ps1) does not touch Qt, so we import
# it lazily inside main() so this script can run on a headless / Linux box
# that has only the headless dependencies installed.

# Shared logging helper. When this script is invoked from auto_built-m4b.ps1
# the LIBBYRIP_LOG_FILE environment variable points at the shared log file;
# otherwise log lines are written to stderr with the same format.
from log_helper import log as _log, attach_external_logger

SCRIPT_NAME = "bakeMetadata.py"

# Custom logging handler to forward logs to the GUI status window.
class GuiLogHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        try:
            msg = self.format(record)
            self.callback(msg)
        except Exception:
            self.handleError(record)

def bake_metadata(workingDir, progress_callback=None):
    _log(SCRIPT_NAME, f"bake_metadata start: workingDir={workingDir}")
    if workingDir.startswith("'"):
        workingDir = workingDir[1:-1]
    # Sort the working list alphabetically
    workingList = sorted(os.listdir(workingDir))
    _log(SCRIPT_NAME, f"found {len(workingList)} entries in workingDir")

    if "metadata" not in workingList:
        error_msg = "ERROR: Working directory MUST contain a metadata directory. Remember to click the 'Export audiobook' button in the website!"
        if progress_callback:
            progress_callback(error_msg, 0)
        _log(SCRIPT_NAME, error_msg)
        raise FileNotFoundError(error_msg)

    cover = [f for f in os.listdir(os.path.join(workingDir, "metadata")) if f.startswith("cover")]
    if len(cover) != 1:
        error_msg = "ERROR: Cover art not found"
        if progress_callback:
            progress_callback(error_msg, 0)
        _log(SCRIPT_NAME, error_msg)
        raise FileNotFoundError(error_msg)

    cover_name = cover[0]
    _log(SCRIPT_NAME, f"cover image: {cover_name}")
    with open(os.path.join(workingDir, "metadata", cover_name), "rb") as f:
        coverBytes = f.read()
    coverMime = mimetypes.guess_type(cover_name)[0]

    # Open JSON file with UTF-8 encoding
    metadata_path = os.path.join(workingDir, "metadata", "metadata.json")
    _log(SCRIPT_NAME, f"loading metadata: {metadata_path}")
    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    authorName = "Unknown"
    for creator in metadata["creator"]:
        if "author" in creator["role"]:
            authorName = creator["name"]
    _log(SCRIPT_NAME, f"author: {authorName}")
    _log(SCRIPT_NAME, f"title: {metadata.get('title', '<none>')}")
    _log(SCRIPT_NAME, f"spines: {len(metadata.get('spine', []))}; chapters: {len(metadata.get('chapters', []))}")

    chapters = {}
    for chap in metadata["chapters"]:
        chapters.setdefault(chap["spine"], []).append(chap)
    # Sort each spine's chapters by offset so out-of-order metadata still bakes correctly
    for spine_index in chapters:
        chapters[spine_index].sort(key=lambda c: c["offset"])
    # If two chapters share the same offset (e.g. an "Introduction" track layered on
    # the first real chapter of a part), nudge the later one forward by 1 ms so the
    # list is strictly monotonic. The nudge is in integer milliseconds to match
    # the units used by the chapter-write loop below; otherwise `int(offset*1000)`
    # would still truncate two equal offsets to the same integer and the chapter
    # end time `next_offset_ms - 1` would go negative.
    for spine_index, spine_chapters in chapters.items():
        prev_ms = -1
        for chap in spine_chapters:
            cur_ms = int(chap["offset"] * 1000)
            if cur_ms <= prev_ms:
                prev_ms += 1
                chap["offset"] = prev_ms / 1000.0
            else:
                prev_ms = cur_ms

    # Collect all parts to process
    parts = [file for file in workingList if file.startswith("Part ")]
    total = len(parts)
    if total == 0:
        error_msg = "ERROR: No parts found to process."
        if progress_callback:
            progress_callback(error_msg, 0)
        _log(SCRIPT_NAME, error_msg)
        raise FileNotFoundError(error_msg)

    _log(SCRIPT_NAME, f"processing {total} parts")

    for index, file in enumerate(parts):
        number = file[len("Part "):].split(".")[0]
        spine_index = int(number) - 1
        _log(SCRIPT_NAME, f"begin part {number} (spine {spine_index})")

        audiofile = eyed3.load(os.path.join(workingDir, file))
        if audiofile.tag is None:
            # explizit ID3v2.4 initialisieren (UTF-8)
            audiofile.initTag(version=ID3_V2_4)
        else:
            audiofile.tag.clear()

        # make that we work with V2.4 and UTF-8
        audiofile.tag.version = ID3_V2_4

        audiofile.tag.title = f"Part {int(number)}"
        audiofile.tag.artist = authorName
        audiofile.tag.images.set(3, coverBytes, coverMime)
        audiofile.tag.album = metadata["title"]
        audiofile.tag.track_num = (int(number), len(metadata["spine"]))

        # Get chapters for this spine index
        spine_chapters = chapters.get(spine_index, [])
        if spine_chapters:
            # Check for non-increasing offsets
            prev_offset = spine_chapters[0]["offset"]
            for i,chap in enumerate(spine_chapters[1:], start=1):
                current_offset = chap["offset"]
                if current_offset <= prev_offset:
                    # Found overlapping chapter - report and exit
                    error_parts = [
                        f"ERROR: {os.path.join(workingDir, 'metadata', 'metadata.json')} has errors with overlapping chapter times.",
                        f"Look in chapters for spine: {spine_index}",
                        f"Chapter '{spine_chapters[i-1]['title']}' at offset: {spine_chapters[i-1]['offset']}",
                        f"Chapter '{chap['title']}' at offset: {current_offset}"
                    ]

                    # Format for current mode
                    if progress_callback and hasattr(progress_callback, '__self__'):
                        error_msg = "<br>".join(error_parts)
                    else:
                        error_msg = "\n".join(error_parts)

                    # Only send via exception, not progress callback
                    _log(SCRIPT_NAME, error_msg)
                    raise ValueError(error_msg)
                prev_offset = current_offset

        # Process chapters normally since offsets are valid
        last = None
        child_ids = []

        for i, chap in enumerate(chapters.get(int(number) - 1, [])):
            if last is None:
                last = chap
                continue
            cid = f"ch{i}".encode("ascii")
            child_ids.append(cid)
            start_ms = int(last["offset"] * 1000)
            end_ms = max(start_ms + 1, int(chap["offset"] * 1000) - 1)
            c = audiofile.tag.chapters.set(
                cid,
                (start_ms, end_ms)
            )
            c.title = last["title"]
            last = chap

        if last is not None:
            c = audiofile.tag.chapters.set(
                b"last",
                (
                    int(last["offset"]) * 1000,
                    int(metadata["spine"][chap["spine"]]["duration"] * 1000)
                )
            )
            c.title = last["title"]

        audiofile.tag.table_of_contents.set(
            b"toc",
            toplevel=True,
            child_ids=child_ids + [b"last"],
            description="Table of Contents"
        )

        # als v2.4 speichern, damit UTF-8 Zeichen (Umlaute) korrekt sind
        audiofile.tag.save(version=ID3_V2_4)
        _log(SCRIPT_NAME, f"saved part {number}")

        # Update progress
        if progress_callback:
            progress = int((index + 1) * 100 / total)
            progress_callback(f"Baked: {file}", progress)

    _log(SCRIPT_NAME, f"bake_metadata complete: {total} parts processed")

# Worker class to run bake_metadata in a separate thread.
# All Qt-using code (Worker, MetadataBakerApp, and the GUI branch of main) is
# defined inside a function so the import of PyQt5 can be lazy. The CLI path
# (used by auto_build_m4b.py / auto_built-m4b.ps1) does not need PyQt5 at all
# and must remain importable on a headless Linux box.
def _build_gui_app():
    # Import PyQt5 only when we actually need the GUI. This keeps the headless
    # CLI / Linux path free of the PyQt5 requirement.
    from PyQt5 import QtWidgets, QtCore

    class Worker(QtCore.QObject):
        progress = QtCore.pyqtSignal(str, int)
        finished = QtCore.pyqtSignal()
        error = QtCore.pyqtSignal(str)

        def __init__(self, path):
            super().__init__()
            self.path = path

        def run(self):
            try:
                bake_metadata(self.path, progress_callback=self.progress.emit)
            except Exception as e:
                # Format for GUI if needed
                if "\n" in str(e):
                    msg = str(e).replace("\n", "<br>")
                else:
                    msg = str(e)
                self.error.emit(msg)
            finally:
                self.finished.emit()

    class MetadataBakerApp(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Bake Metadata")
            self.resize(500, 300)
            self.init_ui()

        def init_ui(self):
            layout = QtWidgets.QVBoxLayout()

            # Directory selection
            dir_layout = QtWidgets.QHBoxLayout()
            self.dirInput = QtWidgets.QLineEdit()
            self.browseBtn = QtWidgets.QPushButton("Browse")
            dir_layout.addWidget(self.dirInput)
            dir_layout.addWidget(self.browseBtn)

            # Run button
            self.runBtn = QtWidgets.QPushButton("Run Bake")

            # Progress bar
            self.progressBar = QtWidgets.QProgressBar()
            self.progressBar.setRange(0, 100)
            self.progressBar.setValue(0)

            # Log output
            self.logOutput = QtWidgets.QTextEdit()
            self.logOutput.setReadOnly(True)
            self.logOutput.setAcceptRichText(True)

            layout.addLayout(dir_layout)
            layout.addWidget(self.runBtn)
            layout.addWidget(self.progressBar)
            layout.addWidget(self.logOutput)
            self.setLayout(layout)

            self.browseBtn.clicked.connect(self.select_dir)
            self.runBtn.clicked.connect(self.run_bake)

        def select_dir(self):
            dir_path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Audiobook Directory")
            if dir_path:
                self.dirInput.setText(dir_path)

        def run_bake(self):
            path = self.dirInput.text().strip()
            self.logOutput.clear()

            if not os.path.isdir(path):
                self.logOutput.append("<span style='color:red;'>Invalid directory path.</span>")
                return

            self.progressBar.setValue(0)
            self.runBtn.setEnabled(False)
            self.browseBtn.setEnabled(False)

            self.thread = QtCore.QThread()
            self.worker = Worker(path)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.progress.connect(self.update_status)
            self.worker.error.connect(self.report_error)
            self.worker.finished.connect(self.process_finished)
            self.worker.finished.connect(self.thread.quit)
            self.worker.finished.connect(self.worker.deleteLater)
            self.thread.finished.connect(self.thread.deleteLater)
            self.thread.start()

        def update_status(self, message, progress):
            # Only display non-error messages here
            if not message.startswith("ERROR"):
                # Convert newlines to HTML line breaks for GUI
                if "\n" in message:
                    message = message.replace("\n", "<br>")
                self.logOutput.append(message)
            self.progressBar.setValue(progress)

        def report_error(self, error_message):
            # Ensure error messages are properly formatted for GUI
            if "\n" in error_message:
                error_message = error_message.replace("\n", "<br>")
            self.logOutput.append(f"<span style='color:red;'>{error_message}</span>")

        def process_finished(self):
            self.logOutput.append("Processing complete.")
            self.runBtn.setEnabled(True)
            self.browseBtn.setEnabled(True)

    return QtWidgets, QtCore, MetadataBakerApp

# -------- MAIN ENTRY POINT --------

def main():
    parser = argparse.ArgumentParser(description="Bake audiobook metadata.")
    parser.add_argument("directory", nargs="?", default=None, help="Path to audiobook directory")
    parser.add_argument("--gui", action="store_true", help="Run in GUI mode")
    args = parser.parse_args()

    def cli_callback(message, progress):
        # Only display non-error messages here
        if not message.startswith("ERROR"):
            print(f"{message} ({progress}%)")

    if args.gui:
        try:
            QtWidgets, QtCore, MetadataBakerApp = _build_gui_app()
        except ImportError as e:
            print(
                "ERROR: PyQt5 is required for GUI mode. Install it with "
                "`pip install PyQt5` or run this script without --gui.",
                file=sys.stderr,
            )
            raise SystemExit(1) from e
        app = QtWidgets.QApplication(sys.argv)
        window = MetadataBakerApp()
        if args.directory:
            window.dirInput.setText(args.directory)

        def gui_log_callback(message, progress=0):
            # Only display non-error messages here
            if not message.startswith("ERROR"):
                QtCore.QMetaObject.invokeMethod(
                    window.logOutput,
                    "append",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, message)
                )

        gui_handler = GuiLogHandler(lambda msg: gui_log_callback(msg))
        gui_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        gui_handler.setLevel(logging.WARNING)
        eyed3.log.addHandler(gui_handler)
        eyed3.log.propagate = False

        window.show()
        sys.exit(app.exec_())
    else:
        # Also forward eyed3's logger to the shared log file (if configured) so
        # warnings such as "Lame tag CRC check failed" land in the same place as
        # the rest of our script output.
        attach_external_logger(eyed3.log, SCRIPT_NAME)
        if args.directory:
            workingDir = args.directory
        else:
            workingDir = input("Path to audiobook dir: ").strip()
        _log(SCRIPT_NAME, f"CLI mode invoked with workingDir={workingDir}")
        try:
            bake_metadata(workingDir, progress_callback=cli_callback)
            _log(SCRIPT_NAME, "CLI run completed successfully")
        except Exception as e:
            _log(SCRIPT_NAME, f"CLI run failed: {e}")
            print(f"\033[91m{e}\033[0m")
            sys.exit(1)

if __name__ == "__main__":
    main()
