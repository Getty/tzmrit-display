"""Tests for the layout and metric logic."""

import math
import time

from display_panel import theme as T
from display_panel.claude_sessions import Session
from display_panel.render import DashboardRenderer, _spark_points
from display_panel.sources import Metric


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
        import display_panel.sources as sources
        monkeypatch.setattr(sources, "HAS_LOADAVG", False)
        monkeypatch.setattr(sources, "_cpu_temperature", lambda: None)
        src = sources.SystemSource()
        assert "load" not in src.metrics, "load average does not exist on Windows"
        assert "temp" not in src.metrics
        assert "disk" in src.metrics, "the freed slot must be filled"

    def test_layout_renders_with_the_windows_metric_set(self, monkeypatch):
        import display_panel.sources as sources
        monkeypatch.setattr(sources, "HAS_LOADAVG", False)
        monkeypatch.setattr(sources, "_cpu_temperature", lambda: None)
        src = sources.SystemSource()
        src.sample()
        img = DashboardRenderer(scale=1).render(src.metrics, src.footer())
        assert img.size == (T.WIDTH, T.HEIGHT)

    def test_split_selection_prefers_disk_over_second_net_rate(self):
        from display_panel.cli import SPLIT_METRICS
        available = ["cpu", "ram", "net_up", "net_down", "disk"]
        chosen = [k for k in SPLIT_METRICS if k in available][:4]
        assert chosen == ["cpu", "ram", "disk", "net_down"]
