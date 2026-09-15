"""Tests for the CLI composition helpers.

Theme selection and pulse parity stay in this composition layer so preview,
headless output, and panel output share the same real renderer. Usage polling
also lives here because claude_limits.py must stay ignorant of claude_sessions.
"""

import time

from PIL import Image

from tzmrit_display import theme as T
import tzmrit_display.cli as cli
from tzmrit_display.claude_sessions import Session
from tzmrit_display.sources import Metric
from tzmrit_display.cli import (
    POLL_ACTIVE,
    POLL_IDLE,
    POLL_RECENT,
    build_parser,
    cmd_preview,
    usage_poll_interval,
)


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _sess(status, inactive):
    """A session whose transcript clock reads `inactive` seconds ago."""
    return Session(1, "n", "/x", status, "i", active_at=time.time() - inactive)


class TestDashboardThemeOption:
    def test_run_and_preview_accept_red_theme(self):
        parser = build_parser()
        for command in ("run", "preview"):
            args = parser.parse_args([command, "--theme", "red"])
            assert args.theme == "red"

    def test_run_and_preview_default_to_blue_theme(self):
        parser = build_parser()
        for command in ("run", "preview"):
            args = parser.parse_args([command])
            assert args.theme == "blue"

    def test_run_and_preview_build_selected_renderer(self):
        parser = build_parser()
        for command in ("run", "preview"):
            red = cli._dashboard_renderer(parser.parse_args([command, "--theme", "red"]))
            blue = cli._dashboard_renderer(parser.parse_args([command]))
            assert red.palette is T.RED_PALETTE
            assert blue.palette is T.BLUE_PALETTE

    def test_headless_run_uses_the_red_renderer_on_its_actual_frame_path(self, monkeypatch):
        class Source:
            def __init__(self):
                self.metric = Metric("cpu", "CPU", scale_max=100)

            def sample(self):
                self.metric.push(100)
                self.metric.text, self.metric.sub = "100", "%"
                return {"cpu": self.metric}

            @staticmethod
            def footer():
                return []

        class Server:
            port = 0

            def __init__(self):
                self.frame = None
                self.stopped = False

            def start(self):
                pass

            def set_frame(self, image):
                self.frame = image

            def stop(self):
                self.stopped = True

        source = Source()
        server = Server()
        stop_requests = iter((False, True))
        monkeypatch.setattr(cli, "SystemSource", lambda: source)
        monkeypatch.setattr(cli, "FrameServer", lambda *args: server)
        monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
        monkeypatch.setattr(cli.runtime, "consume_stop_request", lambda: next(stop_requests))
        monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
        args = build_parser().parse_args([
            "run", "--no-panel", "--http", "0", "--theme", "red", "--scale", "1",
            "--interval", "0",
        ])

        assert cli.cmd_run(args) == 0
        assert server.stopped
        assert server.frame is not None
        assert server.frame.getpixel((1701, 258)) == _rgb(T.RED_PALETTE.accent)

    def test_preview_red_theme_reaches_rendered_sparkline(self, tmp_path):
        out = tmp_path / "preview.png"
        args = build_parser().parse_args([
            "preview", "--theme", "red", "--scale", "1", "--samples", "2",
            "--interval", "0", "--out", str(out),
        ])

        assert cmd_preview(args) == 0
        with Image.open(out) as image:
            colors = image.getcolors(maxcolors=image.width * image.height)
            assert colors is not None
            assert _rgb(T.RED_PALETTE.accent) in {color for _, color in colors}


class TestPulseCompositionAndRunLoops:
    """The CLI forwards local frame parity to the real split renderer."""

    CENTER = (735, 159)
    HALO = (743, 159)
    MARKER = (725, 148, 749, 171)

    class Source:
        def __init__(self):
            self.metrics = {}
            for key in ("cpu", "ram", "temp", "load"):
                metric = Metric(key, key.upper(), scale_max=100)
                metric.text, metric.sub = "42", "%"
                self.metrics[key] = metric

        def sample(self):
            for metric in self.metrics.values():
                metric.push(40)
            return self.metrics

        @staticmethod
        def footer():
            return [("HOST", "host"), ("DISK", "disk"), ("UPTIME", "1h")]

    class Server:
        port = 0

        def __init__(self):
            self.frames = []

        def start(self):
            pass

        def set_frame(self, image):
            self.frames.append(image)

        def stop(self):
            pass

    class Panel:
        port_path = "test-port"
        viewport = (T.WIDTH, T.HEIGHT)

        class Info:
            model = "test-panel"

        info = Info()

        def __init__(self):
            self.frames = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def start_live(self):
            pass

        def show(self, image):
            self.frames.append(image)
            return 0

    @staticmethod
    def _session():
        return Session(
            pid=4000,
            name="working-session",
            cwd="/home/user/project",
            status="busy",
            kind="interactive",
        )

    def _patch_composition_boundaries(self, monkeypatch, source):
        monkeypatch.setattr(cli, "SystemSource", lambda: source)
        monkeypatch.setattr(cli, "list_sessions", lambda: [self._session()])
        monkeypatch.setattr(cli, "get_limits", lambda *args: None)
        monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    def _assert_phase_zero_one_zero(self, frames):
        from PIL import ImageChops

        assert len(frames) == 3
        accent = _rgb(T.ACCENT)
        surface = _rgb(T.SURFACE)
        assert [image.getpixel(self.CENTER) for image in frames] \
            == [accent, accent, accent]
        halo_pixels = [image.getpixel(self.HALO) for image in frames]
        assert halo_pixels[0] == surface
        assert halo_pixels[1] not in (surface, accent, _rgb(T.WARN), _rgb(T.CRIT))
        assert halo_pixels[2] == surface
        assert ImageChops.difference(
            frames[0].crop(self.MARKER), frames[2].crop(self.MARKER)
        ).getbbox() is None
        assert ImageChops.difference(
            frames[0].crop(self.MARKER), frames[1].crop(self.MARKER)
        ).getbbox() is not None

    def test_compose_forwards_explicit_phase_and_defaults_to_phase_zero(self, monkeypatch):
        source = self.Source()
        self._patch_composition_boundaries(monkeypatch, source)
        renderer = cli.DashboardRenderer(scale=1)

        default = cli._compose(source, renderer, True)
        phase0 = cli._compose(source, renderer, True, pulse_phase=0)
        phase1 = cli._compose(source, renderer, True, pulse_phase=1)

        assert default.getpixel(self.HALO) == _rgb(T.SURFACE)
        assert phase0.getpixel(self.HALO) == _rgb(T.SURFACE)
        assert phase1.getpixel(self.HALO) not in (
            _rgb(T.SURFACE), _rgb(T.ACCENT), _rgb(T.WARN), _rgb(T.CRIT)
        )

    def test_headless_run_alternates_consecutive_rendered_frames(self, monkeypatch):
        source = self.Source()
        server = self.Server()
        self._patch_composition_boundaries(monkeypatch, source)
        monkeypatch.setattr(cli, "FrameServer", lambda *args: server)
        monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
        monkeypatch.setattr(
            cli.runtime, "consume_stop_request", lambda: len(server.frames) >= 3
        )
        args = build_parser().parse_args([
            "run", "--no-panel", "--http", "0", "--claude", "--scale", "1",
            "--interval", "0",
        ])

        assert cli.cmd_run(args) == 0
        self._assert_phase_zero_one_zero(server.frames)

    def test_panel_run_alternates_consecutive_rendered_frames(self, monkeypatch):
        source = self.Source()
        panel = self.Panel()
        self._patch_composition_boundaries(monkeypatch, source)
        monkeypatch.setattr(cli, "Panel", lambda: panel)
        monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
        monkeypatch.setattr(
            cli.runtime, "consume_stop_request", lambda: len(panel.frames) >= 3
        )
        monkeypatch.setattr(cli.runtime, "clear_stale_stop_request", lambda: None)
        monkeypatch.setattr(cli.runtime, "read_instance", lambda: None)
        monkeypatch.setattr(cli.runtime, "claim_instance", lambda: None)
        monkeypatch.setattr(cli.runtime, "release_instance", lambda: None)
        args = build_parser().parse_args([
            "run", "--claude", "--scale", "1", "--interval", "0",
        ])

        assert cli.cmd_run(args) == 0
        self._assert_phase_zero_one_zero(panel.frames)

    def test_preview_marker_is_repeatable_and_fixed_to_phase_zero(
            self, monkeypatch, tmp_path):
        from PIL import ImageChops

        source = self.Source()
        self._patch_composition_boundaries(monkeypatch, source)
        outputs = [tmp_path / "phase-a.png", tmp_path / "phase-b.png"]

        for output in outputs:
            args = build_parser().parse_args([
                "preview", "--claude", "--scale", "1", "--out", str(output),
            ])
            assert cmd_preview(args) == 0

        with Image.open(outputs[0]) as first, Image.open(outputs[1]) as second:
            assert first.getpixel(self.CENTER) == _rgb(T.ACCENT)
            assert first.getpixel(self.HALO) == _rgb(T.SURFACE)
            assert ImageChops.difference(
                first.crop(self.MARKER), second.crop(self.MARKER)
            ).getbbox() is None


class TestUsagePollInterval:
    def test_no_sessions_is_idle(self):
        assert usage_poll_interval([]) == POLL_IDLE

    def test_recent_turn_polls_fast(self):
        # A turn moments ago (transcript ~5s old) -> poll every minute.
        assert usage_poll_interval([_sess("busy", 5)]) == POLL_ACTIVE

    def test_activity_within_minutes_polls_medium(self):
        # 2 min since the last turn, nothing waiting/working now -> 3 min.
        assert usage_poll_interval([_sess("idle", 120)]) == POLL_RECENT

    def test_quiet_board_polls_slow(self):
        # Everything quiet for 10 min -> back off to 10 min.
        assert usage_poll_interval([_sess("idle", 600)]) == POLL_IDLE

    def test_waiting_session_keeps_medium_even_when_old(self):
        # A session waiting for a human writes no transcript, so its clock runs
        # up; but a human is expected any moment, so don't sink to fully idle.
        assert usage_poll_interval([_sess("waiting", 600)]) == POLL_RECENT

    def test_working_session_floors_at_medium(self):
        # A genuinely-working (busy + fresh) session with an oddly old clock
        # still counts as activity -> at least medium cadence.
        s = _sess("busy", 3)  # fresh -> working True, and min_inactive < 60
        assert usage_poll_interval([s]) == POLL_ACTIVE

    def test_min_across_sessions_wins(self):
        # One quiet session, one active: the most-recent activity drives it.
        sessions = [_sess("idle", 600), _sess("busy", 4)]
        assert usage_poll_interval(sessions) == POLL_ACTIVE

    def test_ordering_of_the_three_intervals(self):
        assert POLL_ACTIVE < POLL_RECENT < POLL_IDLE
