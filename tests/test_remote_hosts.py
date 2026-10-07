"""Sessions of other machines: the probe that runs there, the reader here."""

import json
import os
import time

from tzmrit_display import remote_hosts, remote_probe
from tzmrit_display.claude_sessions import Session
from tzmrit_display.remote_hosts import RemoteHost, RemoteHosts, parse_hosts, to_sessions


def payload(now=None, **session):
    item = {"pid": 7, "name": "karr-01", "cwd": "/home/u/dev/karr",
            "status": "busy", "kind": "interactive", "session_id": "abc",
            "status_since": 0.0, "rss": 1024, "child_count": 2,
            "active_at": 0.0, "model": "claude-opus-5-5"}
    item.update(session)
    return {"v": 1, "host": "box", "now": now or time.time(), "sessions": [item]}


class TestParseHosts:
    def test_repeated_and_comma_separated_values_flatten_in_order(self):
        assert parse_hosts(["atlas,nova", " patrick "]) == ["atlas", "nova", "patrick"]

    def test_duplicates_and_blanks_drop_out(self):
        assert parse_hosts(["atlas,,atlas"]) == ["atlas"]

    def test_nothing_given_is_no_host(self):
        assert parse_hosts([]) == []
        assert parse_hosts(None) == []


class TestToSessions:
    def test_row_carries_the_configured_host_name(self):
        """The label is the name the user typed, not what the machine calls
        itself - that is the name they can ssh to."""
        [s] = to_sessions("atlas", payload(), time.time())
        assert s.host == "atlas"
        assert (s.name, s.project, s.model_text) == ("karr-01", "karr", "opus 5.5")

    def test_remote_clock_skew_is_taken_out_of_the_timestamps(self):
        """A host whose clock runs an hour behind must not look an hour idle."""
        local = time.time()
        remote = local - 3600
        [s] = to_sessions("h", payload(now=remote, active_at=remote - 3,
                                       status_since=remote - 3), local)
        assert s.working
        assert s.inactive_seconds < 10

    def test_missing_timestamp_stays_missing(self):
        """0 means "unknown" to Session; shifting it by the skew would turn it
        into a bogus point in time."""
        [s] = to_sessions("h", payload(now=time.time() - 3600), time.time())
        assert s.active_at == 0.0 and s.status_since == 0.0

    def test_malformed_entries_are_skipped(self):
        data = {"now": time.time(), "sessions": ["x", {"pid": "7"}, {"pid": 3}]}
        assert [s.pid for s in to_sessions("h", data, time.time())] == [3]


class TestRemoteHost:
    def test_offline_until_the_first_line(self):
        assert RemoteHost("h").sessions() is None

    def test_a_line_makes_the_host_report(self):
        host = RemoteHost("h")
        host.feed(json.dumps(payload()))
        assert [s.name for s in host.sessions()] == ["karr-01"]

    def test_noise_on_stdout_is_ignored(self):
        """A login banner must neither crash the reader nor count as data."""
        host = RemoteHost("h")
        host.feed("Welcome to box!\n")
        host.feed("[1, 2]\n")
        assert host.sessions() is None

    def test_stale_data_turns_the_host_offline(self, monkeypatch):
        host = RemoteHost("h")
        host.feed(json.dumps(payload()))
        host._seen -= remote_hosts.STALE + 1
        assert host.sessions() is None

    def test_collect_separates_sessions_from_offline_hosts(self):
        hosts = RemoteHosts(["up", "down"])
        hosts.hosts[0].feed(json.dumps(payload()))
        sessions, offline = hosts.collect()
        assert [s.host for s in sessions] == ["up"]
        assert offline == ["down"]


class TestProbe:
    def write(self, tmp_path, monkeypatch, **fields):
        sessions = tmp_path / "sessions"
        projects = tmp_path / "projects"
        sessions.mkdir()
        (projects / "-home-u-dev-karr").mkdir(parents=True)
        data = {"pid": os.getpid(), "sessionId": "abc", "cwd": "/home/u/dev/karr",
                "name": "karr-01", "status": "busy", "statusUpdatedAt": 1700000000000}
        data.update(fields)
        (sessions / f"{data['pid']}.json").write_text(json.dumps(data))
        monkeypatch.setattr(remote_probe, "SESSION_DIR", str(sessions))
        monkeypatch.setattr(remote_probe, "PROJECTS_DIR", str(projects))
        monkeypatch.setattr(remote_probe, "HAS_PROC", False)
        remote_probe._transcripts.clear()
        remote_probe._models.clear()
        return projects / "-home-u-dev-karr" / "abc.jsonl"

    def test_snapshot_reads_session_file_and_transcript(self, tmp_path, monkeypatch):
        transcript = self.write(tmp_path, monkeypatch)
        transcript.write_text(json.dumps(
            {"message": {"role": "assistant", "model": "claude-opus-5-5"}}) + "\n")
        snap = remote_probe.snapshot()
        [s] = snap["sessions"]
        assert s["name"] == "karr-01"
        assert s["status_since"] == 1700000000.0
        assert s["model"] == "claude-opus-5-5"
        assert abs(s["active_at"] - transcript.stat().st_mtime) < 1
        assert abs(snap["now"] - time.time()) < 5

    def test_dead_session_is_left_out(self, tmp_path, monkeypatch):
        self.write(tmp_path, monkeypatch, pid=9_999_994)
        assert remote_probe.snapshot()["sessions"] == []

    def test_snapshot_round_trips_into_sessions(self, tmp_path, monkeypatch):
        """What the probe prints is what the reader accepts."""
        self.write(tmp_path, monkeypatch)
        line = json.dumps(remote_probe.snapshot())
        host = RemoteHost("box")
        host.feed(line)
        [s] = host.sessions()
        assert isinstance(s, Session) and s.host == "box" and s.pid == os.getpid()

    def test_probe_source_runs_on_an_old_python(self):
        """It is piped into whatever python3 the host has: no annotations
        import, no walrus, no match, no `X | None`."""
        source = open(remote_probe.__file__, encoding="utf-8").read()
        assert "from __future__" not in source
        assert ":=" not in source
        assert "tzmrit_display" not in source.split('"""', 2)[2]
