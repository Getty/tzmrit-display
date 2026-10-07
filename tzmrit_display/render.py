"""Layout engine for the 1920x462 strip.

The aspect ratio is 4:1 - a wide, shallow band. The layouts are designed for
that shape rather than being stretched square dashboards.

Two views share the same building blocks:

  render()        six metric columns across the full width
  render_split()  four metrics on the left, running Claude sessions on the right
  render_board()  drives and three metrics on the left, sessions in the middle,
                  the limits of every Claude account on the right

Drawing happens with supersampling because PIL has no antialiased lines;
without it every sparkline comes out ragged.
"""

from __future__ import annotations

import datetime

from PIL import Image, ImageDraw

from . import theme as T

# Spelled out rather than via strftime("%A"/"%B"): those follow the system
# locale, so the panel would switch language depending on where it runs.
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

# Fixed viewer-space geometry for the static split view. Coordinates are kept
# here rather than in the full-view theme metrics so render() cannot drift.
_SPLIT_X = 640
_SPLIT_DIVIDER_Y = (94, 432)
_SPLIT_METRIC_CARDS = (
    (34, 94, 315, 256),
    (331, 94, 612, 256),
    (34, 270, 315, 432),
    (331, 270, 612, 432),
)
_SPLIT_SESSION_LANES = ((674, 1104), (1128, 1558))
_SPLIT_SESSION_Y = 144
_SPLIT_SESSION_STEP = 58
_SPLIT_UTILITY = (1582, 1886)
_SPLIT_LIMIT_Y = 138
_SPLIT_LIMIT_STEP = 36
_SPLIT_FOOTER_Y = (334, 366, 398)
_SPLIT_FOOTER_VALUE_X = 1670

# The board view keeps the split view's session lanes and utility column and
# rearranges only the left third: one tall drives card beside three short
# metric cards.
_BOARD_DRIVES = (34, 94, 315, 432)
_BOARD_CARDS = (
    (331, 94, 612, 198),
    (331, 211, 612, 315),
    (331, 328, 612, 432),
)
_BOARD_BAR_Y = 138
_BOARD_BAR_STEP = 36
_BOARD_DRIVE_SLOTS = 8
# Bars per account block, by how many accounts share the utility column.
_BOARD_ACCOUNT_ROWS = {1: 7, 2: 3, 3: 2}
# Past ten sessions the board switches its lanes to one-line rows: half the
# step, so twice the sessions fit before anything is cut.
_BOARD_DENSE_STEP = 29
_BOARD_DENSE_ROWS = 10


def _color(status: str, palette: T.Palette) -> str:
    return {"warn": palette.warn, "crit": palette.crit}.get(status, palette.accent)


def _mix(hex_a: str, hex_b: str, t: float) -> tuple[int, int, int]:
    """Blend two colors (#RRGGBB or an RGB tuple), t=0 -> a, t=1 -> b."""
    a, b = (c if isinstance(c, tuple) else
            tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (hex_a, hex_b))
    return tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))


def _inactive_color(seconds: float, palette: T.Palette):
    """Return a muted status blend for an inactivity counter.

    The numeric age remains the primary encoding, so this never uses a pure
    reserved status color.
    """
    if seconds >= 3600:
        return _mix(palette.ink_dim, palette.crit, 0.55)
    if seconds >= 300:
        return _mix(palette.ink_dim, palette.warn, 0.55)
    return palette.ink_faint


def _spark_points(metric, x0, y0, w, h, top=None):
    """Map a history into pixel coordinates.

    Without a fixed scale the values are scaled from the history, but against
    a floor - otherwise the curve zooms into quiet readings and presents noise
    as a dramatic trend. `top` overrides the scale, for two curves that must
    share one.
    """
    data = list(metric.history)
    if len(data) < 2:
        return []
    if top is not None:
        pass
    elif metric.scale_max is not None:
        top = metric.scale_max
    else:
        floor = 1e5 if metric.key.startswith("net") else 1.0
        top = max(max(data), floor)
    top = max(top, 1e-6)
    step = w / max(1, len(data) - 1)
    return [(x0 + i * step, y0 + h - min(1.0, v / top) * h) for i, v in enumerate(data)]


class DashboardRenderer:
    """Draws the dashboard strip with one immutable theme palette. `scale`
    controls supersampling."""

    def __init__(self, scale: int = 2, theme: str = "blue"):
        self.scale = max(1, scale)
        self.palette = T.palette(theme)
        self._bar_remain = _mix(self.palette.surface, self.palette.accent, 0.35)
        # Second and third bar families of the board view (network drives, a
        # second and third Claude account): same construction as the accent
        # bar - a fill and a track at t=0.35 towards it.
        self._bar_families = (
            (self.palette.accent, self._bar_remain),
            (self.palette.accent_warm,
             _mix(self.palette.surface, self.palette.accent_warm, 0.35)),
            (self.palette.ink_dim,
             _mix(self.palette.surface, self.palette.ink_dim, 0.35)),
        )
        self._pulse_halo = _mix(self.palette.surface, self.palette.accent, 0.30)
        s = self.scale
        self.f_label = T.font(T.FONT_LABEL, 23 * s)
        self.f_value = T.font(T.FONT_VALUE, 74 * s)
        self.f_value_sm = T.font(T.FONT_VALUE, 56 * s)
        self.f_value_xs = T.font(T.FONT_VALUE, 36 * s)
        self.f_value_net = T.font(T.FONT_VALUE, 24 * s)
        self.f_unit = T.font(T.FONT_UNIT, 25 * s)
        self.f_clock = T.font(T.FONT_VALUE, 40 * s)
        self.f_small = T.font(T.FONT_TEXT, 21 * s)
        self.f_foot_key = T.font(T.FONT_LABEL, 19 * s)
        self.f_session = T.font(T.FONT_LABEL, 29 * s)
        self.f_session_sub = T.font(T.FONT_TEXT, 22 * s)
        self.f_session_dense = T.font(T.FONT_LABEL, 22 * s)
        self.f_session_dense_sub = T.font(T.FONT_TEXT, 19 * s)

    # -- small drawing primitives ----------------------------------------

    def _arrow(self, d, x, y, size, color, direction):
        """Direction arrow as a polygon - Roboto only yields a .notdef box for
        U+2191/U+2193, so it gets drawn instead."""
        w, h = size * 0.62, size
        shaft = w * 0.26
        cx = x + w / 2
        if direction == "up":
            d.polygon([(cx, y), (x + w, y + h * 0.42), (x, y + h * 0.42)], fill=color)
            d.rectangle([cx - shaft / 2, y + h * 0.36, cx + shaft / 2, y + h], fill=color)
        else:
            d.polygon([(cx, y + h), (x + w, y + h * 0.58), (x, y + h * 0.58)], fill=color)
            d.rectangle([cx - shaft / 2, y, cx + shaft / 2, y + h * 0.64], fill=color)

    def _warning_mark(self, d, x, y, size, color, background=None):
        """Warning triangle as a second encoding beside the status color.

        Status colors never appear alone - someone who cannot tell red from
        yellow still sees that something here is flagged.
        """
        h = size * 0.88
        d.polygon([(x + size / 2, y), (x + size, y + h), (x, y + h)], fill=color)
        d.rectangle([x + size / 2 - size * 0.05, y + h * 0.3,
                     x + size / 2 + size * 0.05, y + h * 0.66],
                    fill=background or self.palette.surface)

    def _sparkline(self, base, d, metric, x, y, w, h, color, top=None):
        pts = _spark_points(metric, x, y, w, h, top)
        if len(pts) < 2:
            return
        s = self.scale
        fill_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(fill_layer)
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        fd.polygon(pts + [(pts[-1][0], y + h), (pts[0][0], y + h)],
                   fill=rgb + (self.palette.spark_fill_alpha,))
        base.alpha_composite(fill_layer)
        d.line(pts, fill=color, width=max(1, 2 * s), joint="curve")
        r = 3.5 * s
        d.ellipse([pts[-1][0] - r, pts[-1][1] - r, pts[-1][0] + r, pts[-1][1] + r], fill=color)

    # -- sections --------------------------------------------------------

    def _header(self, d, W, right_text=None):
        s = self.scale
        now = datetime.datetime.now()
        date = f"{WEEKDAYS[now.weekday()]}, {MONTHS[now.month - 1]} {now.day}"
        d.text((T.MARGIN_X * s, T.HEADER_Y * s), date.upper(), font=self.f_label,
               fill=self.palette.ink_dim)
        d.text((W - T.MARGIN_X * s, T.HEADER_Y * s - 6 * s),
               right_text or now.strftime("%H:%M:%S"),
               font=self.f_clock, fill=self.palette.ink, anchor="ra")
        d.line([(T.MARGIN_X * s, T.RULE_Y * s), (W - T.MARGIN_X * s, T.RULE_Y * s)],
               fill=self.palette.ink_faint, width=max(1, s))

    def _metric_column(self, img, d, m, x, col_w, compact=False, spark_top=None, spark_h=None):
        s = self.scale
        color = _color(m.status, self.palette)
        alert = m.status != "ok"
        f_value = self.f_value_sm if compact else self.f_value
        spark_top = (spark_top if spark_top is not None else T.SPARK_TOP) * s
        spark_h = (spark_h if spark_h is not None else T.SPARK_H) * s

        label_x = x
        if alert:
            self._warning_mark(d, x, (T.TILE_TOP + 2) * s, 19 * s, color)
            label_x = x + 27 * s
        d.text((label_x, T.TILE_TOP * s), m.label, font=self.f_label,
               fill=color if alert else self.palette.ink_dim)
        if m.arrow:
            lw = d.textlength(m.label, font=self.f_label)
            self._arrow(d, label_x + lw + 9 * s, (T.TILE_TOP + 3) * s, 17 * s,
                        color if alert else self.palette.ink_dim, m.arrow)

        d.text((x, (T.TILE_TOP + 34) * s), m.text, font=f_value,
               fill=color if alert else self.palette.ink)
        if m.sub:
            sub_y = T.TILE_TOP + (106 if compact else 126)
            d.text((x, sub_y * s), m.sub, font=self.f_unit, fill=self.palette.ink_dim)

        self._sparkline(img, d, m, x, spark_top, col_w * 0.9, spark_h, color)
        d.line([(x, spark_top + spark_h), (x + col_w * 0.9, spark_top + spark_h)],
               fill=self.palette.ink_faint, width=max(1, s))

    def _metric_card(self, img, d, metric, box):
        """One split-view metric card with its value over the graph."""
        s = self.scale
        x0, y0, x1, y1 = (value * s for value in box)
        pad = 10 * s
        graph_x = x0 + pad
        graph_y = y0 + 40 * s
        graph_w = x1 - x0 - 20 * s
        graph_h = y1 - y0 - 50 * s
        color = _color(metric.status, self.palette)
        alert = metric.status != "ok"

        d.rectangle([x0, y0, x1, y1], fill=self.palette.surface_tile)
        d.line([(graph_x, graph_y + graph_h),
                (graph_x + graph_w, graph_y + graph_h)],
               fill=self.palette.ink_faint, width=max(1, s))
        self._sparkline(img, d, metric, graph_x, graph_y,
                        graph_w, graph_h, color)

        label_x = graph_x
        if alert:
            self._warning_mark(d, graph_x, y0 + 10 * s, 19 * s, color,
                               self.palette.surface_tile)
            label_x += 27 * s
        d.text((label_x, y0 + 10 * s), metric.label, font=self.f_label,
               fill=color if alert else self.palette.ink_dim)
        if metric.arrow:
            label_w = d.textlength(metric.label, font=self.f_label)
            self._arrow(d, label_x + label_w + 9 * s, y0 + 13 * s,
                        17 * s, color if alert else self.palette.ink_dim,
                        metric.arrow)

        text_color = color if alert else self.palette.ink
        stroke = max(1, 2 * s)
        d.text((graph_x, y0 + 43 * s), metric.text, font=self.f_value_sm,
               fill=text_color, stroke_width=stroke,
               stroke_fill=self.palette.surface_tile)
        if metric.sub:
            d.text((graph_x, y0 + 108 * s), metric.sub, font=self.f_unit,
                   fill=self.palette.ink_dim, stroke_width=stroke,
                   stroke_fill=self.palette.surface_tile)

    def _footer_row(self, d, entries, x0, w, y):
        s = self.scale
        col = w / max(1, len(entries))
        for i, (key, val) in enumerate(entries):
            fx = x0 + i * col
            d.text((fx, y), key, font=self.f_foot_key, fill=self.palette.ink_faint)
            d.text((fx + d.textlength(key, font=self.f_foot_key) + 14 * s, y - 2 * s),
                   val, font=self.f_small, fill=self.palette.ink_dim)

    def _name_segments(self, d, name, project, avail, font=None):
        """Split a session name into draw segments that fit within `avail` px.

        Returns a list of (text, is_prefix) pairs. For a derived name (one that
        starts with its project) the project prefix is a separate segment so it
        can be tinted, and the ELLIPSIS goes inside the prefix so the unique
        suffix survives: `p5-…-plugin-docker-api-7d` truncates to
        `p5-dist-zilla-plugin…-7d`, keeping the `-7d`. Only when the suffix
        will not fit even beside a minimal (2-char) prefix does it fall back to
        right-truncating the whole name as one base-colored segment - which is
        also how a non-derived name is always handled.
        """
        font = font or self.f_session

        def tl(t):
            return d.textlength(t, font=font)

        if project and name.startswith(project) and name != project:
            prefix, suffix = name[:len(project)], name[len(project):]
            if tl(prefix + suffix) <= avail:
                return [(prefix, True), (suffix, False)]
            if len(prefix) >= 2:
                p = prefix
                while len(p) > 2 and tl(p + "…" + suffix) > avail:
                    p = p[:-1]
                if tl(p + "…" + suffix) <= avail:
                    return [(p + "…", True), (suffix, False)]
            # Suffix won't fit even beside a minimal prefix: fall through.

        # Non-derived, or the fallback: right-truncate the whole name.
        t = name
        while t and tl(t) > avail:
            t = t[:-1]
        if t != name:
            t = t[:-1] + "…" if len(t) > 1 else t
        return [(t, False)]

    def _fit(self, d, text, font, avail):
        """Right-truncate `text` with an ellipsis so it fits `avail` px."""
        if not text or d.textlength(text, font=font) <= avail:
            return text
        while text and d.textlength(text + "…", font=font) > avail:
            text = text[:-1]
        return text + "…" if text else ""

    def _session_row(self, d, sess, x, w, y, pulse_phase=0):
        """One session: inactivity counter, marker, name, memory + model, status.

        The name's project prefix (present in derived names) is drawn in the
        warm amber tint so the redundant project reads as secondary and the
        unique suffix pops; the sub-line carries memory and the model in use,
        the project dropped. A working marker always keeps its filled core;
        phase 1 adds only a muted accent-derived halo around it.
        """
        s = self.scale
        if sess.waiting:
            color = self.palette.warn
        elif sess.working:
            color = self.palette.accent
        else:
            color = self.palette.ink_faint

        cy = y + 15 * s
        # Left gutter: time since last LLM activity, right-aligned in a fixed
        # width so the markers and names still line up whatever the value.
        gut = 52 * s
        d.text((x + gut - 10 * s, cy), sess.inactive_text, font=self.f_session_sub,
               fill=_inactive_color(sess.inactive_seconds, self.palette), anchor="rm")

        mk = x + gut
        if sess.waiting:
            # A triangle rather than a dot: the one state that concerns you is
            # legible without relying on color perception.
            self._warning_mark(d, mk, y + 5 * s, 21 * s, color)
        elif sess.working:
            cx = mk + 9 * s
            core_r = 6 * s
            if pulse_phase:
                halo_r = 9 * s
                d.ellipse([cx - halo_r, cy - halo_r,
                           cx + halo_r, cy + halo_r], fill=self._pulse_halo)
            d.ellipse([cx - core_r, cy - core_r,
                       cx + core_r, cy + core_r], fill=color)
        else:
            r = 8 * s
            box = [mk + 1 * s, cy - r, mk + 1 * s + 2 * r, cy + r]
            d.ellipse(box, outline=color, width=max(1, 2 * s))

        name_x = mk + 34 * s
        right = x + w
        status_w = d.textlength(sess.status_text, font=self.f_session_sub)
        # Reserve room for the status word before the name can run into it
        avail = right - name_x - status_w - 20 * s

        base = self.palette.ink if (sess.waiting or sess.working) else self.palette.ink_dim
        seg_x = name_x
        for text, is_prefix in self._name_segments(d, sess.name, sess.project, avail):
            d.text((seg_x, y), text, font=self.f_session,
                   fill=self.palette.accent_warm if is_prefix else base)
            seg_x += d.textlength(text, font=self.f_session)

        # Memory including MCP child processes - they are the bulk of it - and
        # the model that ran the last turn, joined the way summarize() joins its
        # facts. The project is not repeated here; it shows as the tinted
        # prefix. An unknown model simply drops out, leaving the memory where it
        # already was, so the line never shifts. Clipped against the same
        # `avail` as the name so it cannot run into the status word either.
        # On a multi-host board the host leads the line - it is the one fact
        # that tells two equally named sessions apart.
        sub = " · ".join(t for t in (sess.host, sess.memory_text, sess.model_text) if t)
        if sub:
            d.text((name_x, y + 30 * s), self._fit(d, sub, self.f_session_sub, avail),
                   font=self.f_session_sub, fill=self.palette.ink_dim)
        d.text((right, y), sess.status_text, font=self.f_session_sub, fill=color, anchor="ra")

    def _session_row_dense(self, d, sess, x, w, y, pulse_phase=0):
        """One session on a single line: age, marker, name, host, status.

        The same encodings as _session_row at half the height. What gives way
        is the sub-line's memory and model; the host stays, because on a board
        this full it is what tells the rows apart.
        """
        s = self.scale
        if sess.waiting:
            color = self.palette.warn
        elif sess.working:
            color = self.palette.accent
        else:
            color = self.palette.ink_faint
        sub = self.f_session_dense_sub
        cy = y + 13 * s
        gut = 52 * s
        d.text((x + gut - 10 * s, cy), sess.inactive_text, font=sub,
               fill=_inactive_color(sess.inactive_seconds, self.palette), anchor="rm")

        mk = x + gut
        if sess.waiting:
            self._warning_mark(d, mk, y + 5 * s, 17 * s, color)
        elif sess.working:
            cx = mk + 8 * s
            if pulse_phase:
                d.ellipse([cx - 8 * s, cy - 8 * s, cx + 8 * s, cy + 8 * s],
                          fill=self._pulse_halo)
            d.ellipse([cx - 5 * s, cy - 5 * s, cx + 5 * s, cy + 5 * s], fill=color)
        else:
            r = 6 * s
            d.ellipse([mk + 2 * s, cy - r, mk + 2 * s + 2 * r, cy + r],
                      outline=color, width=max(1, 2 * s))

        name_x = mk + 28 * s
        right = x + w
        d.text((right, cy), sess.status_text, font=sub, fill=color, anchor="rm")
        edge = right - d.textlength(sess.status_text, font=sub) - 12 * s
        if sess.host:
            host = self._fit(d, sess.host, sub, 130 * s)
            d.text((edge, cy), host, font=sub, fill=self.palette.ink_faint, anchor="rm")
            edge -= d.textlength(host, font=sub) + 12 * s

        base = self.palette.ink if (sess.waiting or sess.working) else self.palette.ink_dim
        seg_x = name_x
        for text, is_prefix in self._name_segments(
                d, sess.name, sess.project, edge - name_x, self.f_session_dense):
            d.text((seg_x, cy), text, font=self.f_session_dense, anchor="lm",
                   fill=self.palette.accent_warm if is_prefix else base)
            seg_x += d.textlength(text, font=self.f_session_dense)

    def _split_sessions(self, d, sessions, summary, pulse_phase=0, dense=False):
        """Render at most ten deterministic slots across two five-row lanes.

        With `dense` (the board view) more than ten sessions switch both lanes
        to one-line rows, twenty slots instead of ten.
        """
        s = self.scale
        x0 = _SPLIT_SESSION_LANES[0][0] * s
        d.text((x0, T.TILE_TOP * s), "CLAUDE", font=self.f_label,
               fill=self.palette.ink_dim)
        d.text((x0 + d.textlength("CLAUDE", font=self.f_label) + 18 * s,
                T.TILE_TOP * s), summary, font=self.f_small,
               fill=self.palette.ink_faint)

        if not sessions:
            d.text((x0, (_SPLIT_SESSION_Y + 15) * s), "no sessions running",
                   font=self.f_session_sub, fill=self.palette.ink_faint)
            return

        if dense and len(sessions) > 10:
            slots = 2 * _BOARD_DENSE_ROWS
            shown = sessions[:slots - 1] if len(sessions) > slots else sessions
            for index, session in enumerate(shown):
                lane, row = divmod(index, _BOARD_DENSE_ROWS)
                left, right = _SPLIT_SESSION_LANES[lane]
                self._session_row_dense(
                    d, session, left * s, (right - left) * s,
                    (_SPLIT_SESSION_Y + row * _BOARD_DENSE_STEP) * s,
                    pulse_phase=pulse_phase,
                )
            if len(sessions) > len(shown):
                left, _ = _SPLIT_SESSION_LANES[1]
                y = _SPLIT_SESSION_Y + (_BOARD_DENSE_ROWS - 1) * _BOARD_DENSE_STEP
                d.text(((left + 80) * s, (y + 13) * s),
                       f"+{len(sessions) - len(shown)} more",
                       font=self.f_session_dense_sub, fill=self.palette.ink_faint,
                       anchor="lm")
            return

        if len(sessions) > 10:
            shown = sessions[:9]
            overflow = len(sessions) - len(shown)
        else:
            shown = sessions[:10]
            overflow = 0

        for index, session in enumerate(shown):
            lane, row = divmod(index, 5)
            left, right = _SPLIT_SESSION_LANES[lane]
            self._session_row(
                d, session, left * s, (right - left) * s,
                (_SPLIT_SESSION_Y + row * _SPLIT_SESSION_STEP) * s,
                pulse_phase=pulse_phase,
            )

        if overflow:
            left, _ = _SPLIT_SESSION_LANES[1]
            y = _SPLIT_SESSION_Y + 4 * _SPLIT_SESSION_STEP
            d.text(((left + 86) * s, (y + 15) * s), f"+{overflow} more",
                   font=self.f_session_sub, fill=self.palette.ink_faint,
                   anchor="lm")

    def _finish(self, img):
        if self.scale > 1:
            img = img.resize((T.WIDTH, T.HEIGHT), Image.LANCZOS)
        return img.convert("RGB")

    # -- views -----------------------------------------------------------

    def render(self, metrics: dict, footer: list[tuple[str, str]]) -> Image.Image:
        """Six metric columns across the full width."""
        s = self.scale
        W, H = T.WIDTH * s, T.HEIGHT * s
        img = Image.new("RGBA", (W, H), self.palette.surface)
        d = ImageDraw.Draw(img)
        mx = T.MARGIN_X * s

        self._header(d, W)

        items = list(metrics.values())
        col_w = (W - 2 * mx) / max(1, len(items))
        for i, m in enumerate(items):
            x = mx + i * col_w
            if i:
                d.line([(x - col_w * 0.02, (T.TILE_TOP - 6) * s),
                        (x - col_w * 0.02, (T.SPARK_TOP + T.SPARK_H) * s)],
                       fill=self.palette.ink_faint, width=max(1, s))
            self._metric_column(img, d, m, x, col_w)

        fy = T.FOOTER_Y * s
        d.line([(mx, fy - 18 * s), (W - mx, fy - 18 * s)], fill=self.palette.ink_faint, width=max(1, s))
        self._footer_row(d, footer, mx, W - 2 * mx, fy)

        return self._finish(img)

    def _bar_text(self, base, pieces, bx, by, bw, h, fw, inks=None):
        """Draw a bar's labels so every pixel gets the ink its ground wants.

        A label routinely straddles the fill edge - "Session 13%" starts one
        pad in and the edge is at 13% of the bar - so this is the normal case,
        not a corner one, and picking one color per piece would leave half of
        every label washed out. Both inks are therefore rendered on a
        bar-sized layer and the fill edge decides pixel by pixel which layer
        lands: dark ink over the bright fill, light ink over the dark track.
        The seam is invisible because it IS the edge it splits on.
        """
        left, top = int(bx), int(by)
        size = (int(bx + bw) + 1 - left, int(by + h) + 1 - top)
        edge = max(0, min(size[0], int(round(fw))))
        on_fill, on_track = inks or (self.palette.surface, self.palette.ink)
        for ink, (a, b) in ((on_fill, (0, edge)), (on_track, (edge, size[0]))):
            if b <= a:
                continue
            layer = Image.new("RGBA", size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            for (px, py), text, anchor in pieces:
                ld.text((px - left, py - top), text, font=self.f_small,
                        fill=ink, anchor=anchor)
            base.alpha_composite(layer.crop((a, 0, b, size[1])), dest=(left + a, top))

    def _limit_bars(self, base, d, rows, x0, x1, y, *, fill=None, remain=None,
                    stale=False):
        """Draw rate-limit bars inside the caller's ``x0..x1`` range.

        Multiple rows share the supplied range side by side. The split utility
        instead calls this primitive once per vertical slot with a singleton,
        so every utility bar receives the full, stable column width. In either
        arrangement, the consumed fraction (percent/100 from the left) uses the
        selected theme's bright accent and the remaining budget uses a dark
        color on the same SURFACE->ACCENT axis as the sparkline fill.

        The split between those tints is what the bar encodes, so it carries the
        contrast budget. `remain` is always derived at t=0.35: #264365 for blue
        (4.01:1 against its fill) and #572629 for red (3.303:1, clearing the 3:1
        non-text step). The track is chrome marking the 100% reference; the
        filled/unfilled step is the datum. Blue's existing 1.92:1 track-to-panel
        contrast and all of its pixels remain unchanged.

        Because `remain` is dark, one ink can no longer serve both halves: the
        palette surface holds on the bright fill but would vanish on the track,
        so the track's share of each label is drawn in the palette ink instead -
        see _bar_text for the pixel-exact split. A bar is itself a marker shape,
        so the colored chip means something without breaking the theme.py ban on
        bare colored text.

        `stale` draws a reading that could not be renewed: both tints at half
        strength against the surface, so the bar reads as a ghost of itself
        while the split stays visible. The faded fill is too dark for the dark
        ink, so the label is light throughout: full ink on the fill, dimmed on
        the track.
        """
        s = self.scale
        n = max(1, len(rows))
        gap = 18 * s
        h = 30 * s
        rad = int(h // 2)
        pad = 16 * s
        bw = (x1 - x0 - (n - 1) * gap) / n
        cy = y + h / 2
        # The board view draws other bar families (see _bar_families) through
        # this same primitive; without the arguments it is the accent bar.
        fill = fill if fill is not None else self.palette.accent
        remain = remain if remain is not None else self._bar_remain
        inks = None
        if stale:
            fill = _mix(fill, self.palette.surface, 0.5)
            remain = _mix(remain, self.palette.surface, 0.5)
            inks = (self.palette.ink, self.palette.ink_dim)
        for i, lim in enumerate(rows):
            bx = x0 + i * (bw + gap)
            d.rounded_rectangle([bx, y, bx + bw, y + h], radius=rad, fill=remain)
            fw = bw * max(0, min(100, lim.percent)) / 100
            if fw > 0:
                d.rounded_rectangle([bx, y, bx + fw, y + h],
                                    radius=int(min(rad, fw / 2)), fill=fill)
            room = bw - 2 * pad
            # Truncate the name, never the number: the percentage is the datum
            # and a bar reading "An Extremely …" without it says nothing.
            value = f" {lim.percent}%"
            label = self._fit(d, lim.label, self.f_small,
                              room - d.textlength(value, font=self.f_small)) + value
            pieces = [((bx + pad, cy), label, "lm")]
            # The countdown is the first thing to go when the bars get narrow:
            # the percentage is the datum, the reset is the nice-to-have, and
            # two pieces colliding mid-bar ("Session 100%10h04m") is worse than
            # one piece missing. This applies both when bars share a range and
            # when one utility-width bar carries a long scoped label.
            reset = lim.reset_text()
            if reset and (d.textlength(label, font=self.f_small)
                          + d.textlength(reset, font=self.f_small)
                          + 8 * s) <= room:
                pieces.append(((bx + bw - pad, cy), reset, "rm"))
            self._bar_text(base, pieces, bx, y, bw, h, fw, inks)

    def _stale_note(self, d, limits, x1, y):
        """Right-aligned age of a stale reading, on a block's title line."""
        if limits is None or not limits.stale:
            return 0
        age = limits.age_text()
        note = f"{age} old" if age else "old"
        d.text((x1, y), note, font=self.f_small, fill=self.palette.ink_faint,
               anchor="ra")
        return d.textlength(note, font=self.f_small) + 10 * self.scale

    def _split_utility(self, base, d, limits, footer):
        """Vertical limit slots followed by three compact host facts."""
        s = self.scale
        x0, x1 = (value * s for value in _SPLIT_UTILITY)
        d.text((x0, T.TILE_TOP * s), "LIMITS", font=self.f_label,
               fill=self.palette.ink_dim)
        self._stale_note(d, limits, x1, (T.TILE_TOP + 4) * s)
        stale = bool(limits and limits.stale)

        rows = list(limits.rows) if limits else []
        overflow = len(rows) - 4 if len(rows) > 5 else 0
        shown = rows[:4] if overflow else rows[:5]
        for index, limit in enumerate(shown):
            y = (_SPLIT_LIMIT_Y + index * _SPLIT_LIMIT_STEP) * s
            self._limit_bars(base, d, [limit], x0, x1, y, stale=stale)
        if overflow:
            y = _SPLIT_LIMIT_Y + 4 * _SPLIT_LIMIT_STEP
            d.text((x0 + 16 * s, (y + 15) * s), f"+{overflow} windows",
                   font=self.f_small, fill=self.palette.ink_faint, anchor="lm")

        d.line([(x0, 325 * s), (x1, 325 * s)],
               fill=self.palette.ink_faint, width=max(1, s))
        value_x = _SPLIT_FOOTER_VALUE_X * s
        value_room = x1 - value_x
        for (key, value), y in zip((footer or [])[:3], _SPLIT_FOOTER_Y):
            d.text((x0, y * s), key, font=self.f_foot_key,
                   fill=self.palette.ink_faint)
            d.text((value_x, (y - 2) * s),
                   self._fit(d, value, self.f_small, value_room),
                   font=self.f_small, fill=self.palette.ink_dim)

    # -- board view ------------------------------------------------------

    def _header_facts(self, d, W, facts):
        """Host facts centered in the header, between the date and the clock.

        The board view needs the whole utility column for account limits, so
        the facts the split view keeps under its bars move up here.
        """
        s = self.scale
        gap, inner = 44 * s, 12 * s
        widths = [d.textlength(k, font=self.f_foot_key) + inner
                  + d.textlength(v, font=self.f_small) for k, v in facts]
        x = (W - sum(widths) - gap * max(0, len(facts) - 1)) / 2
        y = (T.HEADER_Y + 3) * s
        for (key, value), width in zip(facts, widths):
            d.text((x, y), key, font=self.f_foot_key, fill=self.palette.ink_faint)
            d.text((x + d.textlength(key, font=self.f_foot_key) + inner, y - 2 * s),
                   value, font=self.f_small, fill=self.palette.ink_dim)
            x += width + gap

    def _board_drives(self, base, d, drives):
        """Every drive as a usage bar; network drives in the warm family.

        Two encodings for "network", as everywhere on this panel: the color
        and the position - local drives always come first.
        """
        s = self.scale
        x0, y0, x1, y1 = (value * s for value in _BOARD_DRIVES)
        pad = 10 * s
        d.rectangle([x0, y0, x1, y1], fill=self.palette.surface_tile)
        d.text((x0 + pad, y0 + 10 * s), "DISKS", font=self.f_label,
               fill=self.palette.ink_dim)
        if not drives:
            d.text((x0 + pad, (_BOARD_BAR_Y + 15) * s), "no drives",
                   font=self.f_small, fill=self.palette.ink_faint, anchor="lm")
            return
        overflow = len(drives) - _BOARD_DRIVE_SLOTS
        shown = drives[:_BOARD_DRIVE_SLOTS - 1] if overflow > 0 else drives
        for index, drive in enumerate(shown):
            fill, remain = self._bar_families[1 if drive.remote else 0]
            self._limit_bars(base, d, [drive], x0 + pad, x1 - pad,
                             (_BOARD_BAR_Y + index * _BOARD_BAR_STEP) * s,
                             fill=fill, remain=remain)
        if overflow > 0:
            y = _BOARD_BAR_Y + (_BOARD_DRIVE_SLOTS - 1) * _BOARD_BAR_STEP
            d.text((x0 + pad + 16 * s, (y + 15) * s),
                   f"+{len(drives) - len(shown)} more", font=self.f_small,
                   fill=self.palette.ink_faint, anchor="lm")

    def _board_card(self, img, d, metric, box):
        """A short metric card: label and unit left, value right, graph below."""
        s = self.scale
        x0, y0, x1, y1 = (value * s for value in box)
        pad = 10 * s
        graph_y = y0 + 44 * s
        graph_h = y1 - graph_y - 8 * s
        color = _color(metric.status, self.palette)
        alert = metric.status != "ok"

        d.rectangle([x0, y0, x1, y1], fill=self.palette.surface_tile)
        d.line([(x0 + pad, graph_y + graph_h), (x1 - pad, graph_y + graph_h)],
               fill=self.palette.ink_faint, width=max(1, s))
        self._sparkline(img, d, metric, x0 + pad, graph_y,
                        x1 - x0 - 2 * pad, graph_h, color)

        label_x = x0 + pad
        if alert:
            self._warning_mark(d, label_x, y0 + 10 * s, 19 * s, color,
                               self.palette.surface_tile)
            label_x += 27 * s
        d.text((label_x, y0 + 10 * s), metric.label, font=self.f_label,
               fill=color if alert else self.palette.ink_dim)
        if metric.sub:
            d.text((label_x + d.textlength(metric.label, font=self.f_label) + 10 * s,
                    y0 + 13 * s), metric.sub, font=self.f_foot_key,
                   fill=self.palette.ink_faint)
        d.text((x1 - pad, y0 + 2 * s), metric.text, font=self.f_value_xs,
               fill=color if alert else self.palette.ink, anchor="ra",
               stroke_width=max(1, 2 * s), stroke_fill=self.palette.surface_tile)

    def _board_net(self, img, d, down, up, box):
        """Both network directions in one card, on one shared scale.

        Down is the accent curve, up the warm one; each value carries its
        drawn arrow in the curve's color, so the pairing never rests on color
        alone. A shared scale is the point of putting them together: two
        curves each zoomed to its own maximum would look equally busy.
        """
        s = self.scale
        x0, y0, x1, y1 = (value * s for value in box)
        pad = 10 * s
        graph_y = y0 + 44 * s
        graph_h = y1 - graph_y - 8 * s
        d.rectangle([x0, y0, x1, y1], fill=self.palette.surface_tile)
        d.line([(x0 + pad, graph_y + graph_h), (x1 - pad, graph_y + graph_h)],
               fill=self.palette.ink_faint, width=max(1, s))
        pairs = [(m, c) for m, c in ((down, self.palette.accent),
                                     (up, self.palette.accent_warm)) if m is not None]
        top = max([1e5] + [v for m, _ in pairs for v in m.history])
        for metric, color in pairs:
            self._sparkline(img, d, metric, x0 + pad, graph_y,
                            x1 - x0 - 2 * pad, graph_h, color, top=top)

        d.text((x0 + pad, y0 + 10 * s), "NET", font=self.f_label,
               fill=self.palette.ink_dim)
        x = x1 - pad
        arrow = 17 * s
        for metric, color in reversed(pairs):  # right to left: up, then down
            x -= d.textlength(metric.text, font=self.f_value_net)
            d.text((x, y0 + 9 * s), metric.text, font=self.f_value_net,
                   fill=self.palette.ink, stroke_width=max(1, 2 * s),
                   stroke_fill=self.palette.surface_tile)
            x -= arrow * 0.62 + 6 * s
            self._arrow(d, x, y0 + 13 * s, arrow, color, metric.arrow or "down")
            x -= 14 * s

    def _board_accounts(self, base, d, accounts):
        """One titled block of limit bars per Claude account.

        Each account has its own bar family, announced by a chip of that color
        beside its name. With one account the block has the column to itself;
        with more, each shows only its first windows (session and weekly come
        first in Limits.rows, so those are what survives).
        """
        s = self.scale
        x0, x1 = (value * s for value in _SPLIT_UTILITY)
        accounts = list(accounts)[:max(_BOARD_ACCOUNT_ROWS)]
        if not accounts:
            d.text((x0, T.TILE_TOP * s), "LIMITS", font=self.f_label,
                   fill=self.palette.ink_dim)
            return
        slots = _BOARD_ACCOUNT_ROWS[len(accounts)]
        top, bottom = _SPLIT_DIVIDER_Y
        step = (bottom - top) / len(accounts)
        for index, account in enumerate(accounts):
            fill, remain = self._bar_families[index]
            y = T.TILE_TOP + index * step
            chip = 14 * s
            d.rounded_rectangle([x0, (y + 6) * s, x0 + chip, (y + 6) * s + chip],
                                radius=3 * s, fill=fill)
            stale = bool(account.limits and account.limits.stale)
            if stale:
                # The chip is the key to the bars below, so it fades with them.
                d.rounded_rectangle([x0, (y + 6) * s, x0 + chip, (y + 6) * s + chip],
                                    radius=3 * s,
                                    fill=_mix(fill, self.palette.surface, 0.5))
            note = self._stale_note(d, account.limits, x1, (y + 4) * s)
            d.text((x0 + chip + 10 * s, y * s),
                   self._fit(d, account.name, self.f_label,
                             x1 - x0 - chip - 10 * s - note),
                   font=self.f_label, fill=self.palette.ink_dim)
            rows = list(account.limits.rows) if account.limits else []
            if not rows:
                d.text((x0, (y + 34 + 15) * s), "no data", font=self.f_small,
                       fill=self.palette.ink_faint, anchor="lm")
            for row_index, row in enumerate(rows[:slots]):
                self._limit_bars(base, d, [row], x0, x1,
                                 (y + 34 + row_index * _BOARD_BAR_STEP) * s,
                                 fill=fill, remain=remain, stale=stale)

    def render_board(self, metrics: dict, drives: list, sessions: list,
                     summary: str, accounts=(), facts=(),
                     pulse_phase=0) -> Image.Image:
        """Drives and three metrics left, sessions, then limits per account."""
        s = self.scale
        W, H = T.WIDTH * s, T.HEIGHT * s
        img = Image.new("RGBA", (W, H), self.palette.surface)
        d = ImageDraw.Draw(img)

        self._header(d, W)
        self._header_facts(d, W, list(facts))

        self._board_drives(img, d, drives)
        cards = iter(_BOARD_CARDS)
        for key in ("cpu", "ram"):
            if key in metrics:
                self._board_card(img, d, metrics[key], next(cards))
        if "net_down" in metrics or "net_up" in metrics:
            self._board_net(img, d, metrics.get("net_down"),
                            metrics.get("net_up"), next(cards))

        divider_top, divider_bottom = _SPLIT_DIVIDER_Y
        d.line([(_SPLIT_X * s, divider_top * s), (_SPLIT_X * s, divider_bottom * s)],
               fill=self.palette.ink_faint, width=max(1, s))

        self._split_sessions(d, sessions, summary, pulse_phase=pulse_phase,
                             dense=True)
        self._board_accounts(img, d, accounts)

        return self._finish(img)

    def render_split(self, metrics: dict, sessions: list, summary: str,
                     footer: list[tuple[str, str]] | None = None,
                     limits=None, pulse_phase=0) -> Image.Image:
        """Four metrics left and sessions right, with an explicit pulse phase."""
        s = self.scale
        W, H = T.WIDTH * s, T.HEIGHT * s
        img = Image.new("RGBA", (W, H), self.palette.surface)
        d = ImageDraw.Draw(img)

        self._header(d, W)

        split_x = _SPLIT_X * s
        items = list(metrics.values())
        for metric, box in zip(items, _SPLIT_METRIC_CARDS):
            self._metric_card(img, d, metric, box)

        # Vertical separation of the two halves
        divider_top, divider_bottom = _SPLIT_DIVIDER_Y
        d.line([(split_x, divider_top * s), (split_x, divider_bottom * s)],
               fill=self.palette.ink_faint, width=max(1, s))

        self._split_sessions(d, sessions, summary,
                             pulse_phase=pulse_phase)
        self._split_utility(img, d, limits, footer)

        return self._finish(img)
