"""System metrics with a short history for the sparklines."""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field

import psutil

HISTORY = 90  # samples per metric

# Windows has neither a load average nor CPU temperature sensors via psutil.
# Both are probed rather than assumed, so the metric set adapts per platform.
HAS_LOADAVG = hasattr(os, "getloadavg")

# "/" is ambiguous on Windows; the home directory's anchor gives C:\ there
# and "/" on Unix.
ROOT_PATH = Path.home().anchor or "/"

WINDOWS = sys.platform == "win32"

# Drive usage moves slowly, and asking a network drive for it can block for
# as long as its server takes to time out - so it is read off the frame loop
# and reused for a while.
DRIVES_TTL = 30.0
# How long the very first drives() call may wait for that reading, so the
# first frame is not drawn with an empty list.
DRIVES_FIRST_WAIT = 2.0

_REMOTE_FS = frozenset({
    "nfs", "nfs4", "cifs", "smb", "smbfs", "smb3", "sshfs", "fuse.sshfs",
    "9p", "afpfs", "davfs", "ceph", "glusterfs",
})


@dataclass
class Metric:
    """One measurement with its history and thresholds.

    `warn`/`crit` are optional - a network rate has no meaningful threshold,
    and a tile without one stays neutrally colored at all times.
    """

    key: str
    label: str
    value: float = 0.0
    text: str = "-"
    sub: str = ""
    warn: float | None = None
    crit: float | None = None
    # Direction arrow is drawn, not typeset: Roboto has no U+2191/U+2193
    arrow: str | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    # Fixed sparkline scale; None means derive it from the history
    scale_max: float | None = None

    @property
    def status(self) -> str:
        if self.crit is not None and self.value >= self.crit:
            return "crit"
        if self.warn is not None and self.value >= self.warn:
            return "warn"
        return "ok"

    def push(self, value: float) -> None:
        self.value = value
        self.history.append(value)


def _human_bytes(n: float) -> str:
    for unit, factor in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= factor:
            return f"{n / factor:.1f}{unit}"
    return f"{n:.0f}"


def _human_size(n: float) -> str:
    """Coarse capacity: 3.7T, 510G, 800M - a bar label has no room for more."""
    if n >= 1e12:
        return f"{n / 1e12:.1f}T"
    if n >= 1e9:
        return f"{n / 1e9:.0f}G"
    return f"{n / 1e6:.0f}M"


@dataclass
class Drive:
    """One volume as a usage bar: how full, how much is left, local or not.

    Shaped like a claude_limits.Limit (`label`, `percent`, `reset_text()`) so
    the renderer draws both with the same bar; the slot a limit uses for its
    countdown carries the free space here.
    """

    label: str
    percent: int
    free: float
    remote: bool = False

    def reset_text(self, now=None) -> str:
        return _human_size(self.free)


def _is_real_volume(part) -> bool:
    """Unix: a mounted block device worth a bar (no loop images, no /boot)."""
    if not part.device.startswith("/dev/") or part.device.startswith("/dev/loop"):
        return False
    if part.fstype in ("squashfs", "iso9660", "tmpfs", "devtmpfs", "overlay"):
        return False
    return not part.mountpoint.startswith(("/boot", "/snap", "/var/lib/docker"))


def list_drives() -> list[Drive]:
    """Every mounted volume, local ones first.

    Windows: each drive letter, network drives included (psutil marks them
    `remote`); a drive without a medium (an empty card reader) is skipped.
    Several letters mapped to the same share report identical totals and would
    repeat one bar, so they are merged into one labeled with all its letters
    ("AMSV"). Unix: real block devices plus network filesystems, one bar per
    device.
    """
    found: list[tuple[bool, str, object]] = []
    devices: set[str] = set()
    try:
        partitions = psutil.disk_partitions(all=True)
    except Exception:
        return []
    for part in partitions:
        opts = set((part.opts or "").split(","))
        if WINDOWS:
            if "cdrom" in opts:
                continue
            remote = "remote" in opts
            label = part.mountpoint.rstrip(":\\/") or part.mountpoint
        else:
            remote = part.fstype in _REMOTE_FS
            if not remote and not _is_real_volume(part):
                continue
            if part.device in devices:
                continue
            devices.add(part.device)
            label = os.path.basename(part.mountpoint.rstrip("/")) or "/"
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue  # no medium, or a share that is gone
        if not usage.total:
            continue
        found.append((remote, label, usage))

    drives: list[Drive] = []
    shares: dict[tuple, Drive] = {}
    for remote, label, usage in sorted(found, key=lambda f: (f[0], f[1])):
        key = (usage.total, usage.used)
        if remote and key in shares:
            if WINDOWS:
                shares[key].label += label
            continue
        drive = Drive(label, int(round(usage.used / usage.total * 100)),
                      float(usage.free), remote)
        if remote:
            shares[key] = drive
        drives.append(drive)
    return drives


def _cpu_temperature() -> float | None:
    """CPU package temperature, or None where the platform offers none.

    psutil only ships sensors_temperatures() on Linux and FreeBSD, so the
    attribute itself may be missing.
    """
    probe = getattr(psutil, "sensors_temperatures", None)
    if probe is None:
        return None
    try:
        temps = probe()
    except Exception:
        return None
    for chip in ("coretemp", "k10temp", "zenpower", "acpitz"):
        for sensor in temps.get(chip, []):
            if sensor.label in ("Package id 0", "Tctl", "") or chip == "acpitz":
                return float(sensor.current)
    for entries in temps.values():
        if entries:
            return float(entries[0].current)
    return None


class SystemSource:
    """Collects metrics and keeps their history."""

    def __init__(self):
        self.cores = psutil.cpu_count(logical=True) or 1
        self.hostname = socket.gethostname()
        self._last_net = psutil.net_io_counters()
        self._last_t = time.monotonic()
        self._drives: list[Drive] = []
        self._drives_at = float("-inf")
        self._drives_thread: threading.Thread | None = None
        has_temp = _cpu_temperature() is not None

        self.metrics: dict[str, Metric] = {
            "cpu": Metric("cpu", "CPU", warn=80, crit=95, scale_max=100),
            "ram": Metric("ram", "RAM", warn=85, crit=95, scale_max=100),
            "net_up": Metric("net_up", "NET", arrow="up"),
            "net_down": Metric("net_down", "NET", arrow="down"),
        }
        if HAS_LOADAVG:
            self.metrics["load"] = Metric(
                "load", "LOAD", warn=self.cores, crit=self.cores * 1.5)
        if has_temp:
            temp = Metric("temp", "TEMP", warn=75, crit=88, scale_max=100)
            # Slot temperature in between RAM and network
            ordered = {}
            for k, v in self.metrics.items():
                ordered[k] = v
                if k == "ram":
                    ordered["temp"] = temp
            self.metrics = ordered
        # Fill the sixth slot with disk usage whenever a sensor is missing, so
        # the layout keeps its column count on every platform.
        if not has_temp or not HAS_LOADAVG:
            self.metrics["disk"] = Metric("disk", "DISK", warn=85, crit=95, scale_max=100)

        psutil.cpu_percent(interval=None)  # discard the priming call

    # -- sampling --------------------------------------------------------

    def sample(self) -> dict[str, Metric]:
        m = self.metrics

        cpu = psutil.cpu_percent(interval=None)
        m["cpu"].push(cpu)
        m["cpu"].text = f"{cpu:.0f}"
        m["cpu"].sub = "%"

        vm = psutil.virtual_memory()
        m["ram"].push(vm.percent)
        m["ram"].text = f"{vm.used / 1e9:.1f}"
        m["ram"].sub = f"of {vm.total / 1e9:.0f} GB"

        if "temp" in m:
            t = _cpu_temperature() or 0.0
            m["temp"].push(t)
            m["temp"].text = f"{t:.0f}"
            m["temp"].sub = "°C"

        now = psutil.net_io_counters()
        t_now = time.monotonic()
        dt = max(1e-3, t_now - self._last_t)
        up = (now.bytes_sent - self._last_net.bytes_sent) / dt
        down = (now.bytes_recv - self._last_net.bytes_recv) / dt
        self._last_net, self._last_t = now, t_now
        m["net_up"].push(up)
        m["net_up"].text = _human_bytes(up)
        m["net_up"].sub = "B/s"
        m["net_down"].push(down)
        m["net_down"].text = _human_bytes(down)
        m["net_down"].sub = "B/s"

        if "load" in m:
            load1 = os.getloadavg()[0]
            m["load"].push(load1)
            m["load"].text = f"{load1:.2f}"
            m["load"].sub = f"{self.cores} cores"

        if "disk" in m:
            du = shutil.disk_usage(ROOT_PATH)
            pct = du.used / du.total * 100
            m["disk"].push(pct)
            m["disk"].text = f"{pct:.0f}"
            m["disk"].sub = f"{du.free / 1e9:.0f} GB free"

        return m

    # -- drives ----------------------------------------------------------

    def _refresh_drives(self) -> None:
        self._drives = list_drives()

    def drives(self) -> list[Drive]:
        """Usage of every volume, refreshed off-thread every DRIVES_TTL."""
        now = time.monotonic()
        running = self._drives_thread is not None and self._drives_thread.is_alive()
        if now - self._drives_at >= DRIVES_TTL and not running:
            first = self._drives_thread is None
            self._drives_at = now
            self._drives_thread = threading.Thread(
                target=self._refresh_drives, name="tzmrit-drives", daemon=True)
            self._drives_thread.start()
            if first:
                self._drives_thread.join(DRIVES_FIRST_WAIT)
        return self._drives

    # -- footer ----------------------------------------------------------

    def footer(self) -> list[tuple[str, str]]:
        du = shutil.disk_usage(ROOT_PATH)
        uptime = time.time() - psutil.boot_time()
        days, rem = divmod(int(uptime), 86400)
        hours, minutes = divmod(rem // 60, 60)
        up = f"{days}d {hours}h" if days else f"{hours}h {minutes}m"
        return [
            ("HOST", self.hostname),
            ("DISK", f"{du.free / 1e9:.0f} GB free"),
            ("UPTIME", up),
        ]
