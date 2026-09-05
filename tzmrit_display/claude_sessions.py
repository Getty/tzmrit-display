"""Claude Code sessions running on this machine.

Claude Code writes a file `~/.claude/sessions/<pid>.json` for every running
session and keeps `status` current in it. That file plus the session's own
transcript jsonl are all this module reads - there is no service to query and
no API login involved. The transcript answers the two questions the session
file does not: when the last LLM turn happened (_transcript_mtime) and which
model ran it (_transcript_model).

What is NOT visible here:
  * Sessions on other machines (Remote Control goes through the cloud bridge,
    not through a local file).
  * Subagents inside a session - they live in the same process and do not
    surface externally.
"""

from __future__ import annotations

import json
import os
import re
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
# mtime is the "last LLM activity" clock (see _transcript_mtime) and its
# assistant lines name the model in use (see _transcript_model).
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# The waiting-for-a-human state has two spellings on the wire: Claude Code
# >= 2.1.251 writes "waiting", older versions wrote "requires_action". Both mean
# the same thing here. This set is the single source of truth - the sort order,
# the status label and Session.waiting all derive from it, so a third spelling
# only ever has to be added in one place.
WAITING_STATUSES = frozenset({"requires_action", "waiting"})

# Order of urgency: whatever is waiting on a human goes to the top (0).
STATUS_ORDER = {s: 0 for s in WAITING_STATUSES} | {"busy": 1, "idle": 2}

STATUS_TEXT = {s: "waiting for you" for s in WAITING_STATUSES} | {
    "busy": "working",
    "idle": "ready",
    "offline": "offline",
}

# A busy session counts as *working* only while its transcript (the real
# LLM-activity clock, see _transcript_mtime) is fresh. Claude Code can leave
# `status: "busy"` frozen in the session file long after the turn ended, so the
# status field alone lies. The window is generous on purpose: the transcript is
# appended per LLM turn, and a single long turn (heavy thinking + a long
# generation) writes nothing meanwhile, so a tight threshold would flicker a
# genuinely-working session to hollow mid-turn. 120s clears any realistic turn
# while still catching the frozen-status bug (stale by many minutes to hours).
# Bias is deliberate: a false "working" is a minor annoyance, a false "idle" on
# a session that is actually crunching is the worse error.
WORKING_ACTIVE_WINDOW = 120.0


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


# The model in use is only in the transcript, never in the session file. It
# changes at most when someone types /model, so the TTL is generous - a minute
# of staleness costs nothing, re-reading every frame would cost a file seek per
# session per frame.
_MODEL_TTL = 60.0
# How much of the transcript tail to read. Transcripts run to tens of MB and
# this sits in the frame loop, so the file is never read whole. Measured across
# the 40 largest transcripts on this machine the last `"model"` sat at most
# ~8 KB from EOF, so 64 KB is roughly eight times the observed worst case.
_MODEL_TAIL = 64 * 1024
_model_cache: dict[str, tuple[float, str]] = {}

# A dated snapshot suffix (claude-haiku-4-5-20251001): not part of the version.
_SNAPSHOT_DATE = re.compile(r"^\d{6,8}$")


def _model_label(model_id: str) -> str:
    """Short readable form of a model id: `claude-opus-5` -> `opus 5`.

    The `claude-` vendor prefix says nothing on a panel that only ever shows
    Claude Code sessions, and the dashes read as one long token, so the family
    and its dotted version are separated: `claude-haiku-4-5-20251001` ->
    `haiku 4.5`, `claude-fable-5-1` -> `fable 5.1`.

    Anything that does not fit that shape (a bare alias like `sonnet`, or a
    third-party id arriving through a proxy) is passed through de-prefixed
    rather than mangled - unknown is not a reason to show nothing, and the row
    truncates over-long text anyway.
    """
    raw = (model_id or "").strip()
    if not raw:
        return ""
    name = raw[len("claude-"):] if raw.startswith("claude-") else raw
    parts = name.split("-")
    family, version = parts[0], parts[1:]
    if version and _SNAPSHOT_DATE.match(version[-1]):
        version = version[:-1]
    if family and version and all(p.isdigit() for p in version):
        return f"{family} {'.'.join(version)}"
    return "-".join([family] + version) if family else raw


def _tail_model_id(path: Path, tail: int) -> str:
    """Model id of the last real assistant turn in a transcript's tail.

    Only the last `tail` bytes are read and they are scanned backwards, so the
    cost does not grow with the transcript. Two kinds of line are stepped over:

      * `isSidechain: true` - a subagent turn, which often runs a smaller model
        and must not be mistaken for the session's own. (Current Claude Code
        writes those to `<sessionId>/subagents/agent-*.jsonl` instead, which
        this glob never matches; older versions inlined them here.)
      * `<synthetic>` - Claude Code's placeholder on lines it wrote itself
        ("No response requested."), not a model that ever ran.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail))
            chunk = fh.read()
    except OSError:
        return ""
    lines = chunk.decode("utf-8", "replace").splitlines()
    if size > tail and lines:
        lines.pop(0)  # the seek landed mid-record; that fragment is not JSON
    for line in reversed(lines):
        if '"model"' not in line:
            continue  # cheap gate: most lines are user turns and tool results
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and not model.startswith("<"):
            return model
    return ""


def _transcript_model(session_id: str, ttl: float = _MODEL_TTL,
                      projects: Path | None = None, tail: int = _MODEL_TAIL) -> str:
    """Raw model id the session last ran, or "" when it cannot be determined.

    Same lookup as _transcript_mtime - glob by sessionId rather than rebuilding
    the cwd path encoding - because the session file itself carries no model
    field at all. No transcript, no hit in the tail, or an IO error all give ""
    so the row simply shows what it showed before.
    """
    if not session_id:
        return ""
    now = time.monotonic()
    hit = _model_cache.get(session_id)
    if hit and now - hit[0] < ttl:
        return hit[1]
    projects = projects or PROJECTS_DIR
    model = ""
    try:
        for path in sorted(projects.glob(f"*/{session_id}.jsonl")):
            model = _tail_model_id(path, tail)
            if model:
                break
    except OSError:
        model = ""
    _model_cache[session_id] = (now, model)
    # Don't let entries for ended sessions accumulate forever
    if len(_model_cache) > 64:
        for dead in [k for k, v in _model_cache.items() if now - v[0] > 5 * ttl]:
            _model_cache.pop(dead, None)
    return model


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
    model: str = ""

    @property
    def project(self) -> str:
        base = os.path.basename(self.cwd.rstrip("/"))
        return base or self.cwd

    @property
    def waiting(self) -> bool:
        return self.status in WAITING_STATUSES

    @property
    def working(self) -> bool:
        """Busy *and* actually active right now.

        `status == "busy"` is necessary but not sufficient: Claude Code can
        leave that field frozen after a turn ends. The transcript mtime
        (`active_at`) is the honest clock, so a busy session is working only
        while that clock is within WORKING_ACTIVE_WINDOW. When there is no
        transcript file at all (`active_at == 0.0`, e.g. a just-started
        session) there is no reliable clock, so we trust the status rather than
        hide fresh work - note this deliberately does NOT fall back to
        status_since, which is exactly the stale value the gate defends against.
        `waiting` is never gated: a session waiting for a human isn't writing
        the transcript by definition and must still surface.
        """
        if self.status != "busy":
            return False
        if not self.active_at:
            return True
        return (time.time() - self.active_at) < WORKING_ACTIVE_WINDOW

    @property
    def status_text(self) -> str:
        # Keep the word coherent with the dot: a stale-busy row draws the hollow
        # idle dot, so it must not read "working". It falls back to the idle
        # text ("ready"); the left gutter already shows how long it has been
        # inactive, so the age is not double-encoded here.
        if self.status == "busy" and not self.working:
            return STATUS_TEXT["idle"]
        return STATUS_TEXT.get(self.status, self.status)

    @property
    def memory_text(self) -> str:
        return fmt_memory(self.rss)

    @property
    def model_text(self) -> str:
        return _model_label(self.model)

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
        out[-1].model = _transcript_model(out[-1].session_id)

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
