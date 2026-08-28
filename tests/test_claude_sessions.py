"""Tests for detecting running Claude Code sessions."""

import json
import os
import sys
import time

import pytest

from tzmrit_display.claude_sessions import (
    Session,
    _fmt_age,
    _proc_start,
    _windows_start_ticks,
    list_sessions,
    summarize,
)

LINUX = sys.platform.startswith("linux")


def own_start_token():
    """The procStart value Claude Code would store for this test process."""
    if LINUX:
        return _proc_start(os.getpid())
    return str(_windows_start_ticks(os.getpid())[0])


def write_session(directory, pid, **overrides):
    data = {
        "pid": pid,
        "sessionId": f"sid-{pid}",
        "cwd": "/home/user/dev/example",
        "name": f"session-{pid}",
        "kind": "interactive",
        "status": "idle",
        "statusUpdatedAt": int(time.time() * 1000),
    }
    data.update(overrides)
    (directory / f"{pid}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


class TestProcStart:
    @pytest.mark.skipif(not LINUX, reason="/proc is Linux-only")
    def test_reads_own_start_time(self):
        # Field 22 of /proc/self/stat must be a number
        value = _proc_start(os.getpid())
        assert value is not None and value.isdigit()

    def test_missing_process_returns_none(self):
        assert _proc_start(9_999_999) is None


class TestListSessions:
    def test_reads_live_session(self, tmp_path):
        write_session(tmp_path, os.getpid(), procStart=own_start_token())
        sessions = list_sessions(tmp_path)
        assert len(sessions) == 1
        assert sessions[0].project == "example"

    def test_dead_process_is_skipped(self, tmp_path):
        write_session(tmp_path, 9_999_998, procStart="12345")
        assert list_sessions(tmp_path) == []

    def test_recycled_pid_is_skipped(self, tmp_path):
        """The PID is alive, but it is a different process than back then."""
        write_session(tmp_path, os.getpid(), procStart="999999999")
        assert list_sessions(tmp_path) == []

    def test_entry_without_procstart_is_kept(self, tmp_path):
        # Older formats lacking the field should not silently disappear
        write_session(tmp_path, os.getpid())
        assert len(list_sessions(tmp_path)) == 1

    def test_broken_json_is_ignored(self, tmp_path):
        (tmp_path / "123.json").write_text("{broken", encoding="utf-8")
        write_session(tmp_path, os.getpid(), procStart=own_start_token())
        assert len(list_sessions(tmp_path)) == 1

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert list_sessions(tmp_path / "does-not-exist") == []

    def test_urgent_sessions_come_first(self, tmp_path):
        pid = os.getpid()
        start = own_start_token()
        # All on the same live PID, only the status differs
        for name, status in [("a", "idle"), ("b", "requires_action"), ("c", "busy")]:
            data = {
                "pid": pid, "name": name, "status": status, "cwd": "/x",
                "kind": "interactive", "procStart": start,
                "statusUpdatedAt": int(time.time() * 1000),
            }
            (tmp_path / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
        assert [s.status for s in list_sessions(tmp_path)] == [
            "requires_action", "busy", "idle"]


class TestSessionFields:
    def test_project_is_last_path_element(self):
        assert Session(1, "n", "/home/user/dev/karr/", "idle", "x").project == "karr"

    def test_status_text_is_translated(self):
        assert Session(1, "n", "/x", "requires_action", "y").status_text == "waiting for you"

    def test_unknown_status_passes_through(self):
        assert Session(1, "n", "/x", "compiling", "y").status_text == "compiling"

    def test_waiting_and_working_flags(self):
        assert Session(1, "n", "/x", "requires_action", "y").waiting
        assert Session(1, "n", "/x", "busy", "y").working
        assert not Session(1, "n", "/x", "idle", "y").waiting


class TestFormatting:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"), (45, "45s"), (60, "1m00s"), (252, "4m12s"),
        (3600, "1h00m"), (7300, "2h01m"), (90000, "1d1h"),
    ])
    def test_age_format(self, seconds, expected):
        assert _fmt_age(seconds) == expected

    def test_summary_names_the_urgent_count(self):
        now = time.time()
        sessions = [
            Session(1, "a", "/x", "requires_action", "i", status_since=now),
            Session(2, "b", "/y", "busy", "i", status_since=now),
            Session(3, "c", "/z", "idle", "i", status_since=now),
        ]
        text = summarize(sessions)
        assert "3 sessions" in text and "1 active" in text and "1 waiting" in text

    def test_summary_without_sessions(self):
        assert summarize([]) == "no sessions"


class TestMemory:
    def test_own_process_reports_memory(self):
        from tzmrit_display.claude_sessions import process_memory
        total, children = process_memory(os.getpid(), ttl=0)
        assert total > 0
        assert children >= 0

    def test_dead_process_reports_zero(self):
        from tzmrit_display.claude_sessions import process_memory
        assert process_memory(9_999_997, ttl=0) == (0, 0)

    def test_cache_avoids_repeated_walks(self):
        from tzmrit_display import claude_sessions as cs
        cs._mem_cache.clear()
        first = cs.process_memory(os.getpid())
        cs._mem_cache[os.getpid()] = (cs.time.monotonic(), 12345, 7)
        assert cs.process_memory(os.getpid()) == (12345, 7)
        assert first[0] > 0

    @pytest.mark.parametrize("value,expected", [
        (0, ""), (-5, ""), (512 * 1024**2, "512 MB"),
        (1024**3, "1.0 GB"), (int(1.75 * 1024**3), "1.8 GB"),
    ])
    def test_memory_format(self, value, expected):
        from tzmrit_display.claude_sessions import fmt_memory
        assert fmt_memory(value) == expected

    def test_summary_includes_total_memory(self):
        sessions = [
            Session(1, "a", "/x", "busy", "i", rss=512 * 1024**2),
            Session(2, "b", "/y", "idle", "i", rss=512 * 1024**2),
        ]
        assert "1.0 GB" in summarize(sessions)

    def test_summary_omits_memory_when_unknown(self):
        sessions = [Session(1, "a", "/x", "idle", "i")]
        assert "GB" not in summarize(sessions) and "MB" not in summarize(sessions)


class TestPlatformPortability:
    """The panel protocol is portable; only these probes are Linux-specific."""

    def test_proc_start_returns_none_off_linux(self, monkeypatch):
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", False)
        assert cs._proc_start(os.getpid()) is None

    def test_liveness_falls_back_to_psutil_when_unparseable(self, monkeypatch):
        """A procStart in an unknown format must be ignored rather than
        compared, otherwise every session would look dead."""
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", False)
        assert cs._is_live(os.getpid(), "some-unknown-format")
        assert not cs._is_live(9_999_996, "some-unknown-format")

    def test_session_listing_works_without_platform_probe(self, tmp_path, monkeypatch):
        """On a platform with neither /proc nor the ticks check (say macOS)
        the value is ignored entirely."""
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", False)
        monkeypatch.setattr(cs, "WINDOWS", False)
        write_session(tmp_path, os.getpid(), procStart="131234567890000000")
        sessions = cs.list_sessions(tmp_path)
        assert len(sessions) == 1, "a live session must not vanish off Linux"

    def test_recycled_pid_detection_on_linux(self, tmp_path, monkeypatch):
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", True)
        write_session(tmp_path, os.getpid(), procStart="999999999")
        assert cs.list_sessions(tmp_path) == []


class TestWindowsTicks:
    """procStart on Windows: .NET local-time ticks of the process start.

    _windows_start_ticks only uses psutil, so these run on every platform
    with WINDOWS monkeypatched on.
    """

    def test_matching_ticks_keep_the_session(self, tmp_path, monkeypatch):
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", False)
        monkeypatch.setattr(cs, "WINDOWS", True)
        local_ticks = cs._windows_start_ticks(os.getpid())[0]
        write_session(tmp_path, os.getpid(), procStart=str(local_ticks))
        assert len(cs.list_sessions(tmp_path)) == 1

    def test_utc_reading_is_accepted_too(self, monkeypatch):
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", False)
        monkeypatch.setattr(cs, "WINDOWS", True)
        utc_ticks = cs._windows_start_ticks(os.getpid())[1]
        assert cs._is_live(os.getpid(), str(utc_ticks))

    def test_recycled_pid_is_dropped(self, tmp_path, monkeypatch):
        """A parseable but wrong tick count means: different process."""
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", False)
        monkeypatch.setattr(cs, "WINDOWS", True)
        write_session(tmp_path, os.getpid(), procStart="131234567890000000")
        assert cs.list_sessions(tmp_path) == []

    def test_dead_process_is_dropped(self, monkeypatch):
        from tzmrit_display import claude_sessions as cs
        monkeypatch.setattr(cs, "LINUX", False)
        monkeypatch.setattr(cs, "WINDOWS", True)
        assert not cs._is_live(9_999_995, "639235445713023500")
