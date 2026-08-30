"""Layout engine for the 1920x462 strip.

The aspect ratio is 4:1 - a wide, shallow band. The layouts are designed for
that shape rather than being stretched square dashboards.

Two views share the same building blocks:

  render()        six metric columns across the full width
  render_split()  four metrics on the left, running Claude sessions on the right

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


def _color(status: str) -> str:
    return {"warn": T.WARN, "crit": T.CRIT}.get(status, T.ACCENT)


def _mix(hex_a: str, hex_b: str, t: float) -> tuple[int, int, int]:
    """Blend two #RRGGBB colors, t=0 -> a, t=1 -> b."""
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))


# Inactivity escalation for the session counter. These are deliberately MUTED
# blends of gray toward the status hue, not the pure reserved WARN/CRIT: the
# numeric value ("6m", "1h") is the real (non-color) encoding, so the tint only
# nudges. ~55% keeps them recognizably grau-gelb / grau-rot without claiming to
# be the status colors, which stay reserved for crossed thresholds + a marker.
_STALE_WARN = _mix(T.INK_DIM, T.WARN, 0.55)   # >= 5m idle
_STALE_CRIT = _mix(T.INK_DIM, T.CRIT, 0.55)   # >= 1h idle


def _inactive_color(seconds: float):
    """Color for the inactivity counter, escalating with idle time."""
    if seconds >= 3600:
        return _STALE_CRIT
    if seconds >= 300:
        return _STALE_WARN
    return T.INK_FAINT


def _spark_points(metric, x0, y0, w, h):
    """Map a history into pixel coordinates.

    Without a fixed scale the values are scaled from the history, but against
    a floor - otherwise the curve zooms into quiet readings and presents noise
    as a dramatic trend.
    """
    data = list(metric.history)
    if len(data) < 2:
        return []
    if metric.scale_max is not None:
        top = metric.scale_max
    else:
        floor = 1e5 if metric.key.startswith("net") else 1.0
        top = max(max(data), floor)
    top = max(top, 1e-6)
    step = w / max(1, len(data) - 1)
    return [(x0 + i * step, y0 + h - min(1.0, v / top) * h) for i, v in enumerate(data)]


class DashboardRenderer:
    """Draws the dashboard strip. `scale` controls supersampling."""

    def __init__(self, scale: int = 2):
        self.scale = max(1, scale)
        s = self.scale
        self.f_label = T.font(T.FONT_LABEL, 23 * s)
        self.f_value = T.font(T.FONT_VALUE, 74 * s)
        self.f_value_sm = T.font(T.FONT_VALUE, 56 * s)
        self.f_unit = T.font(T.FONT_UNIT, 25 * s)
        self.f_clock = T.font(T.FONT_VALUE, 40 * s)
        self.f_small = T.font(T.FONT_TEXT, 21 * s)
        self.f_foot_key = T.font(T.FONT_LABEL, 19 * s)
        self.f_session = T.font(T.FONT_LABEL, 29 * s)
        self.f_session_sub = T.font(T.FONT_TEXT, 22 * s)

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

    def _warning_mark(self, d, x, y, size, color):
        """Warning triangle as a second encoding beside the status color.

        Status colors never appear alone - someone who cannot tell red from
        yellow still sees that something here is flagged.
        """
        h = size * 0.88
        d.polygon([(x + size / 2, y), (x + size, y + h), (x, y + h)], fill=color)
        d.rectangle([x + size / 2 - size * 0.05, y + h * 0.3,
                     x + size / 2 + size * 0.05, y + h * 0.66], fill=T.SURFACE)

    def _sparkline(self, base, d, metric, x, y, w, h, color):
        pts = _spark_points(metric, x, y, w, h)
        if len(pts) < 2:
            return
        s = self.scale
        fill_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(fill_layer)
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        fd.polygon(pts + [(pts[-1][0], y + h), (pts[0][0], y + h)], fill=rgb + (46,))
        base.alpha_composite(fill_layer)
        d.line(pts, fill=color, width=max(1, 2 * s), joint="curve")
        r = 3.5 * s
        d.ellipse([pts[-1][0] - r, pts[-1][1] - r, pts[-1][0] + r, pts[-1][1] + r], fill=color)

    # -- sections --------------------------------------------------------

    def _header(self, d, W, right_text=None):
        s = self.scale
        now = datetime.datetime.now()
        date = f"{WEEKDAYS[now.weekday()]}, {MONTHS[now.month - 1]} {now.day}"
        d.text((T.MARGIN_X * s, T.HEADER_Y * s), date.upper(), font=self.f_label, fill=T.INK_DIM)
        d.text((W - T.MARGIN_X * s, T.HEADER_Y * s - 6 * s),
               right_text or now.strftime("%H:%M:%S"),
               font=self.f_clock, fill=T.INK, anchor="ra")
        d.line([(T.MARGIN_X * s, T.RULE_Y * s), (W - T.MARGIN_X * s, T.RULE_Y * s)],
               fill=T.INK_FAINT, width=max(1, s))

    def _metric_column(self, img, d, m, x, col_w, compact=False, spark_top=None, spark_h=None):
        s = self.scale
        color = _color(m.status)
        alert = m.status != "ok"
        f_value = self.f_value_sm if compact else self.f_value
        spark_top = (spark_top if spark_top is not None else T.SPARK_TOP) * s
        spark_h = (spark_h if spark_h is not None else T.SPARK_H) * s

        label_x = x
        if alert:
            self._warning_mark(d, x, (T.TILE_TOP + 2) * s, 19 * s, color)
            label_x = x + 27 * s
        d.text((label_x, T.TILE_TOP * s), m.label, font=self.f_label,
               fill=color if alert else T.INK_DIM)
        if m.arrow:
            lw = d.textlength(m.label, font=self.f_label)
            self._arrow(d, label_x + lw + 9 * s, (T.TILE_TOP + 3) * s, 17 * s,
                        color if alert else T.INK_DIM, m.arrow)

        d.text((x, (T.TILE_TOP + 34) * s), m.text, font=f_value,
               fill=color if alert else T.INK)
        if m.sub:
            sub_y = T.TILE_TOP + (106 if compact else 126)
            d.text((x, sub_y * s), m.sub, font=self.f_unit, fill=T.INK_DIM)

        self._sparkline(img, d, m, x, spark_top, col_w * 0.9, spark_h, color)
        d.line([(x, spark_top + spark_h), (x + col_w * 0.9, spark_top + spark_h)],
               fill=T.INK_FAINT, width=max(1, s))

    def _footer_row(self, d, entries, x0, w, y):
        s = self.scale
        col = w / max(1, len(entries))
        for i, (key, val) in enumerate(entries):
            fx = x0 + i * col
            d.text((fx, y), key, font=self.f_foot_key, fill=T.INK_FAINT)
            d.text((fx + d.textlength(key, font=self.f_foot_key) + 14 * s, y - 2 * s),
                   val, font=self.f_small, fill=T.INK_DIM)

    def _name_segments(self, d, name, project, avail):
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
        font = self.f_session

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

    def _session_row(self, d, sess, x, w, y):
        """One session: inactivity counter, marker, name, memory, status.

        The name's project prefix (present in derived names) is drawn in the
        warm amber tint so the redundant project reads as secondary and the
        unique suffix pops; the sub-line is memory only, the project dropped.
        """
        s = self.scale
        if sess.waiting:
            color = T.WARN
        elif sess.working:
            color = T.ACCENT
        else:
            color = T.INK_FAINT

        cy = y + 15 * s
        # Left gutter: time since last LLM activity, right-aligned in a fixed
        # width so the markers and names still line up whatever the value.
        gut = 52 * s
        d.text((x + gut - 10 * s, cy), sess.inactive_text, font=self.f_session_sub,
               fill=_inactive_color(sess.inactive_seconds), anchor="rm")

        mk = x + gut
        if sess.waiting:
            # A triangle rather than a dot: the one state that concerns you is
            # legible without relying on color perception.
            self._warning_mark(d, mk, y + 5 * s, 21 * s, color)
        else:
            r = 8 * s
            box = [mk + 1 * s, cy - r, mk + 1 * s + 2 * r, cy + r]
            if sess.working:
                d.ellipse(box, fill=color)
            else:
                d.ellipse(box, outline=color, width=max(1, 2 * s))

        name_x = mk + 34 * s
        right = x + w
        status_w = d.textlength(sess.status_text, font=self.f_session_sub)
        # Reserve room for the status word before the name can run into it
        avail = right - name_x - status_w - 20 * s

        base = T.INK if (sess.waiting or sess.working) else T.INK_DIM
        seg_x = name_x
        for text, is_prefix in self._name_segments(d, sess.name, sess.project, avail):
            d.text((seg_x, y), text, font=self.f_session,
                   fill=T.ACCENT_WARM if is_prefix else base)
            seg_x += d.textlength(text, font=self.f_session)

        if sess.memory_text:
            # Memory including MCP child processes - they are the bulk of it.
            # The project is not repeated here; it shows as the tinted prefix.
            d.text((name_x, y + 30 * s), sess.memory_text,
                   font=self.f_session_sub, fill=T.INK_FAINT)
        d.text((right, y), sess.status_text, font=self.f_session_sub, fill=color, anchor="ra")

    def _sessions(self, d, sessions, summary, x0, w, y0, y1):
        """Session list, most urgent first.

        If the list does not fit one column it is set in two - the right half
        is over 1000 px wide and a single column uses barely half of that.
        """
        s = self.scale
        d.text((x0, T.TILE_TOP * s), "CLAUDE", font=self.f_label, fill=T.INK_DIM)
        d.text((x0 + d.textlength("CLAUDE", font=self.f_label) + 18 * s,
                T.TILE_TOP * s), summary, font=self.f_small, fill=T.INK_FAINT)

        if not sessions:
            d.text((x0, y0 + 30 * s), "no sessions running",
                   font=self.f_session_sub, fill=T.INK_FAINT)
            return

        room = y1 - y0
        # 54 px per row fits four rows per column, i.e. eight sessions without
        # overflow - the normal case with several agents open.
        row_h = 54 * s
        per_col = max(1, int(room // row_h))
        columns = 1 if len(sessions) <= per_col else 2
        col_w = (w - (28 * s if columns > 1 else 0)) / columns

        capacity = per_col * columns
        shown = sessions[:capacity]
        overflow = len(sessions) - len(shown)
        if overflow:
            shown = sessions[:capacity - 1]
            overflow = len(sessions) - len(shown)

        # With little content, set it more airily and balance it vertically
        rows_in_col = min(per_col, len(shown) + (1 if overflow else 0)) if columns == 1 \
            else per_col
        if columns == 1 and rows_in_col < per_col:
            row_h = min(78 * s, room / max(1, rows_in_col))
        block = row_h * rows_in_col
        y_start = y0 + max(0, (room - block) / 2)

        for i, sess in enumerate(shown):
            col, row = divmod(i, per_col) if columns > 1 else (0, i)
            x = x0 + col * (col_w + 28 * s)
            self._session_row(d, sess, x, col_w, y_start + row * row_h)

        if overflow:
            i = len(shown)
            col, row = divmod(i, per_col) if columns > 1 else (0, i)
            x = x0 + col * (col_w + 28 * s)
            d.text((x + 34 * s, y_start + row * row_h), f"+{overflow} more",
                   font=self.f_session_sub, fill=T.INK_FAINT)

    def _finish(self, img):
        if self.scale > 1:
            img = img.resize((T.WIDTH, T.HEIGHT), Image.LANCZOS)
        return img.convert("RGB")

    # -- views -----------------------------------------------------------

    def render(self, metrics: dict, footer: list[tuple[str, str]]) -> Image.Image:
        """Six metric columns across the full width."""
        s = self.scale
        W, H = T.WIDTH * s, T.HEIGHT * s
        img = Image.new("RGBA", (W, H), T.SURFACE)
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
                       fill=T.INK_FAINT, width=max(1, s))
            self._metric_column(img, d, m, x, col_w)

        fy = T.FOOTER_Y * s
        d.line([(mx, fy - 18 * s), (W - mx, fy - 18 * s)], fill=T.INK_FAINT, width=max(1, s))
        self._footer_row(d, footer, mx, W - 2 * mx, fy)

        return self._finish(img)

    def _limit_bars(self, d, rows, x0, x1, y):
        """Rate-limit budget as two wide bars spanning the footer's right half.

        The pair fills x0..x1 side by side, in the same two blues as the
        sparklines: the consumed fraction (percent/100 from the left) is the
        graph's bright line color T.ACCENT, the remaining budget the graph's
        dark fill family. The sparkline fill is ACCENT at alpha 46 over
        SURFACE, i.e. opaque _mix(SURFACE, ACCENT, 0.18); left there its
        contrast with SURFACE text is only 1.3:1, so it is lightened along the
        same SURFACE->ACCENT axis to t=0.75 (#4580C4). That clears WCAG AA for
        the near-black SURFACE text at 4.75:1, while staying visibly darker
        than the bright ACCENT (7.69:1) beside it so the two blues still read
        as filled-vs-remaining. A bar is itself a marker shape, so the colored
        chip means something without the theme.py ban on bare colored text.
        """
        s = self.scale
        n = max(1, len(rows))
        gap = 18 * s
        h = 30 * s
        rad = int(h // 2)
        pad = 16 * s
        bw = (x1 - x0 - (n - 1) * gap) / n
        cy = y + h / 2
        remain = _mix(T.SURFACE, T.ACCENT, 0.75)  # lightened graph-fill blue
        for i, lim in enumerate(rows):
            bx = x0 + i * (bw + gap)
            d.rounded_rectangle([bx, y, bx + bw, y + h], radius=rad, fill=remain)
            fw = bw * max(0, min(100, lim.percent)) / 100
            if fw > 0:
                d.rounded_rectangle([bx, y, bx + fw, y + h],
                                    radius=int(min(rad, fw / 2)), fill=T.ACCENT)
            d.text((bx + pad, cy), f"{lim.label} {lim.percent}%",
                   font=self.f_small, fill=T.SURFACE, anchor="lm")
            reset = lim.reset_text()
            if reset:
                d.text((bx + bw - pad, cy), reset,
                       font=self.f_small, fill=T.SURFACE, anchor="rm")

    def render_split(self, metrics: dict, sessions: list, summary: str,
                     footer: list[tuple[str, str]] | None = None,
                     limits=None) -> Image.Image:
        """Four metrics on the left, running Claude sessions on the right."""
        s = self.scale
        W, H = T.WIDTH * s, T.HEIGHT * s
        img = Image.new("RGBA", (W, H), T.SURFACE)
        d = ImageDraw.Draw(img)
        mx = T.MARGIN_X * s

        self._header(d, W)

        split_x = int(W * 0.46)
        items = list(metrics.values())
        col_w = (split_x - mx - 20 * s) / max(1, len(items))
        for i, m in enumerate(items):
            x = mx + i * col_w
            if i:
                d.line([(x - col_w * 0.03, (T.TILE_TOP - 6) * s),
                        (x - col_w * 0.03, (T.SPARK_TOP + 78) * s)],
                       fill=T.INK_FAINT, width=max(1, s))
            self._metric_column(img, d, m, x, col_w, compact=True,
                                spark_top=T.SPARK_TOP - 26, spark_h=78)

        # Vertical separation of the two halves
        d.line([(split_x, (T.TILE_TOP - 12) * s), (split_x, (T.FOOTER_Y - 20) * s)],
               fill=T.INK_FAINT, width=max(1, s))

        right_x = split_x + 34 * s
        self._sessions(d, sessions, summary, right_x, W - mx - right_x,
                       (T.TILE_TOP + 40) * s, (T.FOOTER_Y - 22) * s)

        # Bottom band: host facts on the left half; the two limit bars span the
        # right half. The "waiting" notice that used to sit here is dropped -
        # a waiting session already shows in the list (WARN triangle + "waiting
        # for you") and in the CLAUDE header count, so a footer copy is redundant.
        fy = T.FOOTER_Y * s
        d.line([(mx, fy - 18 * s), (W - mx, fy - 18 * s)], fill=T.INK_FAINT, width=max(1, s))
        if footer:
            self._footer_row(d, footer, mx, split_x - mx - 20 * s, fy)
        rows = limits.rows if limits else []
        if rows:
            self._limit_bars(d, rows, right_x, W - mx, fy - 10 * s)

        return self._finish(img)
