"""Serve the exact composed dashboard frame over HTTP.

This is a viewer, not a second render path. The one image `_compose()` builds
each interval is the single source of truth: `cmd_run` pushes that very image
both to `Panel.show()` and to this server's frame buffer, so the browser sees
the same pixels as the panel (in viewer orientation, before `to_wire()`'s
rotation for the wire). Nothing here composes a frame of its own.

Stdlib only - `http.server.ThreadingHTTPServer` on a daemon thread - so the
PyInstaller freeze gains no dependency and it runs the same on Linux and
Windows.

Endpoints:
  GET /           an HTML page that re-fetches /frame.png ~1x/s (cache-busted)
  GET /frame.png  the latest composed frame as PNG; 503 before the first frame

The PNG is encoded lazily and cached under the lock, so repeat requests for an
unchanged frame do not re-encode.
"""

from __future__ import annotations

import io
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

log = logging.getLogger("tzmrit_display")

# The page letterboxes the 1920x462 (4:1) frame on a dark ground matching the
# panel surface and swaps a preloaded image in, so the refresh never flickers
# or flashes a broken-image gap.
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tzmrit-display</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b0d10; }
  body { display: flex; align-items: center; justify-content: center; }
  img {
    max-width: 100%; max-height: 100%;
    aspect-ratio: 1920 / 462; object-fit: contain;
  }
</style>
</head>
<body>
<img id="frame" alt="dashboard frame">
<script>
  var img = document.getElementById('frame');
  function tick() {
    var next = new Image();
    next.onload = function () { img.src = next.src; };
    next.src = '/frame.png?t=' + Date.now();
  }
  tick();
  setInterval(tick, 1000);
</script>
</body>
</html>
"""


class FrameBuffer:
    """Thread-safe holder for the latest composed frame.

    Producer (`set`) hands over the PIL image `_compose()` produced; consumers
    (HTTP request threads) call `png()`. The PNG bytes are encoded on first
    read after a new frame and cached until the next `set`, so an unchanged
    frame served many times is encoded once.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._img = None
        self._png = None

    def set(self, img):
        with self._lock:
            self._img = img
            self._png = None

    def png(self):
        """Latest frame as PNG bytes, or None if no frame has been set yet."""
        with self._lock:
            if self._img is None:
                return None
            if self._png is None:
                buf = io.BytesIO()
                self._img.save(buf, format="PNG")
                self._png = buf.getvalue()
            return self._png


def _make_handler(buffer: FrameBuffer):
    class Handler(BaseHTTPRequestHandler):
        # Keep request logging off stderr; route it to our logger at debug.
        def log_message(self, fmt, *args):
            log.debug("http %s - %s", self.address_string(), fmt % args)

        def _send(self, code, body, content_type, extra_headers=()):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra_headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, INDEX_HTML.encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            if path == "/frame.png":
                png = buffer.png()
                if png is None:
                    self._send(503, b"no frame yet", "text/plain; charset=utf-8")
                    return
                self._send(200, png, "image/png", extra_headers=(
                    ("Cache-Control", "no-store, no-cache, must-revalidate"),
                    ("Pragma", "no-cache"),
                ))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


class FrameServer:
    """Owns the HTTP server thread and the frame buffer it serves.

    Bind to port 0 to get an ephemeral port; read the real one back from
    `.port` after construction (the socket binds in the constructor).
    """

    def __init__(self, host: str, port: int, buffer: FrameBuffer | None = None):
        self.buffer = buffer if buffer is not None else FrameBuffer()
        self._httpd = ThreadingHTTPServer((host, port), _make_handler(self.buffer))
        self._thread = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="tzmrit-http", daemon=True)
        self._thread.start()

    def set_frame(self, img) -> None:
        self.buffer.set(img)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
