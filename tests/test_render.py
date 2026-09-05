"""Tests for the layout and metric logic."""

import math
import time

import pytest

from tzmrit_display import theme as T
from tzmrit_display.claude_sessions import Session
from tzmrit_display.render import (
    DashboardRenderer,
    _BAR_INK_ON_FILL,
    _BAR_INK_ON_REMAIN,
    _BAR_REMAIN,
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


class TestSessionSubLine:
    """The sub-line under the session name: memory, then the model in use."""

    def _row(self, sess, width=900):
        """Draw one session row, returning every (xy, text, fill) drawn."""
        from PIL import Image, ImageDraw
        r = DashboardRenderer(scale=1)
        d = ImageDraw.Draw(Image.new("RGB", (2000, 200)))
        calls = []
        real = d.text

        def spy(xy, text, *a, **kw):
            calls.append((xy, text, kw.get("fill")))
            return real(xy, text, *a, **kw)

        d.text = spy
        r._session_row(d, sess, 0, width, 0)
        return r, d, calls

    def _session(self, **kw):
        return Session(pid=1, name="display-c0", cwd="/home/user/dev/display",
                       status="idle", kind="interactive", **kw)

    def _sub(self, calls):
        return [c for c in calls if "MB" in c[1] or "GB" in c[1]]

    def test_memory_and_model_share_one_line(self):
        sess = self._session(rss=512 * 1024 ** 2, model="claude-opus-5")
        _, _, calls = self._row(sess)
        sub = self._sub(calls)
        assert len(sub) == 1
        assert sub[0][1] == "512 MB · opus 5"

    def test_sub_line_is_ink_dim_not_ink_faint(self):
        """The maintainer found INK_FAINT too dark to read on the panel."""
        sess = self._session(rss=512 * 1024 ** 2, model="claude-opus-5")
        _, _, calls = self._row(sess)
        assert self._sub(calls)[0][2] == T.INK_DIM

    def test_unknown_model_leaves_the_line_where_it_was(self):
        """No transcript, no hit, IO error -> memory alone, same position."""
        with_model = self._session(rss=512 * 1024 ** 2, model="claude-opus-5")
        without = self._session(rss=512 * 1024 ** 2)
        _, _, a = self._row(with_model)
        _, _, b = self._row(without)
        assert self._sub(b)[0][1] == "512 MB"
        assert self._sub(a)[0][0] == self._sub(b)[0][0]

    def test_sub_line_stays_clear_of_the_status_word(self):
        """A wide model string must be clipped like the name is, not run into
        'waiting for you' on the right."""
        sess = self._session(rss=512 * 1024 ** 2,
                             model="a-very-long-third-party-model-identifier-x")
        r, d, calls = self._row(sess, width=320)
        drawn = self._sub(calls)[0]
        status_w = d.textlength(sess.status_text, font=r.f_session_sub)
        avail = 320 - drawn[0][0] - status_w - 20
        assert drawn[1].endswith("…")
        assert d.textlength(drawn[1], font=r.f_session_sub) <= avail

    def test_fit_leaves_a_fitting_string_untouched(self):
        from PIL import Image, ImageDraw
        r = DashboardRenderer(scale=1)
        d = ImageDraw.Draw(Image.new("RGB", (2000, 200)))
        assert r._fit(d, "512 MB · opus 5", r.f_session_sub, 10_000) \
            == "512 MB · opus 5"


def _contrast(a, b):
    """WCAG 2.x contrast ratio between two (r, g, b) tuples."""
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def lum(c):
        r, g, bl = (lin(v) for v in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl

    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


class TestLimitBarContrast:
    """The bar encodes its datum in the step between consumed and remaining,
    so that step carries the contrast budget - and the label ink has to follow
    whichever of the two it happens to sit on."""

    def test_consumed_versus_remaining_is_a_clear_step(self):
        # The old lightened track (t=0.75) managed only 1.62:1 here, which is
        # the weak step this replaces.
        assert _contrast(_rgb(T.ACCENT), _BAR_REMAIN) >= 4.0

    def test_dark_ink_clears_aa_on_the_bright_fill(self):
        assert _contrast(_rgb(_BAR_INK_ON_FILL), _rgb(T.ACCENT)) >= 4.5

    def test_light_ink_clears_aa_on_the_dark_track(self):
        assert _contrast(_rgb(_BAR_INK_ON_REMAIN), _BAR_REMAIN) >= 4.5

    def test_each_ink_would_fail_on_the_other_ground(self):
        """Why two inks and not one: neither survives both backgrounds, which
        is exactly what makes the pixel-exact split necessary."""
        assert _contrast(_rgb(_BAR_INK_ON_FILL), _BAR_REMAIN) < 4.5
        assert _contrast(_rgb(_BAR_INK_ON_REMAIN), _rgb(T.ACCENT)) < 4.5

    def test_track_stays_visible_against_the_panel(self):
        # Chrome, not datum: in family with the INK_FAINT rules around it.
        assert _contrast(_BAR_REMAIN, _rgb(T.SURFACE)) \
            >= _contrast(_rgb(T.INK_FAINT), _rgb(T.SURFACE)) * 0.75


class TestLimitBarInk:
    """Every label pixel must be drawn in the ink its own background wants,
    at any fill level - including a label straddling the fill edge."""

    BX, BW, BY, BH, PAD = 40, 400, 20, 30, 16

    def _limit(self, percent):
        from datetime import datetime, timedelta, timezone
        from tzmrit_display.claude_limits import Limit
        return Limit("Session", percent, "normal",
                     datetime.now(timezone.utc) + timedelta(days=2, hours=1))

    def _bar(self, percent):
        from PIL import Image, ImageDraw
        r = DashboardRenderer(scale=1)
        img = Image.new("RGBA", (500, 80), T.SURFACE)
        d = ImageDraw.Draw(img)
        r._limit_bars(img, d, [self._limit(percent)],
                      self.BX, self.BX + self.BW, self.BY)
        return r, d, img

    def _band(self, img, x_from, x_to):
        """Luminance (as contrast against black) of a slice of the label band."""
        px = img.convert("RGB").load()
        return [_contrast(px[x, y], (0, 0, 0))
                for x in range(int(x_from), int(x_to))
                for y in range(self.BY + 6, self.BY + self.BH - 6)]

    def _has_dark_ink(self, band):
        return min(band) < _contrast(_rgb(T.ACCENT), (0, 0, 0)) * 0.5

    def _has_light_ink(self, band):
        return max(band) > _contrast(_BAR_REMAIN, (0, 0, 0)) * 2

    def test_label_is_dark_ink_when_the_bar_is_full(self):
        # 100%: the label sits wholly on the bright fill -> dark glyphs.
        _, _, img = self._bar(100)
        assert self._has_dark_ink(self._band(img, self.BX + self.PAD, self.BX + 120))

    def test_label_is_light_ink_when_the_bar_is_empty(self):
        # 0%: the label sits wholly on the dark track -> light glyphs.
        _, _, img = self._bar(0)
        assert self._has_light_ink(self._band(img, self.BX + self.PAD, self.BX + 120))

    def _straddle(self, r, d, span_start, span_end, percent):
        """Assert the edge really falls inside this piece, then check both inks.

        The precondition matters: a piece that has quietly stopped crossing the
        edge would let the ink assertions pass without testing anything.
        """
        edge = self.BX + self.BW * percent / 100
        assert span_start < edge - 1 and edge + 1 < span_end, \
            f"the fill edge at {edge} does not cross the piece {span_start}..{span_end}"
        _, _, img = self._bar(percent)
        assert self._has_dark_ink(self._band(img, span_start, edge - 1)), \
            "no dark ink on the filled side of the edge"
        assert self._has_light_ink(self._band(img, edge + 1, span_end)), \
            "no light ink on the unfilled side of the edge"

    def test_a_straddling_left_label_uses_both_inks(self):
        """The normal case, not a corner one: 'Session 13%' starts one pad in
        while the edge sits at 13% of the bar, so the piece is cut in two."""
        r, d, _ = self._bar(0)
        percent = 12
        for _ in range(3):  # the width depends on the number it prints
            w = d.textlength(f"Session {percent}%", font=r.f_small)
            percent = round(100 * (self.PAD + w / 2) / self.BW)
        w = d.textlength(f"Session {percent}%", font=r.f_small)
        self._straddle(r, d, self.BX + self.PAD, self.BX + self.PAD + w, percent)

    def test_a_straddling_reset_text_uses_both_inks(self):
        """The right-anchored piece straddles instead once the bar is nearly
        full - the same split has to serve it."""
        r, d, _ = self._bar(0)
        reset = self._limit(95).reset_text()
        w = d.textlength(reset, font=r.f_small)
        right = self.BX + self.BW - self.PAD
        percent = round(100 * (right - w / 2 - self.BX) / self.BW)
        self._straddle(r, d, right - w, right, percent)


class TestLimitBarFit:
    """Bar count is driven by the payload (a scoped weekly per scoped model),
    so the label and the countdown must not be allowed to meet in the middle."""

    # The real right half at scale 1, so the fit decisions are the panel's.
    X0, X1, GAP, PAD = int(T.WIDTH * 0.46) + 34, T.WIDTH - T.MARGIN_X, 18, 16

    def _pieces(self, rows):
        """The text pieces `_limit_bars` decides to draw, per bar."""
        from PIL import Image, ImageDraw
        r = DashboardRenderer(scale=1)
        captured = []
        r._bar_text = lambda base, pieces, *a: captured.append(pieces)
        img = Image.new("RGBA", (T.WIDTH, 80), T.SURFACE)
        r._limit_bars(img, ImageDraw.Draw(img), rows, self.X0, self.X1, 20)
        return r, captured

    def _bar_width(self, n):
        return (self.X1 - self.X0 - (n - 1) * self.GAP) / n

    def _limits(self, n, label="Session", percent=100, reset_hours=10):
        from datetime import datetime, timedelta, timezone
        from tzmrit_display.claude_limits import Limit
        when = datetime.now(timezone.utc) + timedelta(hours=reset_hours, minutes=5)
        return [Limit(label, percent, "normal", when) for _ in range(n)]

    def test_a_wide_bar_keeps_the_countdown(self):
        rows = self._limits(1)
        _, pieces = self._pieces(rows)
        assert len(pieces[0]) == 2
        assert pieces[0][1][1] == rows[0].reset_text()

    def test_a_narrow_bar_drops_the_countdown_rather_than_colliding(self):
        """Reproduced before the fix as 'Session 100%10h04m' - two pieces
        printed over each other into mush."""
        _, pieces = self._pieces(self._limits(4))
        assert all(len(p) == 1 for p in pieces), \
            "the countdown must give way once it no longer fits"
        assert pieces[0][0][1] == "Session 100%"

    def test_kept_pieces_never_overlap(self):
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        for n in (1, 2, 3, 4, 5):
            r, bars = self._pieces(self._limits(n))
            for pieces in bars:
                if len(pieces) < 2:
                    continue
                (lx, _), ltext, _ = pieces[0]
                (rx, _), rtext, _ = pieces[1]
                assert lx + d.textlength(ltext, font=r.f_small) \
                    <= rx - d.textlength(rtext, font=r.f_small), \
                    f"label and countdown overlap with {n} bars"

    def test_an_over_long_name_is_clipped_but_keeps_its_number(self):
        """Truncate the name, never the datum - "An Extremely …" alone is a
        bar that says nothing."""
        from PIL import Image, ImageDraw
        rows = self._limits(5, label="An Extremely Long Scoped Model Name", percent=41)
        r, bars = self._pieces(rows)
        d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        text = bars[0][0][1]
        assert text.endswith(" 41%")
        assert "…" in text
        assert d.textlength(text, font=r.f_small) <= self._bar_width(5) - 2 * self.PAD


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
