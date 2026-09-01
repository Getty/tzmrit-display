"""Tests for the HTTP frame server and its CLI wiring.

No hardware and no real network exposure: every live server binds 127.0.0.1
on port 0 (ephemeral) and the real port is read back, so tests never collide
and never listen on a routable address.

The load-bearing claim here is single-source-of-truth: the bytes the browser
gets are a PNG of the very image `_compose()` produced. The headless test
drives the actual `_run_headless` compose->buffer path, not a stand-in.
"""

import types
import urllib.request
import urllib.error

from PIL import Image

from tzmrit_display import cli
from tzmrit_display.webserver import FrameBuffer, FrameServer


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)


class TestFrameServerHTTP:
    def test_index_and_frame_served_once_a_frame_is_set(self):
        server = FrameServer("127.0.0.1", 0)
        server.start()
        try:
            port = server.port

            # Index page is served and points the browser at /frame.png.
            resp = _get(port, "/")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/html")
            body = resp.read().decode("utf-8")
            assert "/frame.png" in body

            # Before any frame: 503.
            try:
                _get(port, "/frame.png")
                assert False, "expected 503 before first frame"
            except urllib.error.HTTPError as exc:
                assert exc.code == 503

            # After a frame is set: 200, image/png, PNG magic.
            server.set_frame(Image.new("RGB", (1920, 462), (10, 20, 30)))
            resp = _get(port, "/frame.png")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.headers["Cache-Control"] is not None
            data = resp.read()
            assert data.startswith(b"\x89PNG")
        finally:
            server.stop()


class TestFrameBuffer:
    def test_none_until_set_then_caches_png(self):
        buf = FrameBuffer()
        assert buf.png() is None
        buf.set(Image.new("RGB", (4, 4), (1, 2, 3)))
        png = buf.png()
        assert png is not None and png.startswith(b"\x89PNG")
        # Same object cached until the next set (byte-identical read).
        assert buf.png() is png


class TestHeadless:
    def test_compose_feeds_the_buffer_without_a_panel(self):
        # Drive the real headless loop for exactly one frame and prove the
        # composed image landed in the buffer as a PNG - no Panel involved.
        server = FrameServer("127.0.0.1", 0)
        args = types.SimpleNamespace(claude=False, interval=0.0, scale=1)

        calls = {"n": 0}

        def stop_requested():
            # Let the body run once, then request stop.
            calls["n"] += 1
            return calls["n"] > 1

        rc = cli._run_headless(
            cli.SystemSource(),
            cli.DashboardRenderer(scale=1),
            args,
            server,
            stop_requested,
            wait=lambda s: None,
        )
        assert rc == 0
        png = server.buffer.png()
        assert png is not None and png.startswith(b"\x89PNG")


class TestCliParsing:
    def test_http_flags_parse(self):
        args = cli.build_parser().parse_args(
            ["run", "--http", "8080", "--http-host", "127.0.0.1", "--no-panel"])
        assert args.http == 8080
        assert args.http_host == "127.0.0.1"
        assert args.no_panel is True

    def test_http_defaults(self):
        args = cli.build_parser().parse_args(["run"])
        assert args.http is None
        assert args.http_host == "0.0.0.0"
        assert args.no_panel is False

    def test_no_panel_without_http_is_rejected(self):
        # Early rc-1 with a clear message, before any Panel work.
        rc = cli.main(["run", "--no-panel"])
        assert rc == 1
