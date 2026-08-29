"""Cross-process coordination for `run`, `stop` and dashboard takeover.

Cooperative and file-based, not kill-based: a windowed Windows process has no
clean SIGTERM to receive (TerminateProcess would skip the panel cleanup), and
on Linux hard-killing the systemd-managed instance just triggers
Restart=on-failure. So the running instance records its PID here, and `stop` -
or a second `run` taking over the panel - asks it to exit by writing a
stop-request sentinel that the run loop polls. The loop then exits exactly
like on Ctrl+C (including --blank-on-exit) and returns 0, which
Restart=on-failure does not restart.

Two files in a per-user runtime directory:

    run.pid       PID of the running dashboard. Removed on exit, but only by
                  the process that wrote it, so a successor's claim survives
                  a slow predecessor's cleanup.
    stop-request  shutdown request. Contains the target PID; empty or
                  unparsable content addresses whichever instance sees it
                  first. The instance that honors it deletes the file as the
                  acknowledgement.

Liveness is existence-based (psutil.pid_exists) - the same trade-off as
claude_sessions._is_live's fallback: a recycled PID can briefly look alive,
which at worst makes `stop` or a takeover wait out its bounded timeout.
"""

from __future__ import annotations

import os
from pathlib import Path

import psutil

PID_FILE = "run.pid"
STOP_FILE = "stop-request"

# Upper bound for waiting on a requested shutdown. The run loop notices a
# request within ~0.2 s; the rest covers closing the panel (keepalive join,
# optional blank, serial close).
STOP_WAIT = 6.0


def runtime_dir() -> Path:
    """Per-user writable directory for runtime state; created on first use.

    Deliberately NOT the install dir: on Windows the app lives under
    %LOCALAPPDATA%\\Programs, state goes next to it under %LOCALAPPDATA%.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_RUNTIME_DIR")
                    or os.environ.get("XDG_STATE_HOME")
                    or Path.home() / ".local" / "state")
    path = base / "tzmrit-display"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_alive(pid: int) -> bool:
    return psutil.pid_exists(pid)


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_instance() -> int | None:
    """PID of the recorded running dashboard, or None.

    A stale record (crashed instance) is removed on sight so it never blocks
    a later start or misdirects a stop.
    """
    path = runtime_dir() / PID_FILE
    pid = _read_pid(path)
    if pid is None or pid == os.getpid():
        return None
    if not pid_alive(pid):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return pid


def claim_instance() -> None:
    (runtime_dir() / PID_FILE).write_text(str(os.getpid()))


def release_instance() -> None:
    """Drop the claim - but only if it is still ours (see module docstring)."""
    path = runtime_dir() / PID_FILE
    if _read_pid(path) == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


def request_stop(target_pid: int | None = None) -> None:
    content = "" if target_pid is None else str(target_pid)
    (runtime_dir() / STOP_FILE).write_text(content)


def consume_stop_request() -> bool:
    """True if a stop request addressed to this process (or to anyone) is
    pending; consuming it removes the file as the acknowledgement. A request
    addressed to another live instance is left for that instance."""
    path = runtime_dir() / STOP_FILE
    try:
        raw = path.read_text().strip()
    except OSError:
        return False
    if raw.isdigit() and int(raw) != os.getpid():
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


def clear_stale_stop_request() -> None:
    """Drop a leftover request whose target is gone (crashed before the ack),
    so it cannot instantly kill the next dashboard to start."""
    path = runtime_dir() / STOP_FILE
    try:
        raw = path.read_text().strip()
    except OSError:
        return
    if raw.isdigit() and int(raw) != os.getpid() and pid_alive(int(raw)):
        return  # addressed to a live instance; let it acknowledge
    try:
        path.unlink()
    except OSError:
        pass
