"""Tests for the layout and metric logic."""

import math
import time
from dataclasses import FrozenInstanceError

import pytest

from tzmrit_display import theme as T
from tzmrit_display.claude_sessions import Session
from tzmrit_display.render import (
    DashboardRenderer,
    _color,
    _inactive_color,
    _spark_points,
)
from tzmrit_display.sources import Metric


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


class TestInactivityColor:
    """The session inactivity counter escalates in color, but with MUTED
    blends - never the pure reserved WARN/CRIT."""

    def test_palette_argument_is_required(self):
        with pytest.raises(TypeError):
            _color("ok")
        with pytest.raises(TypeError):
            _inactive_color(0)

    def test_color_uses_the_supplied_blue_palette(self):
        assert _color("ok", T.BLUE_PALETTE) == T.ACCENT
        assert _color("warn", T.BLUE_PALETTE) == T.WARN
        assert _color("crit", T.BLUE_PALETTE) == T.CRIT

    def test_under_five_minutes_is_neutral(self):
        assert _inactive_color(0, T.BLUE_PALETTE) == T.INK_FAINT
        assert _inactive_color(299, T.BLUE_PALETTE) == T.INK_FAINT

    def test_five_minutes_is_muted_yellow(self):
        expected = tuple(round(a + (b - a) * 0.55)
                         for a, b in zip(_rgb(T.INK_DIM), _rgb(T.WARN)))
        assert _inactive_color(300, T.BLUE_PALETTE) == expected
        assert _inactive_color(3599, T.BLUE_PALETTE) == expected

    def test_one_hour_is_muted_red(self):
        expected = tuple(round(a + (b - a) * 0.55)
                         for a, b in zip(_rgb(T.INK_DIM), _rgb(T.CRIT)))
        assert _inactive_color(3600, T.BLUE_PALETTE) == expected
        assert _inactive_color(7200, T.BLUE_PALETTE) == expected

    def test_muted_blends_are_not_the_pure_status_colors(self):
        warn = _inactive_color(300, T.BLUE_PALETTE)
        crit = _inactive_color(3600, T.BLUE_PALETTE)
        assert warn != _rgb(T.WARN)
        assert crit != _rgb(T.CRIT)
        # and clearly toward gray: darker/less saturated than the pure hue
        assert warn[0] < _rgb(T.WARN)[0]
        assert crit[0] < _rgb(T.CRIT)[0]


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


class TestRendererThemes:
    """Theme selection changes only normal accents, never status semantics."""

    @staticmethod
    def _metric(value, *, warn=None, crit=None):
        metric = Metric("cpu", "CPU", scale_max=100, warn=warn, crit=crit)
        metric.push(0)
        metric.push(value)
        metric.text, metric.sub = str(value), "%"
        return metric

    @staticmethod
    def _render_metric(renderer, metric):
        return renderer.render({"cpu": metric}, [])

    @staticmethod
    def _split_metrics():
        metrics = {}
        for key in ("cpu", "ram", "temp", "load"):
            metric = Metric(key, key.upper(), scale_max=100)
            metric.push(30)
            metric.push(40)
            metric.text, metric.sub = "42", "%"
            metrics[key] = metric
        return metrics

    @pytest.mark.parametrize(("theme", "alpha", "fill"), [
        ("blue", 46, (25, 41, 61)),
        ("red", 72, (72, 33, 37)),
    ])
    def test_normal_sparkline_line_and_area_have_the_theme_visibility(
            self, theme, alpha, fill):
        """The interior pixel verifies the actual alpha-composited fill."""
        renderer = DashboardRenderer(scale=1, theme=theme)
        image = self._render_metric(renderer, self._metric(100))

        assert renderer.palette.spark_fill_alpha == alpha
        assert image.getpixel((1701, 258)) == _rgb(renderer.palette.accent)
        assert image.getpixel((900, 350)) == fill

    def test_red_theme_colors_running_session_marker_and_status_text(self):
        session = Session(pid=1, name="session", cwd="/tmp", status="busy",
                          kind="interactive", active_at=time.time())
        image = DashboardRenderer(scale=1, theme="red").render_split(
            self._split_metrics(), [session], "1 session", [("HOST", "test")]
        )

        assert image.getpixel((735, 159)) == _rgb(T.RED_PALETTE.accent)
        status = image.crop((1000, 144, 1105, 175))
        colors = status.getcolors(maxcolors=status.width * status.height)
        assert colors is not None
        assert _rgb(T.RED_PALETTE.accent) in {color for _, color in colors}

    def test_red_theme_preserves_warn_and_crit_pixels(self):
        renderer = DashboardRenderer(scale=1, theme="red")
        warn = self._render_metric(renderer, self._metric(60, warn=50, crit=90))
        crit = self._render_metric(renderer, self._metric(100, warn=50, crit=90))

        assert warn.getpixel((1701, 303)) == _rgb(T.WARN)
        assert crit.getpixel((1701, 258)) == _rgb(T.CRIT)

    def test_renderer_palettes_are_immutable_and_isolated(self):
        blue = DashboardRenderer(scale=1)
        red = DashboardRenderer(scale=1, theme="red")
        metric = self._metric(100)

        blue_before = self._render_metric(blue, metric)
        red_image = self._render_metric(red, metric)
        blue_after = self._render_metric(blue, metric)

        assert blue_before.getpixel((1701, 258)) == _rgb(T.ACCENT)
        assert red_image.getpixel((1701, 258)) == _rgb(T.RED_PALETTE.accent)
        assert blue_after.getpixel((1701, 258)) == _rgb(T.ACCENT)
        with pytest.raises(FrozenInstanceError):
            red.palette.accent = T.ACCENT

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


def _oklab(rgb):
    """OKLab coordinates for an sRGB tuple."""
    def linear(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v) for v in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (value ** (1 / 3) for value in (l, m, s))
    return (
        0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
    )


def _oklab_distance(a, b):
    return 100 * math.sqrt(sum((x - y) ** 2 for x, y in zip(_oklab(a), _oklab(b))))


def _deuteranopia(rgb):
    """Apply Machado et al.'s full-severity deuteranopia matrix in linear RGB."""
    def linear(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    linear_rgb = tuple(linear(v) for v in rgb)
    matrix = (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    )

    def gamma(c):
        c = max(0, min(1, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055

    return tuple(255 * gamma(sum(x * y for x, y in zip(row, linear_rgb)))
                 for row in matrix)


class TestRedThemeColorValidation:
    def test_normal_red_is_aa_and_distinct_from_brighter_critical_coral(self):
        red = _rgb(T.RED_PALETTE.accent)
        crit = _rgb(T.CRIT)
        assert red == (227, 84, 84)
        red_crit = _oklab_distance(red, crit)
        red_crit_deuteranopia = _oklab_distance(_deuteranopia(red), _deuteranopia(crit))

        assert _contrast(red, _rgb(T.SURFACE)) == pytest.approx(5.24030644, abs=0.00000001)
        assert red_crit == pytest.approx(9.91210549, abs=0.00000001)
        assert red_crit_deuteranopia == pytest.approx(9.62569839, abs=0.00000001)
        assert _contrast(crit, _rgb(T.SURFACE)) > _contrast(red, _rgb(T.SURFACE))

    def test_documented_warn_and_crit_distance_is_preserved(self):
        warn = _rgb(T.WARN)
        crit = _rgb(T.CRIT)

        assert _oklab_distance(warn, crit) == pytest.approx(20.09898, abs=0.00001)
        assert _oklab_distance(_deuteranopia(warn), _deuteranopia(crit)) \
            == pytest.approx(13.00115, abs=0.00001)


class TestLimitBarContrast:
    """The bar's rendered fill, remaining track and label inks carry its
    contrast contract for each actual renderer palette."""

    @staticmethod
    def _limit(percent):
        from datetime import datetime, timedelta, timezone
        from tzmrit_display.claude_limits import Limit
        return Limit("Session", percent, "normal", datetime.now(timezone.utc) + timedelta(days=2))

    def _bar(self, renderer, percent):
        from PIL import Image, ImageDraw
        image = Image.new("RGBA", (500, 80), renderer.palette.surface)
        renderer._limit_bars(image, ImageDraw.Draw(image), [self._limit(percent)], 40, 440, 20)
        return image

    @pytest.mark.parametrize(("theme", "expected_remain", "minimum_step"), [
        ("blue", (38, 67, 101), 4.0),
        ("red", (87, 38, 41), 3.3),
    ])
    def test_runtime_fill_and_remain_colors_have_a_clear_step(
            self, theme, expected_remain, minimum_step):
        renderer = DashboardRenderer(scale=1, theme=theme)
        empty = self._bar(renderer, 0)
        full = self._bar(renderer, 100)

        assert renderer._bar_remain == expected_remain
        assert empty.getpixel((240, 35)) == expected_remain + (255,)
        assert full.getpixel((240, 35)) == _rgb(renderer.palette.accent) + (255,)
        assert _contrast(_rgb(renderer.palette.accent), renderer._bar_remain) >= minimum_step

    @pytest.mark.parametrize("theme", ("blue", "red"))
    def test_runtime_label_inks_clear_their_own_grounds(self, theme):
        renderer = DashboardRenderer(scale=1, theme=theme)

        assert _contrast(_rgb(renderer.palette.surface), _rgb(renderer.palette.accent)) >= 4.5
        assert _contrast(_rgb(renderer.palette.ink), renderer._bar_remain) >= 4.5

    @pytest.mark.parametrize("theme", ("blue", "red"))
    def test_runtime_label_inks_fail_on_the_opposite_ground(self, theme):
        """Neither label ink may be reused for both halves of a rendered bar."""
        renderer = DashboardRenderer(scale=1, theme=theme)

        assert _contrast(_rgb(renderer.palette.surface), renderer._bar_remain) < 4.5
        assert _contrast(_rgb(renderer.palette.ink), _rgb(renderer.palette.accent)) < 4.5


class TestLimitBarInk:
    """Every rendered label pixel uses the ink for its own bar ground, at any
    fill level - including a label straddling the fill edge."""

    BX, BW, BY, BH, PAD = 40, 400, 20, 30, 16

    def _limit(self, percent):
        from datetime import datetime, timedelta, timezone
        from tzmrit_display.claude_limits import Limit
        return Limit("Session", percent, "normal",
                     datetime.now(timezone.utc) + timedelta(days=2, hours=1))

    def _bar(self, theme, percent):
        from PIL import Image, ImageDraw
        renderer = DashboardRenderer(scale=1, theme=theme)
        image = Image.new("RGBA", (500, 80), renderer.palette.surface)
        drawing = ImageDraw.Draw(image)
        renderer._limit_bars(image, drawing, [self._limit(percent)],
                             self.BX, self.BX + self.BW, self.BY)
        return renderer, drawing, image

    def _has_ink(self, image, x_from, x_to, ink):
        crop = image.convert("RGB").crop((int(x_from), self.BY + 6,
                                           max(int(x_from) + 1, int(x_to)),
                                           self.BY + self.BH - 6))
        colors = crop.getcolors(maxcolors=crop.width * crop.height)
        assert colors is not None
        return _rgb(ink) in {color for _, color in colors}

    @pytest.mark.parametrize("theme", ("blue", "red"))
    def test_label_is_surface_ink_when_the_bar_is_full(self, theme):
        # 100%: the label sits wholly on the bright fill -> palette surface ink.
        renderer, _, image = self._bar(theme, 100)
        assert self._has_ink(image, self.BX + self.PAD, self.BX + 120,
                             renderer.palette.surface)

    @pytest.mark.parametrize("theme", ("blue", "red"))
    def test_label_is_palette_ink_when_the_bar_is_empty(self, theme):
        # 0%: the label sits wholly on the dark track -> palette ink.
        renderer, _, image = self._bar(theme, 0)
        assert self._has_ink(image, self.BX + self.PAD, self.BX + 120,
                             renderer.palette.ink)

    def _straddle(self, theme, renderer, span_start, span_end, percent):
        """Assert the edge really crosses the text, then check its actual inks."""
        edge = self.BX + self.BW * percent / 100
        assert span_start < edge - 1 and edge + 1 < span_end, \
            f"the fill edge at {edge} does not cross the piece {span_start}..{span_end}"
        _, _, image = self._bar(theme, percent)
        assert self._has_ink(image, span_start, edge - 1, renderer.palette.surface), \
            "no surface ink on the filled side of the edge"
        assert self._has_ink(image, edge + 1, span_end, renderer.palette.ink), \
            "no palette ink on the unfilled side of the edge"

    @pytest.mark.parametrize("theme", ("blue", "red"))
    def test_a_straddling_left_label_uses_both_actual_inks(self, theme):
        """The normal case: the fill edge cuts the left label in two."""
        renderer, drawing, _ = self._bar(theme, 0)
        percent = 12
        for _ in range(3):  # the width depends on the number it prints
            width = drawing.textlength(f"Session {percent}%", font=renderer.f_small)
            percent = round(100 * (self.PAD + width / 2) / self.BW)
        width = drawing.textlength(f"Session {percent}%", font=renderer.f_small)
        self._straddle(theme, renderer,
                       self.BX + self.PAD, self.BX + self.PAD + width, percent)

    @pytest.mark.parametrize("theme", ("blue", "red"))
    def test_a_straddling_reset_text_uses_both_actual_inks(self, theme):
        """The right-anchored countdown straddles once the bar is nearly full."""
        renderer, drawing, _ = self._bar(theme, 0)
        reset = self._limit(95).reset_text()
        width = drawing.textlength(reset, font=renderer.f_small)
        right = self.BX + self.BW - self.PAD
        percent = round(100 * (right - width / 2 - self.BX) / self.BW)
        self._straddle(theme, renderer, right - width, right, percent)


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


class TestSplitStaticLeftLayout:
    """The split view's left third is a fixed 2x2 metric-card grid."""

    CARDS = (
        (34, 94, 315, 256),
        (331, 94, 612, 256),
        (34, 270, 315, 432),
        (331, 270, 612, 432),
    )
    GRAPHS = (
        (44, 134, 305, 246),
        (341, 134, 602, 246),
        (44, 310, 305, 422),
        (341, 310, 602, 422),
    )

    @staticmethod
    def _metric(key, text="", history=()):
        metric = Metric(key, "", scale_max=100)
        for value in history:
            metric.push(value)
        metric.text = text
        metric.sub = ""
        return metric

    def _render(self, metrics, *, scale=1):
        return DashboardRenderer(scale=scale).render_split(
            metrics, [], "no sessions", [("HOST", "host")]
        )

    @staticmethod
    def _distance(pixel, color):
        return sum((a - b) ** 2 for a, b in zip(pixel, color))

    @classmethod
    def _runs(cls, image, *, y, color, background, x0=0, x1=T.WIDTH):
        matches = [
            x for x in range(x0, x1)
            if cls._distance(image.getpixel((x, y)), color)
            < cls._distance(image.getpixel((x, y)), background)
        ]
        runs = []
        for x in matches:
            if not runs or x != runs[-1][1] + 1:
                runs.append([x, x])
            else:
                runs[-1][1] = x
        return [tuple(run) for run in runs]

    @classmethod
    def _column_runs(cls, image, *, x, color, background, y0=0, y1=T.HEIGHT):
        matches = [
            y for y in range(y0, y1)
            if cls._distance(image.getpixel((x, y)), color)
            < cls._distance(image.getpixel((x, y)), background)
        ]
        runs = []
        for y in matches:
            if not runs or y != runs[-1][1] + 1:
                runs.append([y, y])
            else:
                runs[-1][1] = y
        return [tuple(run) for run in runs]

    @staticmethod
    def _assert_runs_close(actual, expected, tolerance=1):
        assert len(actual) == len(expected)
        for got, want in zip(actual, expected):
            assert abs(got[0] - want[0]) <= tolerance
            assert abs(got[1] - want[1]) <= tolerance

    @staticmethod
    def _color_bbox(image, color, box, tolerance=0):
        points = [
            (x, y)
            for y in range(box[1], box[3] + 1)
            for x in range(box[0], box[2] + 1)
            if max(abs(a - b) for a, b in zip(image.getpixel((x, y)), color))
            <= tolerance
        ]
        if not points:
            return None
        xs, ys = zip(*points)
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _has_color(image, color, box, tolerance=0):
        return any(
            max(abs(a - b) for a, b in zip(image.getpixel((x, y)), color))
            <= tolerance
            for y in range(box[1], box[3])
            for x in range(box[0], box[2])
        )

    @pytest.mark.parametrize("scale", (1, 2))
    def test_split_output_is_rgb_panel_geometry_at_each_supported_scale(self, scale):
        image = self._render({"cpu": self._metric("cpu", "42", (30, 40))}, scale=scale)
        assert image.mode == "RGB"
        assert image.size == (1920, 462)
        center = sum(image.getpixel((640, 200)))
        assert center > sum(image.getpixel((639, 200)))
        assert center > sum(image.getpixel((641, 200)))

    @pytest.mark.parametrize("scale", (1, 2))
    def test_divider_and_card_interiors_have_exact_downsampled_geometry(self, scale):
        metrics = {str(i): self._metric(str(i)) for i in range(4)}
        image = self._render(metrics, scale=scale)
        tile = _rgb(T.SURFACE_TILE)
        surface = _rgb(T.SURFACE)
        expected_columns = [(34, 315), (331, 612)]

        for y in (95, 255, 271, 431):
            runs = self._runs(
                image, y=y, color=tile, background=surface, x0=0, x1=613
            )
            self._assert_runs_close(runs, expected_columns)
        rows = self._column_runs(
            image, x=35, color=tile, background=surface, y0=80, y1=440
        )
        self._assert_runs_close(rows, [(94, 256), (270, 432)])

        divider = _rgb(T.INK_FAINT)
        divider_rows = self._column_runs(
            image, x=640, color=divider, background=surface, y0=80, y1=440
        )
        self._assert_runs_close(divider_rows, [(94, 432)])
        distances = {
            x: self._distance(image.getpixel((x, 200)), divider)
            for x in range(637, 644)
        }
        assert min(distances, key=distances.get) == 640

    @pytest.mark.parametrize("scale", (1, 2))
    def test_four_metric_values_follow_row_major_reading_order_inside_graphs(self, scale):
        metrics = {
            str(i): self._metric(str(i), str(i) * i)
            for i in range(1, 5)
        }
        image = self._render(metrics, scale=scale)
        tolerance = 0 if scale == 1 else 25
        bboxes = [
            self._color_bbox(image, _rgb(T.INK), card, tolerance)
            for card in self.CARDS
        ]

        assert all(bbox is not None for bbox in bboxes)
        widths = [bbox[2] - bbox[0] + 1 for bbox in bboxes]
        assert widths == sorted(widths)
        assert len(set(widths)) == 4
        for bbox, graph in zip(bboxes, self.GRAPHS):
            assert graph[1] <= bbox[1] <= bbox[3] <= graph[3]

    @pytest.mark.parametrize("scale", (1, 2))
    def test_value_ink_is_drawn_over_a_graph_that_crosses_its_bbox(self, scale):
        plain = self._render({"cpu": self._metric("cpu", "8888")}, scale=scale)
        graphed = self._render({
            "cpu": self._metric("cpu", "8888", history=(60, 60))
        }, scale=scale)
        value_box = (44, 134, 200, 200)

        def ink_points(image):
            return {
                (x, y)
                for y in range(value_box[1], value_box[3])
                for x in range(value_box[0], value_box[2])
                if image.getpixel((x, y)) == _rgb(T.INK)
            }

        assert ink_points(plain)
        assert ink_points(graphed) == ink_points(plain)
        assert self._has_color(
            graphed, _rgb(T.ACCENT), value_box, 0 if scale == 1 else 25
        )

    @pytest.mark.parametrize("scale", (1, 2))
    def test_each_card_renders_a_261_by_112_graph_inside_its_edges(self, scale):
        metrics = {
            str(i): self._metric(str(i), history=(0, 100))
            for i in range(4)
        }
        image = self._render(metrics, scale=scale)
        accent = _rgb(T.ACCENT)
        tile = _rgb(T.SURFACE_TILE)
        tolerance = 0 if scale == 1 else 25

        for card, graph in zip(self.CARDS, self.GRAPHS):
            gx0, gy0, gx1, gy1 = graph
            assert (gx1 - gx0, gy1 - gy0) == (261, 112)
            assert card[0] <= gx0 < gx1 <= card[2]
            assert card[1] <= gy0 < gy1 <= card[3]
            assert self._has_color(
                image, accent, (gx0, gy1 - 2, gx0 + 4, gy1 + 1), tolerance
            )
            assert self._has_color(
                image, accent, (gx1 - 4, gy0 - 4, gx1 + 5, gy0 + 5), tolerance
            )
            assert image.getpixel(((gx0 + gx1) // 2, gy0 + 3 * (gy1 - gy0) // 4)) \
                not in (tile, accent)
            accent_bbox = self._color_bbox(image, accent, card, tolerance)
            assert accent_bbox is not None
            expected_bbox = (gx0, gy0 - 4, gx1 + 3, gy1 + 1)
            assert all(abs(got - want) <= 1
                       for got, want in zip(accent_bbox, expected_bbox))
            assert (card[0] <= accent_bbox[0] <= accent_bbox[2] <= card[2]
                    and card[1] <= accent_bbox[1] <= accent_bbox[3] <= card[3])

    @pytest.mark.parametrize("scale", (1, 2))
    @pytest.mark.parametrize(("value", "warn", "crit", "status_color"), [
        (60, 50, 90, T.WARN),
        (100, 50, 90, T.CRIT),
    ])
    def test_alert_card_keeps_marker_arrow_value_scrim_line_and_endpoint(
            self, scale, value, warn, crit, status_color):
        from PIL import ImageChops

        def scene(text):
            metric = self._metric("cpu", text, (64, 64))
            metric.value = value
            metric.warn, metric.crit, metric.arrow = warn, crit, "up"
            return self._render({"cpu": metric}, scale=scale)

        image = scene(str(value))
        without_value = scene("")
        color = _rgb(status_color)
        tile = _rgb(T.SURFACE_TILE)
        tolerance = 0 if scale == 1 else 25
        scrim_tolerance = 0 if scale == 1 else 10
        line_tolerance = 0 if scale == 1 else 35
        gx0, gy0, gx1, gy1 = self.GRAPHS[0]
        line_y = round(gy1 - 0.64 * (gy1 - gy0))

        assert self._has_color(
            image, color, (44, 103, 64, 123), tolerance
        ), "warning triangle"
        assert self._has_color(
            image, color, (80, 103, 94, 126), tolerance
        ), "direction arrow"
        assert self._has_color(
            image, color, (gx0, line_y - 2, gx0 + 4, line_y + 3), tolerance
        ), "line"
        assert self._has_color(
            image, color, (gx1 - 4, line_y - 4, gx1 + 5, line_y + 5), tolerance
        ), "endpoint"

        card = (34, 94, 316, 257)
        local_diff = ImageChops.difference(
            image.crop(card), without_value.crop(card)
        ).getbbox()
        assert local_diff is not None, "missing value must leave no false-positive graph color"
        value_bbox = (
            local_diff[0] + card[0], local_diff[1] + card[1],
            local_diff[2] + card[0], local_diff[3] + card[1],
        )
        assert gy0 <= value_bbox[1] < value_bbox[3] <= gy1 + 1

        changed = [
            ((x, y), image.getpixel((x, y)), without_value.getpixel((x, y)))
            for y in range(value_bbox[1], value_bbox[3])
            for x in range(value_bbox[0], value_bbox[2])
            if image.getpixel((x, y)) != without_value.getpixel((x, y))
        ]
        assert any(
            y < line_y - 4
            and max(abs(a - b) for a, b in zip(with_value, color)) <= tolerance
            and max(abs(a - b) for a, b in zip(without, color)) > tolerance
            for (_, y), with_value, without in changed
        ), "value text itself must introduce the status color above the graph line"
        assert any(
            max(abs(a - b) for a, b in zip(with_value, tile)) <= scrim_tolerance
            and max(abs(a - b) for a, b in zip(without, color)) <= line_tolerance
            for _, with_value, without in changed
        ), "surface_tile scrim must cover the crossing sparkline before text"


class TestSplitStaticRightLayout:
    """The split view's right side has two fixed lanes and one utility column."""

    LANES = ((674, 1104), (1128, 1558))
    UTILITY = (1582, 1886)
    SESSION_Y = 144
    SESSION_STEP = 58
    LIMIT_Y = 138
    LIMIT_STEP = 36

    @staticmethod
    def _session(index, status="busy"):
        now = time.time()
        return Session(
            pid=2000 + index,
            name=f"session-{index}",
            cwd=f"/home/user/project-{index}",
            status=status,
            kind="interactive",
            status_since=now,
            active_at=now,
        )

    @staticmethod
    def _metrics():
        metrics = {}
        for key in ("cpu", "ram", "temp", "load"):
            metric = Metric(key, "", scale_max=100)
            metric.text = ""
            metrics[key] = metric
        return metrics

    @staticmethod
    def _limits(percents):
        from datetime import datetime, timedelta, timezone
        from tzmrit_display.claude_limits import Limit, Limits

        reset = datetime.now(timezone.utc) + timedelta(days=2)
        rows = [Limit(f"Scoped {i}", percent, "normal", reset)
                for i, percent in enumerate(percents)]
        return Limits(
            session=Limit("Session", percents[0], "normal", reset)
            if len(percents) > 0 else None,
            weekly=Limit("Weekly", percents[1], "normal", reset)
            if len(percents) > 1 else None,
            scoped=rows[2:],
        )

    @staticmethod
    def _footer(suffix=""):
        prefix = "value-" + "x" * 80
        return [("HOST", prefix + suffix), ("DISK", prefix + suffix),
                ("UPTIME", prefix + suffix)]

    def _render(self, sessions=(), limits=None, footer=None, *, scale=1):
        return DashboardRenderer(scale=scale).render_split(
            self._metrics(), list(sessions), f"{len(sessions)} sessions",
            footer, limits,
        )

    @staticmethod
    def _has_color(image, color, box, tolerance=0):
        return any(
            max(abs(a - b) for a, b in zip(image.getpixel((x, y)), color))
            <= tolerance
            for y in range(box[1], box[3])
            for x in range(box[0], box[2])
        )

    @staticmethod
    def _color_bbox(image, color, box, tolerance=0):
        points = [
            (x, y)
            for y in range(box[1], box[3])
            for x in range(box[0], box[2])
            if max(abs(a - b) for a, b in zip(image.getpixel((x, y)), color))
            <= tolerance
        ]
        if not points:
            return None
        xs, ys = zip(*points)
        return min(xs), min(ys), max(xs), max(ys)

    def _marker_box(self, slot):
        lane, row = divmod(slot, 5)
        x0, _ = self.LANES[lane]
        y = self.SESSION_Y + row * self.SESSION_STEP
        return x0 + 52, y + 4, x0 + 74, y + 27

    @pytest.mark.parametrize("scale", (1, 2))
    def test_ten_sessions_fill_lane_one_then_lane_two_in_supplied_order(self, scale):
        statuses = ["idle", "busy", "requires_action", "busy", "idle",
                    "requires_action", "idle", "busy", "requires_action", "busy"]
        sessions = [self._session(i, status) for i, status in enumerate(statuses)]
        image = self._render(sessions, scale=scale)
        tolerance = 0 if scale == 1 else 25
        colors = {
            "idle": _rgb(T.INK_FAINT),
            "busy": _rgb(T.ACCENT),
            "requires_action": _rgb(T.WARN),
        }

        for slot, status in enumerate(statuses):
            marker_bbox = self._color_bbox(
                image, colors[status], self._marker_box(slot), tolerance
            )
            assert marker_bbox is not None
            lane, row = divmod(slot, 5)
            marker_cx = (marker_bbox[0] + marker_bbox[2]) / 2
            marker_cy = (marker_bbox[1] + marker_bbox[3]) / 2
            assert abs(marker_cx - (self.LANES[lane][0] + 61)) <= 2
            assert abs(marker_cy - (self.SESSION_Y + row * self.SESSION_STEP + 15)) <= 2

        for lane, (_, right) in enumerate(self.LANES):
            y = self.SESSION_Y
            status_bbox = self._color_bbox(
                image,
                colors[statuses[lane * 5]],
                (right - 170, y, right + 1, y + 30),
                tolerance,
            )
            assert status_bbox is not None
            assert right - 3 <= status_bbox[2] <= right

    @pytest.mark.parametrize("scale", (1, 2))
    def test_waiting_working_and_ready_marker_shapes_stay_semantic(self, scale):
        sessions = [self._session(0, "requires_action"),
                    self._session(1, "busy"), self._session(2, "idle")]
        image = self._render(sessions, scale=scale)
        tolerance = 0 if scale == 1 else 25
        waiting = self._marker_box(0)
        working = self._marker_box(1)
        ready = self._marker_box(2)

        assert self._has_color(image, _rgb(T.WARN), waiting, tolerance)
        working_pixel = image.getpixel((735, self.SESSION_Y + self.SESSION_STEP + 15))
        assert max(abs(a - b) for a, b in zip(working_pixel, _rgb(T.ACCENT))) \
            <= tolerance
        ready_center = (735, self.SESSION_Y + 2 * self.SESSION_STEP + 15)
        assert TestSplitStaticLeftLayout._distance(
            image.getpixel(ready_center), _rgb(T.SURFACE)
        ) < TestSplitStaticLeftLayout._distance(
            image.getpixel(ready_center), _rgb(T.INK_FAINT)
        )
        assert self._has_color(image, _rgb(T.INK_FAINT), ready, tolerance)

    @pytest.mark.parametrize("scale", (1, 2))
    def test_long_session_name_is_truncated_before_status_inside_lane(self, scale):
        from PIL import ImageChops

        common = "an-exceedingly-long-session-name-that-will-not-fit-" * 3
        first_session = self._session(0, "busy")
        second_session = self._session(0, "busy")
        first_session.name = common + "aaa"
        second_session.name = common + "bbb"
        first = self._render([first_session], scale=scale)
        second = self._render([second_session], scale=scale)
        tolerance = 0 if scale == 1 else 25
        name_and_status = (750, self.SESSION_Y, self.LANES[0][1] + 1,
                           self.SESSION_Y + 31)

        assert ImageChops.difference(
            first.crop(name_and_status), second.crop(name_and_status)
        ).getbbox() is None, "suffix beyond the ellipsis must not reach the lane"
        name_bbox = self._color_bbox(
            first, _rgb(T.INK), (760, self.SESSION_Y, 1050, self.SESSION_Y + 31),
            tolerance,
        )
        status_bbox = self._color_bbox(
            first, _rgb(T.ACCENT),
            (900, self.SESSION_Y, self.LANES[0][1] + 1, self.SESSION_Y + 31),
            tolerance,
        )
        assert name_bbox is not None and status_bbox is not None
        assert self.LANES[0][0] < name_bbox[0] <= name_bbox[2] < status_bbox[0]
        assert status_bbox[2] <= self.LANES[0][1]
        assert not self._has_color(
            first, _rgb(T.INK),
            (self.LANES[0][1] + 1, self.SESSION_Y,
             self.LANES[1][0], self.SESSION_Y + 31),
            tolerance,
        )

    @pytest.mark.parametrize("scale", (1, 2))
    def test_more_than_ten_uses_nine_real_sessions_and_exact_slot_ten_summary(self, scale):
        from PIL import Image, ImageChops, ImageDraw

        sessions = [self._session(i, "busy") for i in range(9)]
        sessions += [self._session(9, "requires_action"),
                     self._session(10, "idle"), self._session(11, "idle")]
        image = self._render(sessions, scale=scale)
        tolerance = 0 if scale == 1 else 25

        for slot in range(9):
            assert self._has_color(
                image, _rgb(T.ACCENT), self._marker_box(slot), tolerance
            )
        assert not self._has_color(
            image, _rgb(T.WARN), (674, 138, 1559, 433), tolerance
        )
        assert not self._has_color(
            image, _rgb(T.ACCENT), self._marker_box(9), tolerance
        )

        renderer = DashboardRenderer(scale=scale)
        expected = Image.new(
            "RGBA", (T.WIDTH * scale, T.HEIGHT * scale), renderer.palette.surface
        )
        ImageDraw.Draw(expected).text(
            ((self.LANES[1][0] + 86) * scale,
             (self.SESSION_Y + 4 * self.SESSION_STEP + 15) * scale),
            "+3 more", font=renderer.f_session_sub,
            fill=renderer.palette.ink_faint, anchor="lm",
        )
        expected = renderer._finish(expected)
        slot = (self.LANES[1][0], self.SESSION_Y + 4 * self.SESSION_STEP,
                self.LANES[1][1] + 1, 433)
        assert ImageChops.difference(image.crop(slot), expected.crop(slot)).getbbox() is None

    @pytest.mark.parametrize("scale", (1, 2))
    def test_limit_windows_stack_at_stable_utility_width_in_priority_order(self, scale):
        image = self._render(
            limits=self._limits([11, 22, 33, 44, 55]),
            footer=self._footer("aaa"), scale=scale,
        )
        renderer = DashboardRenderer(scale=scale)
        accent = _rgb(T.ACCENT)
        tolerance = 0 if scale == 1 else 25
        x0, x1 = self.UTILITY

        for slot, percent in enumerate((11, 22, 33, 44, 55)):
            y = self.LIMIT_Y + slot * self.LIMIT_STEP
            bar_box = (x0 - 2, y - 2, x1 + 3, y + 33)
            fill_bbox = self._color_bbox(image, accent, bar_box, tolerance)
            track_bbox = self._color_bbox(
                image, renderer._bar_remain, bar_box, 0 if scale == 1 else 15
            )
            assert fill_bbox is not None and track_bbox is not None
            assert abs(fill_bbox[0] - x0) <= 1
            assert abs(fill_bbox[2] - round(x0 + (x1 - x0) * percent / 100)) <= 2
            assert abs(track_bbox[2] - x1) <= 1
            assert y <= fill_bbox[1] <= fill_bbox[3] <= y + 30
            assert y <= track_bbox[1] <= track_bbox[3] <= y + 30

    @pytest.mark.parametrize("scale", (1, 2))
    def test_limit_overflow_keeps_first_four_and_uses_fifth_slot_for_exact_summary(self, scale):
        from PIL import Image, ImageChops, ImageDraw

        image = self._render(
            limits=self._limits([11, 22, 33, 44, 55, 66]), scale=scale
        )
        x0, x1 = self.UTILITY
        fifth_y = self.LIMIT_Y + 4 * self.LIMIT_STEP
        fifth = (x0, fifth_y, x1 + 1, fifth_y + 31)
        tolerance = 0 if scale == 1 else 25
        renderer = DashboardRenderer(scale=scale)
        assert not self._has_color(image, _rgb(T.ACCENT), fifth, tolerance)
        assert not self._has_color(image, renderer._bar_remain, fifth)

        expected = Image.new(
            "RGBA", (T.WIDTH * scale, T.HEIGHT * scale), renderer.palette.surface
        )
        ImageDraw.Draw(expected).text(
            ((x0 + 16) * scale, (fifth_y + 15) * scale), "+2 windows",
            font=renderer.f_small, fill=renderer.palette.ink_faint, anchor="lm",
        )
        expected = renderer._finish(expected)
        assert ImageChops.difference(image.crop(fifth), expected.crop(fifth)).getbbox() is None

    def test_long_limit_label_drops_reset_before_the_rendered_percentage(self):
        from datetime import datetime, timedelta, timezone
        from PIL import ImageChops
        from tzmrit_display.claude_limits import Limit, Limits

        now = datetime.now(timezone.utc)
        label = "An Extremely Long Scoped Model Window Name"

        def rendered(percent, reset):
            return self._render(limits=Limits(scoped=[
                Limit(label, percent, "normal", now + reset)
            ]))

        short_reset = rendered(41, timedelta(hours=2))
        long_reset = rendered(41, timedelta(days=2))
        bar = (self.UTILITY[0], self.LIMIT_Y, self.UTILITY[1] + 1, self.LIMIT_Y + 31)
        assert ImageChops.difference(short_reset.crop(bar), long_reset.crop(bar)).getbbox() is None

        next_percent = rendered(42, timedelta(hours=2))
        percent_text = (1760, self.LIMIT_Y, self.UTILITY[1] + 1, self.LIMIT_Y + 31)
        assert ImageChops.difference(
            short_reset.crop(percent_text), next_percent.crop(percent_text)
        ).getbbox() is not None

    @pytest.mark.parametrize("scale", (1, 2))
    @pytest.mark.parametrize("limit_count", range(6))
    def test_zero_through_five_limits_and_footer_facts_remain_disjoint(
            self, scale, limit_count):
        from PIL import ImageChops

        percents = [11, 22, 33, 44, 55][:limit_count]
        limits = self._limits(percents)
        first = self._render(
            limits=limits, footer=self._footer("aaa"), scale=scale
        )
        second = self._render(
            limits=limits, footer=self._footer("bbb"), scale=scale
        )
        tolerance = 0 if scale == 1 else 25
        utility_footer = (self.UTILITY[0], 326, T.WIDTH, 433)

        assert ImageChops.difference(
            first.crop(utility_footer), second.crop(utility_footer)
        ).getbbox() is None, "different clipped suffixes must render identically"

        limit_bboxes = []
        for slot in range(limit_count):
            y = self.LIMIT_Y + slot * self.LIMIT_STEP
            bbox = self._color_bbox(
                first, _rgb(T.ACCENT),
                (self.UTILITY[0] - 2, y - 2, self.UTILITY[1] + 3, y + 33),
                tolerance,
            )
            assert bbox is not None
            limit_bboxes.append(bbox)
        if limit_count < 5:
            next_y = self.LIMIT_Y + limit_count * self.LIMIT_STEP
            assert not self._has_color(
                first, _rgb(T.ACCENT),
                (self.UTILITY[0], next_y, self.UTILITY[1] + 1, next_y + 31),
                tolerance,
            )

        fact_bboxes = []
        rows = ((330, 361), (362, 393), (394, 433))
        key_widths = []
        for top, bottom in rows:
            key_bbox = self._color_bbox(
                first, _rgb(T.INK_FAINT), (self.UTILITY[0], top, 1670, bottom),
                tolerance,
            )
            value_bbox = self._color_bbox(
                first, _rgb(T.INK_DIM), (1670, top, T.WIDTH, bottom),
                tolerance,
            )
            assert key_bbox is not None and value_bbox is not None
            assert key_bbox[3] < bottom and value_bbox[3] < bottom
            fact_bboxes.append((min(key_bbox[1], value_bbox[1]),
                                max(key_bbox[3], value_bbox[3])))
            key_widths.append(key_bbox[2] - key_bbox[0] + 1)

        assert key_widths[2] > max(key_widths[:2])
        assert all(above[1] < below[0]
                   for above, below in zip(fact_bboxes, fact_bboxes[1:]))
        if limit_bboxes:
            assert max(bbox[3] for bbox in limit_bboxes) < fact_bboxes[0][0]
        overflow_bbox = self._color_bbox(
            first, _rgb(T.INK_DIM), (1887, 326, T.WIDTH, 433), tolerance
        )
        if scale == 1:
            assert overflow_bbox is None
        else:
            assert overflow_bbox is None or overflow_bbox[0] == 1887


class TestWorkingSessionPulse:
    """Only the filled working marker changes between explicit pulse phases."""

    MARKER_CENTERS = ((735, 159), (735, 217), (735, 275))
    MARKER_BOXES = (
        (725, 148, 749, 171),
        (725, 206, 749, 229),
        (725, 264, 749, 287),
    )

    @staticmethod
    def _metrics():
        metrics = {}
        for key in ("cpu", "ram", "temp", "load"):
            metric = Metric(key, key.upper(), scale_max=100)
            metric.push(30)
            metric.push(40)
            metric.text, metric.sub = "42", "%"
            metrics[key] = metric
        return metrics

    @staticmethod
    def _session(index, status):
        return Session(
            pid=3000 + index,
            name=f"session-{index}",
            cwd=f"/home/user/project-{index}",
            status=status,
            kind="interactive",
        )

    def _render(self, theme, sessions, phase):
        return DashboardRenderer(scale=1, theme=theme).render_split(
            self._metrics(), sessions, f"{len(sessions)} sessions",
            [("HOST", "host"), ("DISK", "disk"), ("UPTIME", "1h")],
            pulse_phase=phase,
        )

    @pytest.mark.parametrize("theme", ("blue", "red"))
    def test_only_working_marker_region_changes_between_phases(self, theme):
        from PIL import ImageChops

        sessions = [self._session(0, "requires_action"),
                    self._session(1, "busy"), self._session(2, "idle")]
        phase0 = self._render(theme, sessions, 0)
        phase1 = self._render(theme, sessions, 1)

        body = (0, 94, T.WIDTH, T.HEIGHT)
        difference = ImageChops.difference(
            phase0.crop(body), phase1.crop(body)
        ).getbbox()
        assert difference is not None
        difference = (
            difference[0] + body[0], difference[1] + body[1],
            difference[2] + body[0], difference[3] + body[1],
        )
        working_box = self.MARKER_BOXES[1]
        assert (working_box[0] <= difference[0] < difference[2] <= working_box[2]
                and working_box[1] <= difference[1] < difference[3] <= working_box[3])

        for marker_box in (self.MARKER_BOXES[0], self.MARKER_BOXES[2]):
            assert ImageChops.difference(
                phase0.crop(marker_box), phase1.crop(marker_box)
            ).getbbox() is None

    @pytest.mark.parametrize(("theme", "accent"), [
        ("blue", T.ACCENT),
        ("red", T.RED_ACCENT),
    ])
    def test_working_marker_keeps_accent_core_and_adds_subtle_theme_halo(
            self, theme, accent):
        session = self._session(0, "busy")
        phase0 = self._render(theme, [session], 0)
        phase1 = self._render(theme, [session], 1)
        center_x, center_y = self.MARKER_CENTERS[0]
        accent_rgb = _rgb(accent)
        surface = _rgb(T.SURFACE)

        for image in (phase0, phase1):
            assert image.getpixel((center_x, center_y)) == accent_rgb
            assert image.getpixel((center_x + 5, center_y)) == accent_rgb
        assert phase0.getpixel((center_x + 8, center_y)) == surface

        halo = phase1.getpixel((center_x + 8, center_y))
        assert halo not in (surface, accent_rgb, _rgb(T.WARN), _rgb(T.CRIT))
        assert all(min(a, b) <= value <= max(a, b)
                   for value, a, b in zip(halo, surface, accent_rgb))
        assert TestSplitStaticLeftLayout._distance(halo, surface) \
            < TestSplitStaticLeftLayout._distance(accent_rgb, surface)

    def test_transition_to_ready_immediately_removes_phase_one_pulse(self):
        from PIL import ImageChops

        session = self._session(0, "busy")
        working = self._render("blue", [session], 1)
        session.status = "idle"
        ready_phase1 = self._render("blue", [session], 1)
        ready_phase0 = self._render("blue", [session], 0)
        marker_box = self.MARKER_BOXES[0]

        assert ImageChops.difference(
            ready_phase0.crop(marker_box), ready_phase1.crop(marker_box)
        ).getbbox() is None
        assert ImageChops.difference(
            working.crop(marker_box), ready_phase1.crop(marker_box)
        ).getbbox() is not None
        center = self.MARKER_CENTERS[0]
        assert ready_phase1.getpixel(center) == _rgb(T.SURFACE)


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

    def test_eight_sessions_fit_without_overflow(self):
        """Eight open agents occupy eight real slots without a summary row."""
        sessions = self._sessions(8, waiting=1)
        img = DashboardRenderer(scale=1).render_split(
            self._four(), sessions, "8 sessions", [("HOST", "x")])
        status_colors = {_rgb(T.ACCENT), _rgb(T.WARN), _rgb(T.INK_FAINT)}

        def marker_colors(slot):
            lane, row = divmod(slot, 5)
            x = (674, 1128)[lane] + 52
            y = 144 + row * 58
            crop = img.crop((x, y + 4, x + 22, y + 27))
            colors = crop.getcolors(maxcolors=crop.width * crop.height)
            assert colors is not None
            return {color for _, color in colors}

        assert all(marker_colors(slot) & status_colors for slot in range(8))
        assert not marker_colors(8) & status_colors


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
