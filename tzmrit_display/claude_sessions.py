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
from datetime import datetime
from pathlib import Path

import psutil

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"

# procStart on Windows holds .NET DateTime ticks: 100 ns units since
# 0001-01-01, in local time. Verified against a live session - the value
# matches psutil's create_time to the microsecond.
_TICKS_PER_SECOND = 10 ** 7
_UNIX_EPOCH_SECONDS = 62_135_596_800  # 0001-01-01 .. 1970-01-01

SESSION_DIR = Path.home() / ".claude" / "sessions"
# Per-session transcript lives here as <cwd-encoded>/<sessionId>.jsonl. Its
# mtime is the "last LLM activity" clock (see _transcript_mtime).
PROJECTS_DIR = Path.home() / ".claude" / "projects"

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


def _windows_start_ticks(pid: int) -> tuple[int, int] | None:
    """Process start time as .NET ticks, in both local and UTC reading.

    Claude Code stores the local-time value; the UTC variant is accepted as
    well so a future change of the writer does not make every session vanish.
    """
    try:
        created = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    local = datetime.fromtimestamp(created)
    local_ticks = int((local - datetime(1, 1, 1)).total_seconds() * _TICKS_PER_SECOND)
    utc_ticks = int((created + _UNIX_EPOCH_SECONDS) * _TICKS_PER_SECOND)
    return local_ticks, utc_ticks


def _is_live(pid: int, expected_start: object) -> bool:
    """Is this session's process still the one that wrote the file?

    On Linux `procStart` is compared exactly, on Windows as .NET ticks with a
    small tolerance - both rule out a recycled PID being mistaken for a live
    session.

    Elsewhere (or when the value does not parse) the stored value is ignored
    and the check falls back to existence: a live process is live, a dead pid
    is not. That is weaker against PID reuse, but it never hides a running
    session, which is the failure that would actually matter here.
    """
    if LINUX and expected_start is not None:
        return str(expected_start) == str(_proc_start(pid))
    if WINDOWS and expected_start is not None:
        try:
            expected = int(str(expected_start))
        except (TypeError, ValueError):
            expected = None
        if expected is not None:
            ticks = _windows_start_ticks(pid)
            if ticks is None:
                return False
            return any(abs(expected - t) <= 2 * _TICKS_PER_SECOND for t in ticks)
    try:
        proc = psutil.Process(pid)
        return proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


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


def _fmt_inactive(seconds: float) -> str:
    """Single-unit 'time since last LLM activity': 3s, 6m, 1h.

    Deliberately one unit only (not _fmt_age's two): this is a live-updating
    counter that resets to a few seconds on every turn, so the coarse unit is
    all that reads at a glance.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


# The transcript's mtime is the last-LLM-activity clock; globbing every session
# every frame is wasteful, so the resolved mtime is cached briefly.
_ACTIVE_TTL = 2.0
_active_cache: dict[str, tuple[float, float | None]] = {}


def _transcript_mtime(session_id: str, ttl: float = _ACTIVE_TTL,
                      projects: Path | None = None) -> float | None:
    """Epoch mtime of the session's transcript jsonl, or None if not found.

    The transcript lives at `~/.claude/projects/<cwd-encoded>/<sessionId>.jsonl`;
    the directory is the cwd path-encoded, so rather than reconstruct that
    encoding we glob by sessionId, which is unique. The file is appended on
    every LLM turn, so its mtime is ~1 s old for a busy session and grows while
    it sits idle - exactly the "time since last activity" the counter wants,
    and unlike statusUpdatedAt it does not freeze during a long busy stretch.

    Caveat: a non-LLM write (transcript compaction, for one) also bumps the
    mtime, so this is a close proxy rather than an exact last-turn timestamp.
    """
    if not session_id:
        return None
    now = time.monotonic()
    hit = _active_cache.get(session_id)
    if hit and now - hit[0] < ttl:
        return hit[1]
    projects = projects or PROJECTS_DIR
    mtime: float | None = None
    try:
        for path in projects.glob(f"*/{session_id}.jsonl"):
            try:
                m = path.stat().st_mtime
            except OSError:
                continue
            if mtime is None or m > mtime:
                mtime = m
    except OSError:
        mtime = None
    _active_cache[session_id] = (now, mtime)
    # Don't let entries for ended sessions accumulate forever
    if len(_active_cache) > 64:
        for dead in [k for k, v in _active_cache.items() if now - v[0] > 60]:
            _active_cache.pop(dead, None)
    return mtime


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
    active_at: float = 0.0

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
    def inactive_seconds(self) -> float:
        """Seconds since the last LLM activity (transcript write).

        Falls back to status_since when no transcript file was located.
        """
        ref = self.active_at or self.status_since
        if not ref:
            return 0.0
        return max(0.0, time.time() - ref)

    @property
    def inactive_text(self) -> str:
        return _fmt_inactive(self.inactive_seconds)

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
        out[-1].active_at = _transcript_mtime(out[-1].session_id) or 0.0

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
