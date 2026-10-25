"""Dia browser extractor."""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from pathlib import Path

from .base import BrowserExtractorError, ExportData, SpaceRecord, TabRecord
from .chromium import ChromiumExtractor

logger = logging.getLogger(__name__)

_NS_DIA_SPACE = uuid.UUID("7c1f0b52-3d0e-4f7a-9a4e-5f0d6b8e2a91")
_FIELD_SEP = "\x1f"
_ROW_SEP = "\x1e"
_MIGRATABLE_SCHEMES = ("http://", "https://", "ftp://")

_LIVE_SCRIPT = """
on clean(value)
  set AppleScript's text item delimiters to {character id 30, character id 31}
  set parts to text items of value
  set AppleScript's text item delimiters to " "
  set cleaned to parts as text
  set AppleScript's text item delimiters to ""
  return cleaned
end clean

set fieldSep to character id 31
set rowSep to character id 30
set rows to {}
if application "Dia" is running then
  tell application "Dia"
    repeat with w in windows
      repeat with p in profiles of w
        set spaceName to my clean(name of p as text)
        repeat with t in tabs of p
          try
            set tabUrl to URL of t
            set tabTitle to title of t
            if tabUrl is missing value then set tabUrl to ""
            if tabTitle is missing value then set tabTitle to ""
            set tabUrl to tabUrl as text
            set tabTitle to my clean(tabTitle as text)
            set pinnedText to isPinned of t as text
            set end of rows to spaceName & fieldSep & pinnedText & fieldSep & tabUrl & fieldSep & tabTitle
          end try
        end repeat
      end repeat
    end repeat
  end tell
end if
set AppleScript's text item delimiters to rowSep
return rows as text
"""


def _space_id(name: str) -> str:
    return str(uuid.uuid5(_NS_DIA_SPACE, name))


def parse_live_spaces(output: str) -> list[SpaceRecord]:
    order: list[str] = []
    pinned: dict[str, list[TabRecord]] = {}
    opened: dict[str, list[TabRecord]] = {}
    seen: set[tuple[str, str, bool]] = set()

    for row in output.strip("\r\n").split(_ROW_SEP):
        parts = row.split(_FIELD_SEP, 3)
        if len(parts) != 4:
            continue
        space, is_pinned_text, url, title = parts
        is_pinned = is_pinned_text.strip() == "true"
        if space not in pinned:
            order.append(space)
            pinned[space] = []
            opened[space] = []
        key = (space, url, is_pinned)
        if key in seen or not url.lower().startswith(_MIGRATABLE_SCHEMES):
            continue
        seen.add(key)
        target = pinned if is_pinned else opened
        target[space] = [*target[space], TabRecord(url=url, title=title)]

    return [
        SpaceRecord(
            space_id=_space_id(name),
            space_name=name,
            pinned_tabs=pinned[name],
            open_tabs=opened[name],
            bookmarks=[],
            bookmark_folders=[],
        )
        for name in order
        if pinned[name] or opened[name]
    ]


class DiaExtractor(ChromiumExtractor):
    name = "dia"
    display_name = "Dia"

    user_data_dirs_macos = (
        "Library/Application Support/Dia/User Data",
    )

    keychain_service = "Dia Safe Storage"
    keychain_account = "Dia"

    macos_app_name = "Dia"
    macos_process_paths = ("Dia.app/Contents/MacOS/Dia",)

    live_read_attempts = 5
    live_read_retry_delay = 1.0
    session_write_slack = 2.0

    def __init__(self) -> None:
        super().__init__()
        self._live_spaces: list[SpaceRecord] = []
        self._quit_at: float | None = None

    def _run_live_script(self) -> str | None:
        try:
            result = subprocess.run(
                ["osascript", "-e", _LIVE_SCRIPT],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            logger.warning("reading Dia spaces over AppleScript failed: %s", exc)
            return None
        if result.returncode != 0:
            logger.warning("reading Dia spaces over AppleScript failed: %s",
                           result.stderr.strip())
            return None
        return result.stdout

    def _snapshot_live_spaces(self) -> None:
        if not self.is_running():
            return
        self._live_spaces = []
        self._quit_at = None
        for attempt in range(self.live_read_attempts):
            output = self._run_live_script()
            if output is None:
                return
            spaces = parse_live_spaces(output)
            if spaces:
                self._live_spaces = spaces
                return
            if attempt + 1 < self.live_read_attempts:
                time.sleep(self.live_read_retry_delay)
        logger.warning("Dia is running but reported no spaces over AppleScript")

    def _latest_session_write(self) -> float:
        sessions = [
            entry
            for profile in self.profile_paths()
            for entry in (Path(profile) / "Sessions").glob("*")
            if entry.is_file()
        ]
        return max((entry.stat().st_mtime for entry in sessions), default=0.0)

    def _drop_snapshot_if_dia_ran_again(self) -> None:
        if self._quit_at is None or self.is_running():
            return
        if self._latest_session_write() > self._quit_at + self.session_write_slack:
            logger.info("Dia ran again after the spaces were read; dropping the snapshot")
            self._live_spaces = []
            self._quit_at = None

    def quit(self) -> dict:
        self._snapshot_live_spaces()
        result = super().quit()
        if self._live_spaces and not self.is_running():
            self._quit_at = time.time()
        return result

    def extract(self) -> ExportData:
        self._drop_snapshot_if_dia_ran_again()
        self._snapshot_live_spaces()
        if not self._live_spaces:
            return super().extract()
        try:
            session_spaces = super().extract().spaces
        except BrowserExtractorError:
            session_spaces = []

        return ExportData(
            source=self.name,
            spaces=list(self._live_spaces),
            profile_bookmarks=[b for s in session_spaces for b in (s.bookmarks or [])],
            profile_bookmark_folders=[f for s in session_spaces for f in (s.bookmark_folders or [])],
        )
