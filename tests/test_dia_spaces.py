"""Dia spaces and pinned tabs read live over AppleScript."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

US = "\x1f"
RS = "\x1e"


def _row(space, pinned, url, title):
    return US.join([space, "true" if pinned else "false", url, title])


LIVE_OUTPUT = RS.join([
    _row("Casual", True, "https://music.example.com/", "Music"),
    _row("Work", True, "https://work.example.com/", "Tracker"),
    _row("Work", False, "https://example.com/project", "project\twith a tab"),
    _row("Work", True, "https://work.example.com/", "Tracker"),
    _row("Work", False, "https://example.com/project", "duplicate from window 2"),
    _row("Work", False, "https://example.com/second-window", "Second window"),
    _row("School", True, "chrome://newtab/", "New Tab"),
    _row("School", True, "https://draw.example.com/", "Draw"),
])

LIVE_SPACE_NAMES = ["Casual", "Work", "School"]


@pytest.fixture
def dia_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = tmp_path / "Library/Application Support/Dia/User Data/Default"
    profile.mkdir(parents=True)
    (profile / "History").write_bytes(b"")
    (profile / "Bookmarks").write_text(json.dumps({
        "roots": {
            "bookmark_bar": {
                "type": "folder",
                "name": "Bookmarks bar",
                "children": [
                    {"type": "url", "name": "Zen", "url": "https://zen-browser.app/"},
                ],
            },
        },
    }))
    return tmp_path


def test_parse_live_spaces_groups_tabs_by_space_in_order():
    from extractors.dia import parse_live_spaces

    spaces = parse_live_spaces(LIVE_OUTPUT)

    assert [s.space_name for s in spaces] == LIVE_SPACE_NAMES
    work = spaces[1]
    assert [t.url for t in work.pinned_tabs] == ["https://work.example.com/"]
    assert [t.url for t in work.open_tabs] == [
        "https://example.com/project",
        "https://example.com/second-window",
    ]
    assert work.open_tabs[0].title == "project\twith a tab"


def test_parse_live_spaces_drops_internal_urls():
    from extractors.dia import parse_live_spaces

    school = parse_live_spaces(LIVE_OUTPUT)[2]

    assert [t.url for t in school.pinned_tabs] == ["https://draw.example.com/"]


def test_parse_live_spaces_gives_stable_distinct_space_ids():
    from extractors.dia import parse_live_spaces

    first = [s.space_id for s in parse_live_spaces(LIVE_OUTPUT)]
    second = [s.space_id for s in parse_live_spaces(LIVE_OUTPUT)]

    assert first == second
    assert len(set(first)) == len(first)


def test_parse_live_spaces_handles_empty_output():
    from extractors.dia import parse_live_spaces

    assert parse_live_spaces("") == []
    assert parse_live_spaces("\n") == []


def test_extract_uses_live_spaces_while_dia_runs(dia_home, monkeypatch):
    from extractors import DiaExtractor

    ext = DiaExtractor()
    monkeypatch.setattr(ext, "is_running", lambda: True)
    monkeypatch.setattr(ext, "_run_live_script", lambda: LIVE_OUTPUT)

    data = ext.extract()

    assert [s.space_name for s in data.spaces] == LIVE_SPACE_NAMES
    assert sum(len(s.pinned_tabs) for s in data.spaces) == 3
    assert [t.url for t in data.profile_bookmarks] == ["https://zen-browser.app/"]
    assert all(s.bookmarks == [] for s in data.spaces)


def test_extract_reuses_snapshot_after_quit(dia_home, monkeypatch):
    from extractors import DiaExtractor

    ext = DiaExtractor()
    running = {"value": True}
    monkeypatch.setattr(ext, "is_running", lambda: running["value"])
    monkeypatch.setattr(ext, "_run_live_script", lambda: LIVE_OUTPUT)
    ext.extract()

    running["value"] = False
    monkeypatch.setattr(ext, "_run_live_script", lambda: pytest.fail("Dia is not running"))
    data = ext.extract()

    assert [s.space_name for s in data.spaces] == LIVE_SPACE_NAMES


def test_quit_snapshots_spaces_before_quitting(dia_home, monkeypatch):
    from extractors import DiaExtractor
    from extractors.chromium import ChromiumExtractor

    ext = DiaExtractor()
    running = {"value": True}
    monkeypatch.setattr(ext, "is_running", lambda: running["value"])
    monkeypatch.setattr(ext, "_run_live_script", lambda: LIVE_OUTPUT)

    def fake_quit(self):
        running["value"] = False
        return {"ok": True, "running": False, "elapsed": 0.0}

    monkeypatch.setattr(ChromiumExtractor, "quit", fake_quit)
    assert ext.quit()["ok"]

    data = ext.extract()
    assert [s.space_name for s in data.spaces] == LIVE_SPACE_NAMES


def test_extract_falls_back_to_session_when_live_read_fails(dia_home, monkeypatch):
    from extractors import DiaExtractor

    ext = DiaExtractor()
    ext.live_read_retry_delay = 0
    calls = []
    monkeypatch.setattr(ext, "is_running", lambda: True)
    monkeypatch.setattr(ext, "_run_live_script", lambda: calls.append(1))

    data = ext.extract()

    assert len(calls) == 1
    assert [s.space_name for s in data.spaces] == ["Default"]


def test_live_spaces_do_not_mirror_pinned_tabs_into_bookmarks(dia_home, monkeypatch):
    from extractors import DiaExtractor

    (dia_home / "Library/Application Support/Dia/User Data/Default/Bookmarks").unlink()
    ext = DiaExtractor()
    monkeypatch.setattr(ext, "is_running", lambda: True)
    monkeypatch.setattr(ext, "_run_live_script", lambda: LIVE_OUTPUT)

    legacy = ext.extract().to_legacy_dict()

    assert all(not s.get("bookmarks") for s in legacy["spaces"])


def test_extract_waits_for_dia_to_restore_its_windows(dia_home, monkeypatch):
    from extractors import DiaExtractor

    ext = DiaExtractor()
    ext.live_read_retry_delay = 0
    outputs = iter(["", "", LIVE_OUTPUT])
    monkeypatch.setattr(ext, "is_running", lambda: True)
    monkeypatch.setattr(ext, "_run_live_script", lambda: next(outputs))

    data = ext.extract()

    assert [s.space_name for s in data.spaces] == LIVE_SPACE_NAMES


def test_extract_gives_up_after_bounded_retries(dia_home, monkeypatch):
    from extractors import DiaExtractor

    ext = DiaExtractor()
    ext.live_read_retry_delay = 0
    calls = []
    monkeypatch.setattr(ext, "is_running", lambda: True)
    monkeypatch.setattr(ext, "_run_live_script", lambda: calls.append(1) or "")

    data = ext.extract()

    assert len(calls) == ext.live_read_attempts
    assert [s.space_name for s in data.spaces] == ["Default"]


def test_parse_live_spaces_keeps_a_last_tab_with_an_empty_title():
    from extractors.dia import parse_live_spaces

    output = _row("Solo", False, "https://example.com/loading", "") + "\n"

    spaces = parse_live_spaces(output)

    assert [t.url for s in spaces for t in s.open_tabs] == ["https://example.com/loading"]


def test_failed_live_read_after_running_drops_the_previous_snapshot(dia_home, monkeypatch):
    from extractors import DiaExtractor

    ext = DiaExtractor()
    ext.live_read_retry_delay = 0
    monkeypatch.setattr(ext, "is_running", lambda: True)
    monkeypatch.setattr(ext, "_run_live_script", lambda: LIVE_OUTPUT)
    ext.extract()

    monkeypatch.setattr(ext, "_run_live_script", lambda: "")
    data = ext.extract()

    assert [s.space_name for s in data.spaces] == ["Default"]


def test_repicking_the_same_source_keeps_its_snapshot():
    from app.bridge import Bridge

    bridge = Bridge()
    bridge.set_source("dia")
    first = bridge.orchestrator.source
    bridge.set_source("dia")
    assert bridge.orchestrator.source is first

    bridge.set_source("chrome")
    assert bridge.orchestrator.source.name == "chrome"


def test_parse_live_spaces_tolerates_separators_stripped_by_the_script():
    from extractors.dia import parse_live_spaces

    output = _row("Space", False, "https://example.com/a", "Title one") + RS + "\n"

    assert [t.title for s in parse_live_spaces(output) for t in s.open_tabs] == ["Title one"]


def test_live_script_replaces_separator_characters_in_text():
    from extractors.dia import _LIVE_SCRIPT

    assert "on clean(" in _LIVE_SCRIPT
    assert "my clean(name of p" in _LIVE_SCRIPT
    assert "my clean(tabTitle as text)" in _LIVE_SCRIPT


def test_snapshot_is_dropped_when_dia_ran_again_after_quitting(dia_home, monkeypatch):
    import os
    import time as time_module

    from extractors import DiaExtractor
    from extractors.chromium import ChromiumExtractor

    session_dir = dia_home / "Library/Application Support/Dia/User Data/Default/Sessions"
    session_dir.mkdir()
    session = session_dir / "Session_1"
    session.write_bytes(b"SNSS")

    ext = DiaExtractor()
    running = {"value": True}
    monkeypatch.setattr(ext, "is_running", lambda: running["value"])
    monkeypatch.setattr(ext, "_run_live_script", lambda: LIVE_OUTPUT)

    def fake_quit(self):
        running["value"] = False
        return {"ok": True, "running": False, "elapsed": 0.0}

    monkeypatch.setattr(ChromiumExtractor, "quit", fake_quit)
    ext.quit()
    assert [s.space_name for s in ext.extract().spaces] == LIVE_SPACE_NAMES

    later = time_module.time() + 120
    os.utime(session, (later, later))

    assert [s.space_name for s in ext.extract().spaces] == ["Default"]
