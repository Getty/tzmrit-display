"""Tests for the Claude account rate-limit budget.

No network here: the parser is exercised against a captured usage fixture, and
`fetch`/`get_limits` are steered through their credential and cache seams.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tzmrit_display import claude_limits as cl
from tzmrit_display.claude_limits import (
    Limit,
    Limits,
    _fmt_reset,
    _read_token,
    fetch,
    get_limits,
    parse_usage,
)

FIXTURE = Path(__file__).parent / "fixtures" / "usage.json"


def load_usage():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestParseUsage:
    def test_prefers_the_limits_array(self):
        limits = parse_usage(load_usage())
        assert limits is not None
        assert limits.session.label == "Session"
        assert limits.session.percent == 13
        assert limits.weekly.label == "Weekly"
        assert limits.weekly.percent == 37

    def test_percent_is_an_int(self):
        limits = parse_usage(load_usage())
        assert isinstance(limits.session.percent, int)
        assert isinstance(limits.weekly.percent, int)

    def test_resets_at_is_parsed_utc(self):
        limits = parse_usage(load_usage())
        assert limits.session.resets_at == datetime(2026, 8, 29, 9, 20, tzinfo=timezone.utc)

    def test_severity_is_carried(self):
        limits = parse_usage(load_usage())
        assert limits.session.severity == "normal"

    def test_falls_back_to_flat_buckets(self):
        data = load_usage()
        del data["limits"]
        limits = parse_usage(data)
        # utilization is a float; it becomes an int percentage
        assert limits.session.percent == 13
        assert limits.weekly.percent == 37
        assert limits.session.label == "Session"
        assert limits.session.resets_at == datetime(2026, 8, 29, 9, 20, tzinfo=timezone.utc)

    def test_one_window_can_come_from_each_source(self):
        """A limits[] entry for one window, a flat bucket for the other."""
        data = load_usage()
        data["limits"] = [e for e in data["limits"] if e["kind"] == "session"]
        limits = parse_usage(data)
        assert limits.session.percent == 13          # from limits[]
        assert limits.weekly.percent == 37           # from seven_day

    def test_empty_returns_none(self):
        assert parse_usage({}) is None
        assert parse_usage({"limits": []}) is None

    def test_garbage_returns_none(self):
        assert parse_usage(None) is None
        assert parse_usage("nope") is None
        assert parse_usage([1, 2, 3]) is None

    def test_missing_resets_at_is_tolerated(self):
        limits = parse_usage({"limits": [{"kind": "session", "percent": 5}]})
        assert limits.session.percent == 5
        assert limits.session.resets_at is None
        assert limits.weekly is None

    def test_bad_percent_falls_back_to_zero(self):
        limits = parse_usage({"limits": [{"kind": "session", "percent": None}]})
        assert limits.session.percent == 0


class TestFormatting:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "now"), (-30, "now"), (59, "0m"), (600, "10m"),
        (3600, "1h00m"), (4800, "1h20m"), (7300, "2h01m"),
        (86400, "1d"), (2 * 86400, "2d"), (9 * 86400, "9d"),
    ])
    def test_reset_format(self, seconds, expected):
        assert _fmt_reset(seconds) == expected

    def test_reset_text_uses_supplied_now(self):
        lim = Limit("Session", 13,
                    resets_at=datetime(2026, 8, 29, 9, 20, tzinfo=timezone.utc))
        now = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
        assert lim.reset_text(now) == "1h20m"

    def test_reset_text_without_reset_is_empty(self):
        assert Limit("Session", 13).reset_text() == ""


class TestReadToken:
    def _creds(self, tmp_path, **oauth):
        payload = {"claudeAiOauth": oauth}
        (tmp_path / "creds.json").write_text(json.dumps(payload), encoding="utf-8")
        return tmp_path / "creds.json"

    def test_reads_a_valid_token(self, tmp_path, monkeypatch):
        future = time.time() * 1000 + 3_600_000
        path = self._creds(tmp_path, accessToken="secret", expiresAt=future)
        monkeypatch.setattr(cl, "CREDENTIALS", path)
        assert _read_token() == "secret"

    def test_expired_token_is_ignored(self, tmp_path, monkeypatch):
        past = time.time() * 1000 - 1000
        path = self._creds(tmp_path, accessToken="secret", expiresAt=past)
        monkeypatch.setattr(cl, "CREDENTIALS", path)
        assert _read_token() is None

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cl, "CREDENTIALS", tmp_path / "absent.json")
        assert _read_token() is None

    def test_no_expiry_is_treated_as_valid(self, tmp_path, monkeypatch):
        path = self._creds(tmp_path, accessToken="secret")
        monkeypatch.setattr(cl, "CREDENTIALS", path)
        assert _read_token() == "secret"

    def test_missing_token_field_returns_none(self, tmp_path, monkeypatch):
        path = self._creds(tmp_path, expiresAt=time.time() * 1000 + 1000)
        monkeypatch.setattr(cl, "CREDENTIALS", path)
        assert _read_token() is None

    def test_broken_json_returns_none(self, tmp_path, monkeypatch):
        path = tmp_path / "creds.json"
        path.write_text("{broken", encoding="utf-8")
        monkeypatch.setattr(cl, "CREDENTIALS", path)
        assert _read_token() is None


class TestFetch:
    def test_no_token_means_no_network(self, monkeypatch):
        """Without a token, fetch must return None and never open a socket."""
        monkeypatch.setattr(cl, "_read_token", lambda *a, **k: None)

        def explode(*a, **k):
            raise AssertionError("fetch must not hit the network without a token")

        monkeypatch.setattr(cl.urllib.request, "urlopen", explode)
        assert fetch() is None

    def test_network_error_fails_silent(self, monkeypatch):
        monkeypatch.setattr(cl, "_read_token", lambda *a, **k: "secret")

        def explode(*a, **k):
            raise OSError("offline")

        monkeypatch.setattr(cl.urllib.request, "urlopen", explode)
        assert fetch() is None


class TestGetLimits:
    def _reset(self):
        cl._cache["at"] = 0.0
        cl._cache["value"] = None
        cl._fetching = False

    def test_background_refresh_populates_cache(self, monkeypatch):
        self._reset()
        sentinel = Limits(session=Limit("Session", 42))
        monkeypatch.setattr(cl, "fetch", lambda: sentinel)
        assert get_limits() is None  # first call returns before the fetch lands
        deadline = time.monotonic() + 2.0
        while cl._cache["value"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cl._cache["value"] is sentinel
        assert get_limits() is sentinel  # fresh cache, returned directly

    def test_fresh_cache_does_not_refetch(self, monkeypatch):
        self._reset()
        sentinel = Limits(session=Limit("Session", 7))
        cl._cache["at"] = time.monotonic()
        cl._cache["value"] = sentinel

        def explode():
            raise AssertionError("a fresh cache must not trigger a fetch")

        monkeypatch.setattr(cl, "fetch", explode)
        assert get_limits(ttl=60.0) is sentinel
