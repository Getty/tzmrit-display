"""Tests for detecting running Claude Code sessions."""

import json
import os
import sys
import time

import pytest

from tzmrit_display.claude_sessions import (
    WORKING_ACTIVE_WINDOW,
    Session,
    _fmt_age,
    _fmt_inactive,
    _MODEL_TAIL,
    _model_label,
    _proc_start,
    _transcript_mtime,
    _transcript_model,
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


def assistant_line(model, sidechain=False):
    """One transcript record in the shape Claude Code writes it.

    Verified against a live transcript: the model sits in `message.model` of an
    `assistant` record, and `isSidechain` marks a subagent turn.
    """
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"role": "assistant", "model": model}}


def write_transcript(projects, session_id, records, project="home-user-dev-x"):
    """Write records as <projects>/<project>/<session_id>.jsonl."""
    proj = projects / project
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


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

    @pytest.mark.parametrize("waiting_status", ["requires_action", "waiting"])
    def test_urgent_sessions_come_first(self, tmp_path, waiting_status):
        pid = os.getpid()
        start = own_start_token()
        # All on the same live PID, only the status differs
        for name, status in [("a", "idle"), ("b", waiting_status), ("c", "busy")]:
            data = {
                "pid": pid, "name": name, "status": status, "cwd": "/x",
                "kind": "interactive", "procStart": start,
                "statusUpdatedAt": int(time.time() * 1000),
            }
            (tmp_path / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
        assert [s.status for s in list_sessions(tmp_path)] == [
            waiting_status, "busy", "idle"]


class TestSessionFields:
    def test_project_is_last_path_element(self):
        assert Session(1, "n", "/home/user/dev/karr/", "idle", "x").project == "karr"

    # Both the current ("waiting", Claude Code >= 2.1.251) and the legacy
    # ("requires_action") spellings map to the same waiting-for-human state.
    @pytest.mark.parametrize("status", ["requires_action", "waiting"])
    def test_status_text_is_translated(self, status):
        assert Session(1, "n", "/x", status, "y").status_text == "waiting for you"

    @pytest.mark.parametrize("status", ["requires_action", "waiting"])
    def test_waiting_state_recognized(self, status):
        assert Session(1, "n", "/x", status, "y").waiting

    def test_unknown_status_passes_through(self):
        assert Session(1, "n", "/x", "compiling", "y").status_text == "compiling"

    def test_waiting_and_working_flags(self):
        assert Session(1, "n", "/x", "requires_action", "y").waiting
        # busy with no transcript clock trusts status -> working
        assert Session(1, "n", "/x", "busy", "y").working
        assert not Session(1, "n", "/x", "idle", "y").waiting


class TestWorkingGate:
    """A busy session only counts as *working* while its transcript (the real
    LLM-activity clock) is fresh; stale busy is idle-in-disguise."""

    def test_busy_with_recent_transcript_is_working(self):
        # Preview: status=busy + inactive 3s -> filled dot
        s = Session(1, "n", "/x", "busy", "i", active_at=time.time() - 3)
        assert s.working

    def test_busy_with_stale_transcript_is_not_working(self):
        # The reproduced bug: status="busy" frozen ~18h ago while the transcript
        # shows ~48m of inactivity. Preview: status=busy + inactive 48m -> hollow.
        s = Session(1, "n", "/x", "busy", "i", active_at=time.time() - 48 * 60)
        assert not s.working

    def test_busy_without_transcript_trusts_status(self):
        # No transcript file yet (just started): no reliable activity clock, so
        # prefer trusting status over hiding fresh work. status_since is stale
        # here on purpose - it must NOT be used to demote a clockless session.
        s = Session(1, "n", "/x", "busy", "i",
                    active_at=0.0, status_since=time.time() - 48 * 60)
        assert s.working

    def test_gate_boundary(self):
        assert Session(1, "n", "/x", "busy", "i",
                       active_at=time.time() - (WORKING_ACTIVE_WINDOW - 5)).working
        assert not Session(1, "n", "/x", "busy", "i",
                           active_at=time.time() - (WORKING_ACTIVE_WINDOW + 5)).working

    @pytest.mark.parametrize("status", ["requires_action", "waiting"])
    def test_waiting_is_not_gated_by_transcript(self, status):
        # A session waiting for a human isn't writing the transcript by
        # definition; it must still surface regardless of activity. The 120s
        # working window only touches the busy branch.
        s = Session(1, "n", "/x", status, "i", active_at=time.time() - 48 * 60)
        assert s.waiting
        assert not s.working

    @pytest.mark.parametrize("status", ["requires_action", "waiting"])
    def test_waiting_counts_in_summary(self, status):
        assert "1 waiting" in summarize([Session(1, "a", "/x", status, "i")])

    def test_stale_busy_status_text_matches_hollow_dot(self):
        # Coherent with the dot: a stale-busy row reads "ready", not "working"
        # (the gutter already shows the inactivity age).
        stale = Session(1, "n", "/x", "busy", "i", active_at=time.time() - 48 * 60)
        assert stale.status_text == "ready"
        fresh = Session(1, "n", "/x", "busy", "i", active_at=time.time() - 3)
        assert fresh.status_text == "working"

    def test_stale_busy_drops_out_of_active_count(self):
        sessions = [
            Session(1, "a", "/x", "busy", "i", active_at=time.time() - 3),
            Session(2, "b", "/y", "busy", "i", active_at=time.time() - 48 * 60),
        ]
        assert "1 active" in summarize(sessions)


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


class TestInactivity:
    """Single-unit 'time since last LLM activity' counter."""

    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"), (1, "1s"), (3, "3s"), (59, "59s"),   # boundary at 60
        (60, "1m"), (120, "2m"), (3599, "59m"),         # boundary at 3600
        (3600, "1h"), (7200, "2h"),
    ])
    def test_inactive_format(self, seconds, expected):
        assert _fmt_inactive(seconds) == expected

    def test_inactive_uses_active_at(self):
        s = Session(1, "n", "/x", "busy", "i", active_at=time.time() - 65)
        assert s.inactive_text == "1m"

    def test_inactive_falls_back_to_status_since(self):
        s = Session(1, "n", "/x", "idle", "i", status_since=time.time() - 2)
        assert s.inactive_text == "2s"

    def test_inactive_zero_without_any_reference(self):
        s = Session(1, "n", "/x", "idle", "i")
        assert s.inactive_seconds == 0.0
        assert s.inactive_text == "0s"

    def test_transcript_mtime_reads_by_session_id(self, tmp_path):
        proj = tmp_path / "home-user-dev-x"
        proj.mkdir()
        f = proj / "sid-abc.jsonl"
        f.write_text("{}\n", encoding="utf-8")
        os.utime(f, (1_000_000, 1_700_000_000))
        assert _transcript_mtime("sid-abc", ttl=0, projects=tmp_path) == 1_700_000_000

    def test_transcript_mtime_missing_is_none(self, tmp_path):
        assert _transcript_mtime("nope", ttl=0, projects=tmp_path) is None

    def test_transcript_mtime_empty_id_is_none(self, tmp_path):
        assert _transcript_mtime("", ttl=0, projects=tmp_path) is None


class TestModelLabel:
    """Short label for the model id, so the row reads `opus 5`, not
    `claude-opus-5`."""

    @pytest.mark.parametrize("model_id,expected", [
        # The ids actually seen in transcripts on this machine
        ("claude-opus-5", "opus 5"),
        ("claude-sonnet-5", "sonnet 5"),
        ("claude-opus-4-8", "opus 4.8"),
        ("claude-fable-5-1", "fable 5.1"),
        ("claude-haiku-4-5-20251001", "haiku 4.5"),   # snapshot date dropped
        ("sonnet", "sonnet"),                          # bare alias
        # Unknown shapes must degrade, never crash and never vanish
        ("claude-3-5-sonnet-20241022", "3-5-sonnet"),
        ("MiniMax-M3", "MiniMax-M3"),
        ("deepseek-v4-flash-0731", "deepseek-v4-flash-0731"),
        ("", ""),
    ])
    def test_label(self, model_id, expected):
        assert _model_label(model_id) == expected

    def test_session_exposes_the_label(self):
        assert Session(1, "n", "/x", "idle", "i", model="claude-opus-5").model_text \
            == "opus 5"

    def test_unknown_model_is_an_empty_label(self):
        assert Session(1, "n", "/x", "idle", "i").model_text == ""


class TestTranscriptModel:
    """The model is only in the transcript - the session file has no such
    field - and the transcript is read from the tail, never whole."""

    def test_reads_the_last_assistant_model(self, tmp_path):
        write_transcript(tmp_path, "sid-a", [
            assistant_line("claude-sonnet-5"),
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            assistant_line("claude-opus-5"),
        ])
        assert _transcript_model("sid-a", ttl=0, projects=tmp_path) == "claude-opus-5"

    def test_subagent_turns_do_not_override_the_session_model(self, tmp_path):
        # A fan-out ends with subagent turns on a smaller model; the row must
        # keep naming the model the session itself runs.
        write_transcript(tmp_path, "sid-b", [
            assistant_line("claude-opus-5"),
            assistant_line("claude-haiku-4-5-20251001", sidechain=True),
            assistant_line("claude-haiku-4-5-20251001", sidechain=True),
        ])
        assert _transcript_model("sid-b", ttl=0, projects=tmp_path) == "claude-opus-5"

    def test_synthetic_placeholder_is_not_a_model(self, tmp_path):
        # Claude Code writes "<synthetic>" on lines no model produced.
        write_transcript(tmp_path, "sid-c", [
            assistant_line("claude-opus-5"),
            assistant_line("<synthetic>"),
        ])
        assert _transcript_model("sid-c", ttl=0, projects=tmp_path) == "claude-opus-5"

    def test_broken_line_is_skipped_not_fatal(self, tmp_path):
        path = write_transcript(tmp_path, "sid-d", [assistant_line("claude-opus-5")])
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"message": {"model": "claude-sonnet-5"\n')  # truncated write
        assert _transcript_model("sid-d", ttl=0, projects=tmp_path) == "claude-opus-5"

    def test_only_the_tail_is_read(self, tmp_path):
        """The performance contract: transcripts run to tens of MB in the frame
        loop, so a model buried before the tail window is deliberately NOT
        found. Turning this green by reading the whole file is the regression."""
        proj = tmp_path / "home-user-dev-x"
        proj.mkdir()
        filler = json.dumps({"type": "user", "pad": "x" * 4000}) + "\n"
        # The model is on line 2, not line 1: line 1 is dropped anyway as the
        # fragment the tail seek lands in, so it must not be what hides it.
        (proj / "sid-e.jsonl").write_text(
            filler
            + json.dumps(assistant_line("claude-opus-5")) + "\n"
            + filler * (2 * _MODEL_TAIL // len(filler) + 1),
            encoding="utf-8")
        assert _transcript_model("sid-e", ttl=0, projects=tmp_path) == ""

    def test_recent_model_wins_over_an_older_one_in_the_tail(self, tmp_path):
        proj = tmp_path / "home-user-dev-x"
        proj.mkdir()
        filler = json.dumps({"type": "user", "pad": "x" * 4000}) + "\n"
        (proj / "sid-f.jsonl").write_text(
            json.dumps(assistant_line("claude-sonnet-5")) + "\n"
            + filler * (2 * _MODEL_TAIL // len(filler) + 1)
            + json.dumps(assistant_line("claude-opus-5")) + "\n",
            encoding="utf-8")
        assert _transcript_model("sid-f", ttl=0, projects=tmp_path) == "claude-opus-5"

    def test_no_transcript_is_empty(self, tmp_path):
        assert _transcript_model("nope", ttl=0, projects=tmp_path) == ""

    def test_transcript_without_any_model_is_empty(self, tmp_path):
        write_transcript(tmp_path, "sid-g", [{"type": "user", "message": {"x": 1}}])
        assert _transcript_model("sid-g", ttl=0, projects=tmp_path) == ""

    def test_empty_session_id_is_empty(self, tmp_path):
        assert _transcript_model("", ttl=0, projects=tmp_path) == ""

    def test_cache_avoids_re_reading_every_frame(self, tmp_path):
        from tzmrit_display import claude_sessions as cs
        cs._model_cache.clear()
        write_transcript(tmp_path, "sid-h", [assistant_line("claude-opus-5")])
        assert cs._transcript_model("sid-h", projects=tmp_path) == "claude-opus-5"
        cs._model_cache["sid-h"] = (cs.time.monotonic(), "claude-sonnet-5")
        assert cs._transcript_model("sid-h", projects=tmp_path) == "claude-sonnet-5"
        cs._model_cache.clear()

    def test_listing_fills_the_model_from_the_transcript(self, tmp_path, monkeypatch):
        from tzmrit_display import claude_sessions as cs
        cs._model_cache.clear()
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        projects = tmp_path / "projects"
        write_session(sessions_dir, os.getpid(), procStart=own_start_token(),
                      sessionId="sid-live")
        write_transcript(projects, "sid-live", [assistant_line("claude-opus-5")])
        monkeypatch.setattr(cs, "PROJECTS_DIR", projects)
        sessions = cs.list_sessions(sessions_dir)
        assert len(sessions) == 1
        assert sessions[0].model_text == "opus 5"
        cs._model_cache.clear()


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
