"""Claude Code sessions running on this machine.

Claude Code writes a file `~/.claude/sessions/<pid>.json` for every running
session and keeps `status` current in it. That file is all this module reads -
there is no service to query and no API login involved.

What is NOT visible here:
  * Sessions on other machines (Remote Control goes through the cloud bridge,
    not through a local file).
  * Subagents inside a session - they live in the same process and do not
    surface externally.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

LINUX = sys.platform.startswith("linux")

SESSION_DIR = Path.home() / ".claude" / "sessions"

# Order of urgency: whatever is waiting on a human goes to the top.
STATUS_ORDER = {"requires_action": 0, "busy": 1, "idle": 2}

STATUS_TEXT = {
    "requires_action": "waiting for you",
    "busy": "working",
    "idle": "ready",
    "offline": "offline",
}


def _proc_start(pid: int) -> str | None:
    """Process start time in clock ticks (field 22 of /proc/<pid>/stat).

    Linux only, and that is deliberate: this is the exact value Claude Code
    stores in `procStart`, so the two can be compared byte for byte. Returns
    None where /proc does not exist.

    The process name in field 2 may contain spaces and parentheses, so the
    split happens after the last closing parenthesis.
    """
    if not LINUX:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    try:
        fields = raw[raw.rindex(")") + 2:].split()
        return fields[19]  # field 22 overall, counting past comm and state
    except (ValueError, IndexError):
        return None


def _is_live(pid: int, expected_start: object) -> bool:
    """Is this session's process still the one that wrote the file?

    On Linux `procStart` is compared exactly - that rules out a recycled PID
    being mistaken for a live session.

    Elsewhere the stored value is in an unknown format, so it is ignored and
    the check falls back to: the process exists and still looks like Claude.
    That is weaker against PID reuse, but it never hides a running session,
    which is the failure that would actually matter here.
    """
    if LINUX and expected_start is not None:
        return str(expected_start) == str(_proc_start(pid))
    try:
        proc = psutil.Process(pid)
        name = (proc.name() or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return "claude" in name or "node" in name or "python" in name


# Memory figures move slowly; re-walking every child process once a second
# would be pure waste.
_MEM_TTL = 4.0
_mem_cache: dict[int, tuple[float, int, int]] = {}


def process_memory(pid: int, ttl: float = _MEM_TTL) -> tuple[int, int]:
    """Memory of a session in bytes, including its child processes.

    MCP servers run as children of the session process - measured on a real
    session that is the difference between 493 MB and 814 MB. Leaving them out
    would make the number misleading.

    Returns: (total bytes, number of child processes).
    """
    now = time.monotonic()
    hit = _mem_cache.get(pid)
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    try:
        proc = psutil.Process(pid)
        total = proc.memory_info().rss
        children = proc.children(recursive=True)
        for child in children:
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        result = (total, len(children))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        result = (0, 0)
    _mem_cache[pid] = (now, *result)
    # Don't carry entries for vanished processes forever
    if len(_mem_cache) > 64:
        for dead in [k for k in _mem_cache if not psutil.pid_exists(k)]:
            _mem_cache.pop(dead, None)
    return result


def fmt_memory(num_bytes: float) -> str:
    if num_bytes <= 0:
        return ""
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.1f} GB"
    return f"{num_bytes / 1024 ** 2:.0f} MB"


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    hours, rest = divmod(seconds, 3600)
    if hours < 24:
        return f"{hours}h{rest // 60:02d}m"
    return f"{hours // 24}d{hours % 24}h"


@dataclass
class Session:
    pid: int
    name: str
    cwd: str
    status: str
    kind: str
    session_id: str = ""
    status_since: float = 0.0
    rss: int = 0
    child_count: int = 0

    @property
    def project(self) -> str:
        base = os.path.basename(self.cwd.rstrip("/"))
        return base or self.cwd

    @property
    def waiting(self) -> bool:
        return self.status == "requires_action"

    @property
    def working(self) -> bool:
        return self.status == "busy"

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)

    @property
    def memory_text(self) -> str:
        return fmt_memory(self.rss)

    @property
    def age_text(self) -> str:
        if not self.status_since:
            return ""
        return _fmt_age(time.time() - self.status_since)

    @property
    def sort_key(self) -> tuple:
        return (STATUS_ORDER.get(self.status, 9), -self.status_since)


def list_sessions(directory: Path | None = None) -> list[Session]:
    """Live sessions, most urgent first.

    Files of dead sessions occasionally linger; they are filtered out by PID
    and start time so a recycled PID never shows up as a stale entry.
    """
    directory = directory or SESSION_DIR
    out: list[Session] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return out

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int):
            continue
        if not _is_live(pid, data.get("procStart")):
            continue  # process is gone, or the PID was reassigned
        stamp = data.get("statusUpdatedAt") or data.get("updatedAt") or 0
        out.append(Session(
            pid=pid,
            name=str(data.get("name") or f"pid {pid}"),
            cwd=str(data.get("cwd") or ""),
            status=str(data.get("status") or "idle"),
            kind=str(data.get("kind") or ""),
            session_id=str(data.get("sessionId") or ""),
            status_since=float(stamp) / 1000.0 if stamp else 0.0,
        ))
        out[-1].rss, out[-1].child_count = process_memory(pid)

    out.sort(key=lambda s: s.sort_key)
    return out


def summarize(sessions: list[Session]) -> str:
    """Header line that leads with the number that matters."""
    if not sessions:
        return "no sessions"
    waiting = sum(1 for s in sessions if s.waiting)
    working = sum(1 for s in sessions if s.working)
    parts = [f"{len(sessions)} session" + ("s" if len(sessions) != 1 else "")]
    if working:
        parts.append(f"{working} active")
    if waiting:
        parts.append(f"{waiting} waiting")
    total = sum(s.rss for s in sessions)
    if total:
        parts.append(fmt_memory(total))
    return " · ".join(parts)
