"""System metrics with a short history for the sparklines."""

from __future__ import annotations

import os
import shutil
import socket
import time
from collections import deque
from dataclasses import dataclass, field

import psutil

HISTORY = 90  # samples per metric


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


def _cpu_temperature() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
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
        has_temp = _cpu_temperature() is not None

        self.metrics: dict[str, Metric] = {
            "cpu": Metric("cpu", "CPU", warn=80, crit=95, scale_max=100),
            "ram": Metric("ram", "RAM", warn=85, crit=95, scale_max=100),
            "net_up": Metric("net_up", "NET", arrow="up"),
            "net_down": Metric("net_down", "NET", arrow="down"),
            "load": Metric("load", "LOAD", warn=self.cores, crit=self.cores * 1.5),
        }
        if has_temp:
            temp = Metric("temp", "TEMP", warn=75, crit=88, scale_max=100)
            # Slot temperature in between RAM and network
            ordered = {}
            for k, v in self.metrics.items():
                ordered[k] = v
                if k == "ram":
                    ordered["temp"] = temp
            self.metrics = ordered
        else:
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

        load1 = os.getloadavg()[0]
        m["load"].push(load1)
        m["load"].text = f"{load1:.2f}"
        m["load"].sub = f"{self.cores} cores"

        if "disk" in m:
            du = shutil.disk_usage("/")
            pct = du.used / du.total * 100
            m["disk"].push(pct)
            m["disk"].text = f"{pct:.0f}"
            m["disk"].sub = f"{du.free / 1e9:.0f} GB free"

        return m

    # -- footer ----------------------------------------------------------

    def footer(self) -> list[tuple[str, str]]:
        du = shutil.disk_usage("/")
        uptime = time.time() - psutil.boot_time()
        days, rem = divmod(int(uptime), 86400)
        hours, minutes = divmod(rem // 60, 60)
        up = f"{days}d {hours}h" if days else f"{hours}h {minutes}m"
        return [
            ("HOST", self.hostname),
            ("ROOT", f"{du.free / 1e9:.0f} GB free"),
            ("UPTIME", up),
        ]
