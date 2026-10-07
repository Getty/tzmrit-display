"""Host labels, foreign-account limits and subagent counting."""

import json
import os
import time

from tzmrit_display import remote_hosts, remote_probe
from tzmrit_display.claude_sessions import Session
from tzmrit_display.remote_hosts import RemoteHost, RemoteHosts


def line(**extra):
    data = {"v": 1, "host": "box", "now": time.time(), "sessions": [],
            "account": {"email": "alex@example.org", "name": "Alex"}}
    data.update(extra)
    return json.dumps(data)


USAGE = {"limits": [{"kind": "session", "percent": 9, "resets_at": None},
                    {"kind": "weekly_all", "percent": 56, "resets_at": None}]}


class TestHostLabel:
    def test_login_and_domain_are_dropped(self):
        assert remote_hosts.host_label("alex@lab.example.org") == "lab"
        assert remote_hosts.host_label("atlas") == "atlas"

    def test_an_ip_address_stays_whole(self):
        assert remote_hosts.host_label("root@10.0.0.5") == "10.0.0.5"

    def test_explicit_label_wins_and_is_not_sent_to_ssh(self):
        host = RemoteHost("alex=alex@lab.example.org")
        assert (host.label, host.name) == ("alex", "alex@lab.example.org")

    def test_offline_list_uses_the_label(self):
        assert RemoteHosts(["alex=f@x.example.org"]).collect() == ([], ["alex"])


class TestForeignAccount:
    def test_same_account_host_contributes_no_limits(self):
        """The probe omits `usage` when its account is the viewer's own."""
        hosts = RemoteHosts(["a"])
        hosts.hosts[0].feed(line())
        assert hosts.accounts() == []

    def test_foreign_account_is_titled_and_parsed(self):
        hosts = RemoteHosts(["a"])
        hosts.hosts[0].feed(line(usage=USAGE))
        [account] = hosts.accounts()
        assert account.name == "ALEX"
        assert [(r.label, r.percent) for r in account.limits.rows] == [
            ("Session", 9), ("Weekly", 56)]

    def test_block_stays_while_usage_is_unreadable(self):
        """An expired token on the host must not make the block vanish."""
        hosts = RemoteHosts(["a"])
        hosts.hosts[0].feed(line(usage=None))
        [account] = hosts.accounts()
        assert account.name == "ALEX" and account.limits is None

    def test_two_hosts_on_one_account_give_one_block_with_data(self):
        hosts = RemoteHosts(["a", "b"])
        hosts.hosts[0].feed(line(usage=None))
        hosts.hosts[1].feed(line(usage=USAGE))
        [account] = hosts.accounts()
        assert account.limits is not None

    def test_account_survives_the_host_going_stale(self):
        hosts = RemoteHosts(["a"])
        hosts.hosts[0].feed(line(usage=USAGE))
        hosts.hosts[0]._seen -= remote_hosts.STALE + 1
        assert len(hosts.accounts()) == 1

    def test_viewer_email_cannot_inject_into_the_remote_command(self):
        host = RemoteHost("a", viewer_email="me@x.org; rm -rf ~ $(id)")
        assert host._viewer == "me@x.orgrm-rfid"

    def test_probe_fetches_usage_only_for_a_foreign_account(self, monkeypatch):
        calls = []
        monkeypatch.setattr(remote_probe, "account",
                            lambda: {"email": "Alex@example.org", "name": "Alex"})
        monkeypatch.setattr(remote_probe, "usage",
                            lambda: calls.append(1) or {"limits": []})
        monkeypatch.setattr(remote_probe, "SESSION_DIR", "/nonexistent")
        assert "usage" not in remote_probe.snapshot("alex@EXAMPLE.org")
        assert calls == []
        assert remote_probe.snapshot("getty@example.org")["usage"] == {"limits": []}

    def test_probe_keeps_the_last_good_usage_through_a_failure(self, monkeypatch):
        answers = [{"limits": [1]}, None]
        monkeypatch.setattr(remote_probe, "fetch_usage", lambda: answers.pop(0))
        monkeypatch.setattr(remote_probe, "_usage", {"at": None, "value": None, "fails": 0})
        assert remote_probe.usage() == {"limits": [1]}
        remote_probe._usage["at"] -= remote_probe.USAGE_TTL + 1
        assert remote_probe.usage() == {"limits": [1]}
        assert remote_probe._usage["fails"] == 1


class TestSubagents:
    def test_fresh_subagent_transcripts_count_stale_ones_do_not(self, tmp_path):
        from tzmrit_display import claude_sessions as cs
        sub = tmp_path / "-proj" / "sid-1" / "subagents"
        sub.mkdir(parents=True)
        for name, age in (("agent-a", 5), ("agent-b", 30), ("agent-c", 3600)):
            path = sub / f"{name}.jsonl"
            path.write_text("{}\n")
            os.utime(path, (time.time() - age,) * 2)
        (sub / "agent-a.meta.json").write_text("{}")
        cs._subagent_cache.clear()
        count, newest = cs._subagent_activity("sid-1", projects=tmp_path)
        assert count == 2
        assert abs(newest - (time.time() - 5)) < 2

    def test_probe_counts_the_same_way(self, tmp_path):
        log = tmp_path / "sid-1.jsonl"
        sub = tmp_path / "sid-1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.jsonl").write_text("{}\n")
        old = sub / "agent-b.jsonl"
        old.write_text("{}\n")
        os.utime(old, (time.time() - 3600,) * 2)
        assert remote_probe.subagents(str(log))[0] == 1

    def test_idle_session_with_running_agents_reads_as_working(self):
        s = Session(1, "n", "/x", "idle", "i", subagents=3)
        assert s.working and s.status_text == "3 agents"
        assert s.sort_key[0] == Session(2, "m", "/y", "busy", "i").sort_key[0]

    def test_waiting_is_never_masked_by_agents(self):
        s = Session(1, "n", "/x", "waiting", "i", subagents=2)
        assert not s.working and s.status_text == "waiting for you"

    def test_one_agent_is_singular_and_summary_totals_them(self):
        from tzmrit_display.claude_sessions import summarize
        a = Session(1, "a", "/x", "busy", "i", subagents=1)
        b = Session(2, "b", "/y", "idle", "i", subagents=2)
        assert a.status_text == "1 agent"
        assert "3 agents" in summarize([a, b])
