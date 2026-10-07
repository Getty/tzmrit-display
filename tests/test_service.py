"""Stale limits, the hosts file and the Windows autostart task."""

import json
import os
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from tzmrit_display import claude_limits as cl
from tzmrit_display import cli, remote_hosts, service, theme as T
from tzmrit_display.claude_limits import AccountLimits, Limit, Limits
from tzmrit_display.remote_hosts import RemoteHosts, read_hosts_file
from tzmrit_display.render import DashboardRenderer, _SPLIT_UTILITY

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def colors(image, box):
    return {found for _, found in image.crop(box).getcolors(1 << 24)}


class TestStaleLimits:
    @pytest.fixture(autouse=True)
    def clean_cache(self):
        saved = dict(cl._cache), cl._fail_count
        cl._cache.update(at=cl._NEVER, value=None)
        cl._fail_count = 0
        yield
        cl._cache.clear()
        cl._cache.update(saved[0])
        cl._fail_count = saved[1]

    def test_failed_refresh_keeps_the_last_reading_marked_stale(self, monkeypatch):
        good = Limits(session=Limit("Session", 40))
        monkeypatch.setattr(cl, "fetch", lambda: good)
        cl._refresh()
        assert cl._cache["value"] is good and not good.stale
        assert good.as_of is not None

        monkeypatch.setattr(cl, "fetch", lambda: None)
        cl._refresh()
        kept = cl._cache["value"]
        assert kept.stale and kept.session.percent == 40
        assert kept.as_of == good.as_of
        assert not good.stale, "a frame holding the old object must not see it change"

    def test_a_good_refresh_clears_the_mark(self, monkeypatch):
        answers = [Limits(session=Limit("Session", 40)), None,
                   Limits(session=Limit("Session", 41))]
        monkeypatch.setattr(cl, "fetch", lambda: answers.pop(0))
        for _ in range(3):
            cl._refresh()
        assert not cl._cache["value"].stale
        assert cl._cache["value"].session.percent == 41

    def test_last_reading_survives_a_restart(self, monkeypatch):
        """A fresh process whose first fetch fails shows the stored reading."""
        usage = {"limits": [{"kind": "session", "percent": 33, "resets_at": None}]}
        cl._remember(usage)
        monkeypatch.setattr(cl, "fetch", lambda: None)
        cl._refresh()
        shown = cl._cache["value"]
        assert shown.stale and shown.session.percent == 33
        assert abs(shown.as_of - time.time()) < 5

    def test_a_week_old_stored_reading_is_not_shown(self):
        cl._remember({"limits": [{"kind": "session", "percent": 33}]})
        assert cl._recall(now=time.time() + 8 * 86400) is None
        assert cl._recall(now=time.time() + 60) is not None

    def test_a_broken_store_is_ignored(self):
        cl._store_path().write_text("{not json", encoding="utf-8")
        assert cl._recall() is None

    def test_age_text(self):
        now = 1_000_000.0
        assert Limits(as_of=now - 20).age_text(now) == "<1m"
        assert Limits(as_of=now - 720).age_text(now) == "12m"
        assert Limits(as_of=now - 7500).age_text(now) == "2h05m"
        assert Limits().age_text(now) == ""

    def test_remote_reading_is_stale_when_the_probe_says_so(self):
        usage = {"limits": [{"kind": "session", "percent": 9, "resets_at": None}]}
        base = {"v": 1, "host": "box", "sessions": [], "usage": usage,
                "account": {"email": "alex@example.org", "name": "Alex"}}
        hosts = RemoteHosts(["a"])
        now = time.time() + 500          # the remote clock runs ahead
        hosts.hosts[0].feed(json.dumps(dict(
            base, now=now, usage_at=now - 600, usage_stale=True)))
        [account] = hosts.accounts()
        assert account.limits.stale
        assert abs(account.limits.as_of - (time.time() - 600)) < 5, \
            "the age must survive a skewed remote clock"

        hosts.hosts[0].feed(json.dumps(dict(
            base, now=now, usage_at=now, usage_stale=False)))
        assert not hosts.accounts()[0].limits.stale

    def test_remote_reading_goes_stale_with_its_host(self):
        usage = {"limits": [{"kind": "session", "percent": 9, "resets_at": None}]}
        hosts = RemoteHosts(["a"])
        hosts.hosts[0].feed(json.dumps({
            "v": 1, "host": "box", "now": time.time(), "sessions": [],
            "usage": usage, "account": {"email": "f@example.org", "name": "F"}}))
        assert not hosts.accounts()[0].limits.stale
        hosts.hosts[0]._seen -= remote_hosts.STALE + 1
        assert hosts.accounts()[0].limits.stale

    def test_stale_bars_are_faded_not_dropped(self):
        utility = (_SPLIT_UTILITY[0], 94, _SPLIT_UTILITY[1], 432)

        def board(stale):
            limits = Limits(session=Limit("Session", 60), stale=stale,
                            as_of=time.time() - 900)
            return DashboardRenderer(scale=1).render_board(
                {}, [], [], "", [AccountLimits("GETTY", limits)], [])

        fresh, old = colors(board(False), utility), colors(board(True), utility)
        assert rgb(T.ACCENT) in fresh
        assert rgb(T.ACCENT) not in old, "a stale bar must not look current"
        faded = tuple(round((a + b) / 2) for a, b in zip(rgb(T.ACCENT), rgb(T.SURFACE)))
        assert faded in old

    def test_stale_split_view_renders(self):
        limits = Limits(session=Limit("Session", 60), stale=True)
        DashboardRenderer(scale=1).render_split({}, [], "", [], limits)


class TestHostsFile:
    def test_lines_comments_and_labels(self, tmp_path):
        path = tmp_path / "hosts.txt"
        path.write_text("# my machines\natlas\n\n  nova  # the small one\n"
                        "alex=alex@lab.example.org, atlas\n", encoding="utf-8")
        assert read_hosts_file(path) == [
            "atlas", "nova", "alex=alex@lab.example.org"]

    def test_missing_file_is_no_hosts(self, tmp_path):
        assert read_hosts_file(tmp_path / "nope.txt") == []

    def test_bom_from_notepad_is_ignored(self, tmp_path):
        path = tmp_path / "hosts.txt"
        path.write_bytes(b"\xef\xbb\xbfatlas\r\n")
        assert read_hosts_file(path) == ["atlas"]

    def test_install_without_hosts_never_empties_an_existing_file(self, tmp_path):
        path = tmp_path / "sub" / "hosts.txt"
        service.write_hosts(["atlas"], path)
        service.write_hosts([], path)
        assert read_hosts_file(path) == ["atlas"]

    def test_first_install_leaves_a_commented_template(self, tmp_path):
        path = service.write_hosts([], tmp_path / "hosts.txt")
        assert path.read_text(encoding="utf-8").startswith("#")
        assert read_hosts_file(path) == []


class FakeRemotes:
    made = []

    def __init__(self, names, ssh="ssh", viewer_email=""):
        self.names, self.stopped = list(names), False
        FakeRemotes.made.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True


class TestHostList:
    @pytest.fixture(autouse=True)
    def fake(self, monkeypatch):
        FakeRemotes.made = []
        monkeypatch.setattr(cli, "RemoteHosts", FakeRemotes)
        monkeypatch.setattr(cli, "local_account", lambda: ("me@example.org", "Me"))

    def touch(self, path, text):
        path.write_text(text, encoding="utf-8")
        stamp = time.time_ns() + len(FakeRemotes.made) * 10**9 + 10**9
        os.utime(path, ns=(stamp, stamp))

    def test_no_host_option_means_no_board(self):
        hosts = cli.HostList(SimpleNamespace(hosts=[], hosts_file=None))
        assert not hosts.enabled and hosts.current() is None

    def test_file_edit_swaps_the_pollers(self, tmp_path):
        path = tmp_path / "hosts.txt"
        self.touch(path, "atlas\n")
        hosts = cli.HostList(SimpleNamespace(hosts=["nova"], hosts_file=str(path)))
        first = hosts.current()
        assert first.names == ["nova", "atlas"]
        assert hosts.current() is first, "an unchanged file must not reconnect"

        self.touch(path, "atlas\norion\n")
        second = hosts.current()
        assert second.names == ["nova", "atlas", "orion"]
        assert first.stopped and not second.stopped

    def test_a_comment_only_edit_keeps_the_connections(self, tmp_path):
        path = tmp_path / "hosts.txt"
        self.touch(path, "atlas\n")
        hosts = cli.HostList(SimpleNamespace(hosts=[], hosts_file=str(path)))
        first = hosts.current()
        self.touch(path, "# note\natlas\n")
        assert hosts.current() is first and not first.stopped

    def test_an_empty_file_still_gives_a_board(self, tmp_path):
        hosts = cli.HostList(SimpleNamespace(
            hosts=[], hosts_file=str(tmp_path / "missing.txt")))
        assert hosts.enabled and hosts.current().names == []


class TestTask:
    def test_definition_restarts_on_failure_and_never_times_out(self):
        xml = service.task_xml(r"C:\x\pythonw.exe", "-m tzmrit_display run",
                               r"C:\x", r"BOX\me")
        root = ET.fromstring(xml.split("?>", 1)[1])
        assert root.findtext(".//t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
        assert root.findtext(".//t:RestartOnFailure/t:Interval", namespaces=NS) == "PT1M"
        assert root.findtext(".//t:DisallowStartIfOnBatteries", namespaces=NS) == "false"
        assert root.findtext(".//t:LogonTrigger/t:UserId", namespaces=NS) == r"BOX\me"
        # the user's own session, unelevated: that is where the mapped drives are
        assert root.findtext(".//t:LogonType", namespaces=NS) == "InteractiveToken"
        assert root.findtext(".//t:RunLevel", namespaces=NS) == "LeastPrivilege"

    def test_paths_with_xml_characters_survive(self):
        xml = service.task_xml(r"C:\R&D\pythonw.exe", '--hosts-file "C:\\a <b>\\h.txt"',
                               r"C:\R&D", "me")
        root = ET.fromstring(xml.split("?>", 1)[1])
        assert root.findtext(".//t:Command", namespaces=NS) == r"C:\R&D\pythonw.exe"
        assert "<b>" in root.findtext(".//t:Arguments", namespaces=NS)

    def test_install_options_come_back_as_run_arguments(self):
        args = cli.build_parser().parse_args(
            ["service", "install", "--hosts", "atlas,nova", "--http", "8765",
             "--http-host", "127.0.0.1", "--theme", "red", "--blank-on-exit"])
        assert cli._run_argv(args) == ["--theme", "red", "--http", "8765",
                                       "--http-host", "127.0.0.1", "--blank-on-exit"]
        plain = cli.build_parser().parse_args(["service", "install"])
        assert cli._run_argv(plain) == []

    def test_run_accepts_what_install_registers(self, tmp_path):
        args = cli.build_parser().parse_args(
            ["--log-file", str(tmp_path / "d.log"), "run",
             "--hosts-file", str(tmp_path / "hosts.txt"), "--http", "8765"])
        assert args.hosts_file.endswith("hosts.txt") and args.http == 8765

    def test_service_is_refused_off_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(service.os, "name", "posix")
        assert service.status() == 1
        assert "systemd" in capsys.readouterr().err
