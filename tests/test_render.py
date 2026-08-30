"""Tests for the layout and metric logic."""

import math
import time

from tzmrit_display import theme as T
from tzmrit_display.claude_sessions import Session
from tzmrit_display.render import (
    DashboardRenderer,
    _STALE_CRIT,
    _STALE_WARN,
    _inactive_color,
    _spark_points,
)
from tzmrit_display.sources import Metric


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


class TestInactivityColor:
    """The session inactivity counter escalates in color, but with MUTED
    blends - never the pure reserved WARN/CRIT."""

    def test_under_five_minutes_is_neutral(self):
        assert _inactive_color(0) == T.INK_FAINT
        assert _inactive_color(299) == T.INK_FAINT

    def test_five_minutes_is_muted_yellow(self):
        assert _inactive_color(300) == _STALE_WARN
        assert _inactive_color(3599) == _STALE_WARN

    def test_one_hour_is_muted_red(self):
        assert _inactive_color(3600) == _STALE_CRIT
        assert _inactive_color(7200) == _STALE_CRIT

    def test_muted_blends_are_not_the_pure_status_colors(self):
        assert _STALE_WARN != _rgb(T.WARN)
        assert _STALE_CRIT != _rgb(T.CRIT)
        # and clearly toward gray: darker/less saturated than the pure hue
        assert _STALE_WARN[0] < _rgb(T.WARN)[0]
        assert _STALE_CRIT[0] < _rgb(T.CRIT)[0]


class TestMetricStatus:
    def test_thresholds(self):
        m = Metric("cpu", "CPU", warn=80, crit=95)
        m.push(50);  assert m.status == "ok"
        m.push(85);  assert m.status == "warn"
        m.push(99);  assert m.status == "crit"

    def test_metric_without_thresholds_stays_neutral(self):
        # A network rate has no meaningful threshold
        m = Metric("net_up", "NET")
        m.push(9.9e9)
        assert m.status == "ok"

    def test_history_is_bounded(self):
        m = Metric("x", "X")
        for i in range(500):
            m.push(i)
        assert len(m.history) == m.history.maxlen


class TestSparkline:
    def test_needs_two_points(self):
        m = Metric("x", "X")
        m.push(1)
        assert _spark_points(m, 0, 0, 100, 50) == []

    def test_fixed_scale_maps_percent_to_height(self):
        m = Metric("cpu", "CPU", scale_max=100)
        m.push(0); m.push(100)
        pts = _spark_points(m, 0, 0, 100, 50)
        assert pts[0][1] == 50   # 0 % sits at the bottom
        assert pts[1][1] == 0    # 100 % sits at the top

    def test_quiet_network_does_not_fill_the_plot(self):
        """Without a floor scale, noise would render as a dramatic trend."""
        m = Metric("net_up", "NET")
        for v in (10, 20, 15, 12):  # a few bytes per second
            m.push(v)
        pts = _spark_points(m, 0, 0, 100, 50)
        # Every point stays close to the baseline (floor is 100 kB/s)
        assert all(p[1] > 49.9 for p in pts)

    def test_values_above_scale_are_clamped(self):
        m = Metric("cpu", "CPU", scale_max=100)
        m.push(50); m.push(180)
        pts = _spark_points(m, 0, 0, 100, 50)
        assert pts[1][1] == 0  # not negative, so not outside the frame


class TestRenderer:
    def _metrics(self):
        out = {}
        for key, label in [("cpu", "CPU"), ("ram", "RAM"), ("net_up", "NET")]:
            m = Metric(key, label, scale_max=100 if key != "net_up" else None)
            for i in range(20):
                m.push(40 + 10 * math.sin(i / 3))
            m.text, m.sub = "42", "%"
            out[key] = m
        return out

    def test_renders_exact_panel_geometry(self):
        img = DashboardRenderer(scale=1).render(self._metrics(), [("HOST", "test")])
        assert img.size == (T.WIDTH, T.HEIGHT)
        assert img.mode == "RGB"

    def test_supersampling_yields_same_output_size(self):
        img = DashboardRenderer(scale=3).render(self._metrics(), [("HOST", "test")])
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_alert_state_renders_without_error(self):
        metrics = self._metrics()
        metrics["cpu"].warn, metrics["cpu"].crit = 10, 20
        metrics["cpu"].arrow = "up"
        img = DashboardRenderer(scale=1).render(metrics, [("HOST", "test")])
        assert img.size == (T.WIDTH, T.HEIGHT)


class TestNameSegments:
    """Name truncation must keep the unique suffix and tint the project prefix."""

    def _draw(self):
        from PIL import Image, ImageDraw
        return ImageDraw.Draw(Image.new("RGB", (2000, 100)))

    def _width(self, r, d, text):
        return d.textlength(text, font=r.f_session)

    def test_short_derived_name_splits_untruncated(self):
        r = DashboardRenderer(scale=1)
        d = self._draw()
        segs = r._name_segments(d, "display-c0", "display", 10_000)
        assert segs == [("display", True), ("-c0", False)]

    def test_long_derived_name_keeps_suffix_with_ellipsis_in_prefix(self):
        r = DashboardRenderer(scale=1)
        d = self._draw()
        name = "p5-dist-zilla-plugin-docker-api-7d"
        project = "p5-dist-zilla-plugin-docker-api"
        full = self._width(r, d, name)
        segs = r._name_segments(d, name, project, full * 0.6)  # forces truncation
        assert len(segs) == 2
        prefix, suffix = segs
        # the real suffix survives, in the base color (is_prefix False)
        assert suffix == ("-7d", False)
        # the prefix is tinted, truncated, and carries the ellipsis
        assert prefix[1] is True
        assert prefix[0].endswith("…")
        assert prefix[0].startswith("p5")
        # and the whole thing actually fits the budget
        drawn = prefix[0] + suffix[0]
        assert self._width(r, d, drawn) <= full * 0.6

    def test_non_derived_name_is_single_base_segment(self):
        r = DashboardRenderer(scale=1)
        d = self._draw()
        segs = r._name_segments(d, "standalone-agent", "other", 10_000)
        assert segs == [("standalone-agent", False)]

    def test_tiny_budget_falls_back_to_whole_name_truncation(self):
        r = DashboardRenderer(scale=1)
        d = self._draw()
        name = "p5-dist-zilla-plugin-docker-api-7d"
        project = "p5-dist-zilla-plugin-docker-api"
        # Too narrow even for a minimal prefix + the suffix -> one base segment
        segs = r._name_segments(d, name, project, self._width(r, d, "-7dxx"))
        assert len(segs) == 1
        assert segs[0][1] is False
        assert segs[0][0].endswith("…")


class TestSplitLayout:
    def _sessions(self, n, waiting=1):
        now = time.time()
        out = []
        for i in range(n):
            status = "requires_action" if i < waiting else ("busy" if i % 2 else "idle")
            out.append(Session(pid=1000 + i, name=f"session-{i}", cwd=f"/home/user/project{i}",
                               status=status, kind="interactive", status_since=now - 60 * i))
        out.sort(key=lambda s: s.sort_key)
        return out

    def _four(self):
        out = {}
        for key in ("cpu", "ram", "temp", "load"):
            m = Metric(key, key.upper(), scale_max=100)
            for i in range(20):
                m.push(30 + i)
            m.text, m.sub = "42", "%"
            out[key] = m
        return out

    def test_split_renders_panel_geometry(self):
        img = DashboardRenderer(scale=1).render_split(
            self._four(), self._sessions(3), "3 sessions", [("HOST", "x")])
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_split_without_sessions(self):
        img = DashboardRenderer(scale=1).render_split(self._four(), [], "no sessions")
        assert img.size == (T.WIDTH, T.HEIGHT)

    def _limits(self):
        from datetime import datetime, timedelta, timezone
        from tzmrit_display.claude_limits import Limit, Limits
        now = datetime.now(timezone.utc)
        return Limits(
            session=Limit("Session", 14, "normal", now + timedelta(hours=4, minutes=20)),
            weekly=Limit("Weekly", 37, "normal", now + timedelta(days=3)),
        )

    def test_split_with_limit_bars_renders(self):
        img = DashboardRenderer(scale=1).render_split(
            self._four(), self._sessions(3, waiting=0), "3 sessions",
            [("HOST", "x")], self._limits())
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_limit_bars_render_with_a_waiting_session(self):
        """A waiting session no longer draws a footer notice; the wide bars
        occupy the full right half regardless."""
        img = DashboardRenderer(scale=1).render_split(
            self._four(), self._sessions(3, waiting=2), "3 sessions",
            [("HOST", "x")], self._limits())
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_split_with_many_sessions_does_not_overflow(self):
        img = DashboardRenderer(scale=1).render_split(
            self._four(), self._sessions(30), "30 sessions", [("HOST", "x")])
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_split_with_very_long_names(self):
        sessions = self._sessions(4)
        for s in sessions:
            s.name = "an-exceedingly-long-session-name-that-will-not-fit"
        img = DashboardRenderer(scale=1).render_split(
            self._four(), sessions, "4 sessions", [("HOST", "x")])
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_eight_sessions_fit_without_overflow(self):
        """Eight open agents is the normal case the layout must carry."""
        sessions = self._sessions(8, waiting=1)
        img = DashboardRenderer(scale=1).render_split(
            self._four(), sessions, "8 sessions", [("HOST", "x")])
        assert img.size == (T.WIDTH, T.HEIGHT)
        # Eight sessions must not need a "+N more" row: capacity is two
        # columns times four rows.
        room = (T.FOOTER_Y - 22) - (T.TILE_TOP + 40)
        assert (room // 54) * 2 >= 8


class TestPlatformMetrics:
    """Metric selection must survive platforms without load average or sensors."""

    def test_metric_set_adapts_without_loadavg(self, monkeypatch):
        import tzmrit_display.sources as sources
        monkeypatch.setattr(sources, "HAS_LOADAVG", False)
        monkeypatch.setattr(sources, "_cpu_temperature", lambda: None)
        src = sources.SystemSource()
        assert "load" not in src.metrics, "load average does not exist on Windows"
        assert "temp" not in src.metrics
        assert "disk" in src.metrics, "the freed slot must be filled"

    def test_layout_renders_with_the_windows_metric_set(self, monkeypatch):
        import tzmrit_display.sources as sources
        monkeypatch.setattr(sources, "HAS_LOADAVG", False)
        monkeypatch.setattr(sources, "_cpu_temperature", lambda: None)
        src = sources.SystemSource()
        src.sample()
        img = DashboardRenderer(scale=1).render(src.metrics, src.footer())
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_split_selection_prefers_disk_over_second_net_rate(self):
        from tzmrit_display.cli import SPLIT_METRICS
        available = ["cpu", "ram", "net_up", "net_down", "disk"]
        chosen = [k for k in SPLIT_METRICS if k in available][:4]
        assert chosen == ["cpu", "ram", "disk", "net_down"]
