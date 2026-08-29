"""Command line interface for the panel."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

import serial
from PIL import Image

from .claude_sessions import list_sessions, summarize
from .panel import Panel, PanelError, find_port
from .render import DashboardRenderer
from .sources import SystemSource

log = logging.getLogger("tzmrit_display")

# The split layout has room for four metrics. This is a preference order, not
# a fixed set: temperature and load average are unavailable on Windows, so
# disk usage moves up rather than leaving a gap.
SPLIT_METRICS = ("cpu", "ram", "temp", "load", "disk", "net_down", "net_up")

# How often `run` probes for the panel while it is absent or unplugged. A
# fixed short interval rather than a backoff: listing serial ports is cheap,
# and the Linux systemd unit already retries on the same order (RestartSec=5).
RECONNECT_POLL = 3.0


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
    return renderer.render_split(chosen, sessions, summarize(sessions), src.footer())


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


def cmd_run(args) -> int:
    """Continuous dashboard.

    Survives the panel being absent at start or unplugged mid-run: the session
    loop waits for the port to (re)appear and reconnects through the normal
    cold-start sequence (connect, live mode, brightness). Metric history lives
    in `src` and is kept across reconnects.
    """
    src = SystemSource()
    renderer = DashboardRenderer(scale=args.scale)
    stop = False

    def handler(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    def wait(seconds):
        """Sleep in short slices so a stop signal stays responsive."""
        deadline = time.monotonic() + seconds
        while not stop and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    total_frames = 0
    primed = False
    while not stop:
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

                frames, t_start, last_log = 0, time.monotonic(), time.monotonic()
                while not stop:
                    began = time.monotonic()
                    size = p.show(_compose(src, renderer, args.claude))
                    frames += 1
                    total_frames += 1
                    if time.monotonic() - last_log > 60:
                        elapsed = time.monotonic() - t_start
                        log.info("%d frames, %.2f fps, last %.0f KB", frames, frames / elapsed, size / 1024)
                        last_log = time.monotonic()
                    time.sleep(max(0.0, args.interval - (time.monotonic() - began)))

                if args.blank_on_exit:
                    p.close(blank=True)
        except (PanelError, serial.SerialException) as exc:
            # Panel not plugged in yet, or unplugged mid-run. The context
            # manager already closed the port; wait for it to (re)appear.
            if stop:
                break
            log.warning("panel lost: %s", exc)
            log.info("waiting for the panel (polling every %.0f s)", RECONNECT_POLL)
            while not stop:
                # Sleep first: on Windows a replugged port can enumerate
                # before it is openable, so never retry back-to-back.
                wait(RECONNECT_POLL)
                if not stop and find_port():
                    log.info("panel port is back, reconnecting")
                    break
    log.info("stopped after %d frames", total_frames)
    return 0


def main(argv=None) -> int:
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
    p_run.set_defaults(func=cmd_run)

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
