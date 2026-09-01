"""Command line interface for the panel."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

import serial
from PIL import Image

from . import runtime
from .claude_limits import get_limits
from .claude_sessions import list_sessions, summarize
from .panel import Panel, PanelError, find_port
from .render import DashboardRenderer
from .sources import SystemSource
from .webserver import FrameServer

log = logging.getLogger("tzmrit_display")

# The split layout has room for four metrics. This is a preference order, not
# a fixed set: temperature and load average are unavailable on Windows, so
# disk usage moves up rather than leaving a gap.
SPLIT_METRICS = ("cpu", "ram", "temp", "load", "disk", "net_down", "net_up")

# How often `run` probes for the panel while it is absent or unplugged. A
# fixed short interval rather than a backoff: listing serial ports is cheap,
# and the Linux systemd unit already retries on the same order (RestartSec=5).
RECONNECT_POLL = 3.0

# How often to poll the account usage endpoint, chosen from how recently anyone
# was active on the session board. Usage moves over hours, so a quiet board is
# polled rarely; only a live turn warrants the fast cadence. These feed
# get_limits(ttl) - the client itself stays ignorant of the session board.
POLL_ACTIVE = 60.0    # a turn in the last minute: someone is working now
POLL_RECENT = 180.0   # activity in the last few minutes, or a session waiting
POLL_IDLE = 600.0     # board quiet: check about every ten minutes
_RECENT_WINDOW = 300.0  # "the last few minutes" for POLL_RECENT (5 min)


def usage_poll_interval(sessions) -> float:
    """Desired usage-poll interval (seconds) for the current board state.

    Pure and side-effect free so it can be unit-tested and reasoned about. The
    signal is the smallest `inactive_seconds` across sessions - the
    transcript-activity clock we already compute - with any waiting or working
    session treated as live activity (a waiting session writes no transcript, so
    its clock runs up even though a human is expected any moment).

    < 60s since the last turn                      -> POLL_ACTIVE
    < 5 min, or any session working/waiting        -> POLL_RECENT
    otherwise (board quiet)                        -> POLL_IDLE
    """
    if not sessions:
        return POLL_IDLE
    min_inactive = min(s.inactive_seconds for s in sessions)
    any_live = any(s.working or s.waiting for s in sessions)
    if min_inactive < POLL_ACTIVE:
        return POLL_ACTIVE
    if min_inactive < _RECENT_WINDOW or any_live:
        return POLL_RECENT
    return POLL_IDLE


def _compose(src, renderer, with_claude):
    """Build the image for the selected mode."""
    metrics = src.sample()
    if not with_claude:
        return renderer.render(metrics, src.footer())
    chosen = {}
    for key in SPLIT_METRICS:
        if key in metrics and len(chosen) < 4:
            chosen[key] = metrics[key]
    for key, metric in metrics.items():  # anything unusual still fills a gap
        if key not in chosen and len(chosen) < 4:
            chosen[key] = metric
    sessions = list_sessions()
    # cached, refreshed off-thread; None until it lands. The TTL rides the board
    # activity so a quiet board is polled far less than a busy one.
    limits = get_limits(usage_poll_interval(sessions))
    return renderer.render_split(chosen, sessions, summarize(sessions),
                                 src.footer(), limits)


def _run_headless(src, renderer, args, server, stop_requested, wait) -> int:
    """Fully headless run: compose frames on --interval, serve them, no panel.

    Touches none of the Panel/reconnect/instance machinery - there is no panel
    to drive or hand over. It shares the loop's compose step (so the served
    frame is the same image the panel path would show) and the same
    signal/stop-request handling for a clean rc-0 exit.
    """
    log.info("headless mode: composing frames for HTTP only, no panel")
    # Prime the history so the sparklines don't grow out of nothing, matching
    # the panel path.
    for _ in range(3):
        src.sample()
        time.sleep(0.2)
    if args.claude:
        get_limits()
    frames = 0
    while not stop_requested():
        began = time.monotonic()
        img = _compose(src, renderer, args.claude)
        server.set_frame(img)
        frames += 1
        wait(max(0.0, args.interval - (time.monotonic() - began)))
    log.info("stopped after %d frames", frames)
    return 0


def cmd_info(args) -> int:
    port = find_port()
    if not port:
        print("No panel found (VID 33c3).", file=sys.stderr)
        return 1
    with Panel(port) as p:
        i = p.info
        print(f"Port        {port}")
        print(f"Model       {i.model}")
        print(f"Geometry    {i.width} x {i.height}   (angle {i.angle})")
        print(f"Firmware    {i.version}   length header: {'yes' if i.uses_length_header else 'no'}")
        print(f"Brightness  {i.brightness}")
        print(f"Budget      {i.max_frame_kb} KB per frame")
        print(f"UID         {i.uid}")
    return 0


def cmd_preview(args) -> int:
    """Render a PNG without touching the hardware - handy for layout work."""
    src = SystemSource()
    renderer = DashboardRenderer(scale=args.scale)
    for _ in range(max(1, args.samples - 1)):
        src.sample()
        time.sleep(args.interval if args.samples > 1 else 0)
    img = _compose(src, renderer, args.claude)
    img.save(args.out)
    print(f"wrote {args.out} ({img.width}x{img.height})")
    return 0


def cmd_image(args) -> int:
    img = Image.open(args.file).convert("RGB")
    with Panel() as p:
        p.start_live()
        if args.brightness is not None:
            p.set_brightness(args.brightness)
        size = p.show(img)
        print(f"Displayed: {size / 1024:.1f} KB. Ctrl-C exits (image stays).")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print()
    return 0


def cmd_clear(args) -> int:
    with Panel() as p:
        p.clear()
    print("Panel cleared.")
    return 0


def cmd_brightness(args) -> int:
    with Panel() as p:
        p.start_live()
        p.set_brightness(args.level)
    print(f"Brightness set to {args.level}.")
    return 0


def cmd_stop(args) -> int:
    """Ask the running dashboard to exit gracefully.

    Cooperative via the runtime stop-request file - no process is killed, so
    the panel is closed exactly as on Ctrl+C (including --blank-on-exit if
    the running instance was started with it). Exit codes: 0 the dashboard
    exited, 1 no dashboard was running, 2 the request was delivered but the
    dashboard had not exited within the wait.
    """
    pid = runtime.read_instance()
    if pid is None:
        print("No dashboard running.")
        return 1
    runtime.request_stop(pid)
    deadline = time.monotonic() + runtime.STOP_WAIT
    while time.monotonic() < deadline:
        if not runtime.pid_alive(pid):
            print("Dashboard stopped.")
            return 0
        time.sleep(0.2)
    print("Stop requested, but the dashboard has not exited yet.", file=sys.stderr)
    return 2


def cmd_run(args) -> int:
    """Continuous dashboard.

    Survives the panel being absent at start or unplugged mid-run: the session
    loop waits for the port to (re)appear and reconnects through the normal
    cold-start sequence (connect, live mode, brightness). Metric history lives
    in `src` and is kept across reconnects.

    Only one dashboard drives the panel: a second `run` asks a running
    instance to exit and takes over, and `stop` requests the same shutdown.
    Both go through the runtime stop-request file, which the loops below poll;
    honoring it is identical to Ctrl+C, and the clean rc 0 keeps systemd's
    Restart=on-failure from restarting a deliberately stopped instance.
    """
    if args.no_panel and args.http is None:
        print("--no-panel requires --http PORT", file=sys.stderr)
        return 1

    src = SystemSource()
    renderer = DashboardRenderer(scale=args.scale)
    stop = False

    def handler(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    def stop_requested():
        """Signal received, or a stop request addressed to this instance."""
        nonlocal stop
        if not stop and runtime.consume_stop_request():
            log.info("stop requested, shutting down")
            stop = True
        return stop

    def wait(seconds):
        """Sleep in short slices so signals and stop requests stay responsive."""
        deadline = time.monotonic() + seconds
        while not stop_requested() and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    server = None
    if args.http is not None:
        server = FrameServer(args.http_host, args.http)
        server.start()
        log.warning("HTTP dashboard on http://%s:%d - this exposes your system "
                    "metrics and Claude session names/projects to anyone who "
                    "can reach that address; restrict it with "
                    "--http-host 127.0.0.1", args.http_host, server.port)

    # Fully headless: no panel to drive or hand over, so skip the reconnect and
    # instance machinery entirely and just compose frames for the web buffer.
    if args.no_panel:
        try:
            return _run_headless(src, renderer, args, server, stop_requested, wait)
        finally:
            server.stop()

    # Take over from an already running dashboard (e.g. the other Start menu
    # variant): ask it to exit and give it a bounded window to let go of the
    # port. If it does not, the reconnect loop below waits it out instead of
    # a second instance lingering silently forever.
    runtime.clear_stale_stop_request()
    other = runtime.read_instance()
    if other is not None:
        log.info("dashboard pid %d already running, asking it to hand over", other)
        runtime.request_stop(other)
        deadline = time.monotonic() + runtime.STOP_WAIT
        while time.monotonic() < deadline and runtime.pid_alive(other):
            time.sleep(0.2)
        if runtime.pid_alive(other):
            log.warning("pid %d has not exited; if it still holds the port, "
                        "this instance will wait in the reconnect loop", other)
    runtime.claim_instance()

    total_frames = 0
    primed = False
    try:
        while not stop_requested():
            try:
                with Panel() as p:
                    log.info("panel %s on %s, %dx%d", p.info.model, p.port_path, *p.viewport)
                    p.start_live()
                    if args.brightness is not None:
                        p.set_brightness(args.brightness)

                    if not primed:
                        # Prime the history so the sparklines don't grow out of nothing
                        for _ in range(3):
                            src.sample()
                            time.sleep(0.2)
                        primed = True

                    # Kick the first usage fetch off-thread now, so the limit
                    # bars have data as early as possible on (re)connect rather
                    # than only after the first frame triggers it.
                    if args.claude:
                        get_limits()

                    frames, t_start, last_log = 0, time.monotonic(), time.monotonic()
                    while not stop_requested():
                        began = time.monotonic()
                        img = _compose(src, renderer, args.claude)
                        size = p.show(img)
                        if server is not None:
                            server.set_frame(img)
                        frames += 1
                        total_frames += 1
                        if time.monotonic() - last_log > 60:
                            elapsed = time.monotonic() - t_start
                            log.info("%d frames, %.2f fps, last %.0f KB", frames, frames / elapsed, size / 1024)
                            last_log = time.monotonic()
                        wait(max(0.0, args.interval - (time.monotonic() - began)))

                    if args.blank_on_exit:
                        p.close(blank=True)
            except (PanelError, serial.SerialException) as exc:
                # Panel not plugged in yet, or unplugged mid-run. The context
                # manager already closed the port; wait for it to (re)appear.
                if stop:
                    break
                log.warning("panel lost: %s", exc)
                log.info("waiting for the panel (polling every %.0f s)", RECONNECT_POLL)
                while not stop_requested():
                    # Sleep first: on Windows a replugged port can enumerate
                    # before it is openable, so never retry back-to-back.
                    wait(RECONNECT_POLL)
                    if not stop and find_port():
                        log.info("panel port is back, reconnecting")
                        break
    finally:
        runtime.release_instance()
        if server is not None:
            server.stop()
    log.info("stopped after %d frames", total_frames)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tzmrit-display", description="Drive a HONGTAI USB LCD")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="show the dashboard continuously")
    p_run.add_argument("--interval", type=float, default=1.0, help="seconds per frame (default 1.0)")
    p_run.add_argument("--scale", type=int, default=2, help="supersampling factor (default 2)")
    p_run.add_argument("--brightness", type=int, default=None)
    p_run.add_argument("--blank-on-exit", action="store_true", help="clear the panel on exit")
    p_run.add_argument("--claude", action="store_true",
                       help="split layout: system metrics left, running Claude sessions right")
    p_run.add_argument("--http", type=int, default=None, metavar="PORT",
                       help="also serve the exact rendered frame over HTTP on PORT")
    p_run.add_argument("--http-host", default="0.0.0.0", metavar="HOST",
                       help="bind address for --http (default 0.0.0.0; use "
                            "127.0.0.1 to keep it on this machine)")
    p_run.add_argument("--no-panel", action="store_true",
                       help="headless: serve over --http only, no panel (requires --http)")
    p_run.set_defaults(func=cmd_run)

    p_stop = sub.add_parser(
        "stop",
        help="ask a running dashboard to exit gracefully "
             "(rc 0 stopped, 1 none running, 2 not confirmed)")
    p_stop.set_defaults(func=cmd_stop)

    p_prev = sub.add_parser("preview", help="render a PNG without using the panel")
    p_prev.add_argument("-o", "--out", default="preview.png")
    p_prev.add_argument("--scale", type=int, default=2)
    p_prev.add_argument("--samples", type=int, default=1, help="samples to collect first")
    p_prev.add_argument("--interval", type=float, default=0.3)
    p_prev.add_argument("--claude", action="store_true", help="split layout with Claude sessions")
    p_prev.set_defaults(func=cmd_preview)

    p_img = sub.add_parser("image", help="display an image")
    p_img.add_argument("file")
    p_img.add_argument("--brightness", type=int, default=None)
    p_img.set_defaults(func=cmd_image)

    p_clr = sub.add_parser("clear", help="clear the panel")
    p_clr.set_defaults(func=cmd_clear)

    p_br = sub.add_parser("brightness", help="set brightness (0-100)")
    p_br.add_argument("level", type=int)
    p_br.set_defaults(func=cmd_brightness)

    sub.add_parser("info", help="device information").set_defaults(func=cmd_info)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
    )
    try:
        return args.func(args)
    except (PanelError, serial.SerialException) as exc:
        # Backstop for the one-shot commands (and anything unforeseen): a
        # vanished port must never surface as an unhandled-exception dialog
        # in the windowed build.
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
