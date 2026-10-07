"""Claude Code sessions on other machines, fetched over ssh.

Each configured host gets one long-lived `ssh <host> python3 -u -` with
remote_probe's source on stdin. The probe prints a JSON line every couple of
seconds; a reader thread per host keeps the latest one. The frame loop never
waits on the network - `sessions()` only hands out what is already there.

Nothing is installed on the host: it needs an sshd, a python3 and key based
login (BatchMode - a password prompt would hang a windowless dashboard). The
host name is whatever `ssh` accepts, so aliases from ~/.ssh/config work; write
`label=target` to choose what the panel calls it.

A host logged into a different Claude account than this machine also reports
that account's usage (see remote_probe); `accounts()` hands those out so the
panel can show each account's limits.

A host whose last line is older than STALE counts as offline. Its ssh is
restarted after RETRY seconds for as long as the dashboard runs, so a host that
comes back simply reappears.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .claude_limits import AccountLimits, account_title, parse_usage
from .claude_sessions import Session

log = logging.getLogger("tzmrit_display")

# The probe reports every 2 s; five missed lines is a dead link, not jitter.
STALE = 10.0
RETRY = 5.0

_PROBE = Path(__file__).with_name("remote_probe.py")

# ServerAlive makes ssh itself notice a silently dead link (suspend, pulled
# cable) and exit, which is what triggers the reconnect below.
_SSH_OPTIONS = (
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
)

# Without this every ssh would flash a console window under pythonw.exe.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def parse_hosts(values) -> list[str]:
    """Flatten repeated and comma separated --hosts values, order kept."""
    out: list[str] = []
    for value in values or ():
        for name in str(value).split(","):
            name = name.strip()
            if name and name not in out:
                out.append(name)
    return out


def read_hosts_file(path) -> list[str]:
    """Hosts from a text file: one `[label=]host` per line, `#` starts a comment.

    A missing or unreadable file is an empty list, not an error - the dashboard
    then simply shows this machine alone.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    return parse_hosts(line.split("#", 1)[0] for line in text.splitlines())


def host_label(name: str) -> str:
    """What a session row calls a host when the user gave it no label.

    The machine's short name: `alex@lab.example.org` -> `lab`.
    A row has room for about a dozen characters, so login and domain go. An IP
    address has no domain to drop and is kept whole. Anything else - two
    logins on one machine, say - is what `label=target` is for.
    """
    host = name.rpartition("@")[2]
    if not host.replace(".", "").isdigit() and ":" not in host:
        host = host.split(".")[0] or host
    return host


def to_sessions(host: str, payload: dict, received: float) -> list[Session]:
    """Turn one probe line into Session objects labeled with `host`.

    The timestamps in the payload are on the remote clock. They are shifted by
    the difference between `received` (local) and the payload's own `now`, so
    "working" and the inactivity counter stay right when the clocks disagree.
    """
    try:
        skew = received - float(payload.get("now") or received)
    except (TypeError, ValueError):
        skew = 0.0
    out: list[Session] = []
    for item in payload.get("sessions") or ():
        if not isinstance(item, dict) or not isinstance(item.get("pid"), int):
            continue

        def stamp(key):
            try:
                value = float(item.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
            return value + skew if value else 0.0

        out.append(Session(
            pid=item["pid"],
            name=str(item.get("name") or f"pid {item['pid']}"),
            cwd=str(item.get("cwd") or ""),
            status=str(item.get("status") or "idle"),
            kind=str(item.get("kind") or ""),
            session_id=str(item.get("session_id") or ""),
            status_since=stamp("status_since"),
            rss=int(item.get("rss") or 0),
            child_count=int(item.get("child_count") or 0),
            active_at=stamp("active_at"),
            model=str(item.get("model") or ""),
            host=host,
            subagents=int(item.get("subagents") or 0),
        ))
    return out


def _last_line(stream) -> str:
    """The last non-empty line ssh wrote to stderr, shortened for the log."""
    try:
        stream.seek(0)
        lines = stream.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError):
        return ""
    for line in reversed(lines):
        if line.strip():
            return line.strip()[:200]
    return ""


class RemoteHost:
    """One host: the ssh child, its reader thread and the latest payload."""

    def __init__(self, name: str, ssh: str = "ssh", viewer_email: str = ""):
        # `label=target` names the host on the panel; the target alone gets
        # its short machine name.
        label, named, target = name.partition("=")
        self.name = target if named and target else name
        self.label = label.strip() if named and label.strip() else host_label(self.name)
        self._ssh = ssh
        # Shell-safe by construction: anything but the characters of an email
        # address is dropped before it becomes part of the remote command.
        self._viewer = "".join(c for c in viewer_email if c.isalnum() or c in "@._+-")
        self._lock = threading.Lock()
        self._payload: dict | None = None
        self._received = 0.0   # time.time() of the latest line
        self._seen = 0.0       # time.monotonic() of the latest line
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"tzmrit-ssh-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def feed(self, line: str) -> None:
        """Take one line of probe output; anything that is not a payload
        (a login banner, a shell complaint) is ignored."""
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(payload, dict) or "sessions" not in payload:
            return
        with self._lock:
            self._payload = payload
            self._received = time.time()
            self._seen = time.monotonic()

    def sessions(self) -> list[Session] | None:
        """Sessions from the latest line, or None while the host is offline."""
        with self._lock:
            payload, received, seen = self._payload, self._received, self._seen
        if payload is None or time.monotonic() - seen > STALE:
            return None
        return to_sessions(self.label, payload, received)

    def account(self) -> tuple[str, AccountLimits] | None:
        """(email, limits) when this host runs under a foreign account.

        Answered from the last line ever received, stale or not: an account
        block that vanished whenever its host hiccups would make the whole
        utility column jump, and hours-scale percentages age gracefully.
        """
        with self._lock:
            payload, received, seen = self._payload, self._received, self._seen
        if payload is None or "usage" not in payload:
            return None
        account = payload.get("account")
        if not isinstance(account, dict) or not account.get("email"):
            return None
        email = str(account["email"])
        title = account_title(email, str(account.get("name") or ""))
        limits = parse_usage(payload.get("usage"))
        if limits is not None:
            # Old when the host could not renew it, or when the host itself
            # has gone quiet and nothing newer can arrive.
            limits.stale = (bool(payload.get("usage_stale"))
                            or time.monotonic() - seen > STALE)
            taken, sent = payload.get("usage_at"), payload.get("now")
            if isinstance(taken, (int, float)) and isinstance(sent, (int, float)):
                limits.as_of = received - (sent - taken)  # in this clock
        return email, AccountLimits(title, limits)

    @property
    def reported(self) -> bool:
        """Has the host answered at least once, or already failed once?"""
        return self._payload is not None or self._failed

    _failed = False

    def _run(self) -> None:
        script = _PROBE.read_bytes()
        complaint = None   # the last failure logged, so a dead host says it once
        while not self._stop.is_set():
            # ssh's own words are the only clue to a refused key or an unknown
            # host; a file, because an undrained pipe would block the child.
            errors = tempfile.TemporaryFile()
            try:
                proc = subprocess.Popen(
                    [self._ssh, *_SSH_OPTIONS, self.name,
                     f"python3 -u - {self._viewer}".rstrip()],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=errors, creationflags=_NO_WINDOW)
            except OSError as exc:
                errors.close()
                log.warning("host %s: cannot start ssh: %s", self.name, exc)
                self._failed = True
                self._stop.wait(RETRY * 6)
                continue
            self._proc = proc
            try:
                proc.stdin.write(script)
                proc.stdin.close()  # python3 - runs the script at EOF
                connected = False
                for raw in proc.stdout:
                    self.feed(raw.decode("utf-8", "replace"))
                    if not connected and self._payload is not None:
                        connected = True
                        complaint = None
                        log.info("host %s: connected", self.name)
            except OSError:
                pass
            finally:
                if proc.poll() is None:
                    proc.terminate()
                rc = proc.wait()
                proc.stdout.close()
                reason = _last_line(errors)
                errors.close()
            self._failed = True
            if not self._stop.is_set():
                said = f"ssh ended (rc {rc})" + (f": {reason}" if reason else "")
                if said != complaint:
                    complaint = said
                    log.warning("host %s: %s - retrying every %.0f s",
                                self.name, said, RETRY)
                self._stop.wait(RETRY)


class RemoteHosts:
    """The configured hosts as one source for the frame loop."""

    def __init__(self, names, ssh: str = "ssh", viewer_email: str = ""):
        self.hosts = [RemoteHost(name, ssh, viewer_email) for name in names]

    def accounts(self) -> list[AccountLimits]:
        """Limits of every foreign account seen, one entry per account."""
        seen: dict[str, AccountLimits] = {}
        for host in self.hosts:
            found = host.account()
            if found is None:
                continue
            email, limits = found
            # Two hosts on one account: keep whichever actually has data.
            if email.lower() not in seen or seen[email.lower()].limits is None:
                seen[email.lower()] = limits
        return list(seen.values())

    def start(self) -> None:
        for host in self.hosts:
            host.start()

    def stop(self) -> None:
        for host in self.hosts:
            host.stop()

    def wait_first(self, timeout: float) -> None:
        """Block until every host has answered or failed once (for preview)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not all(h.reported for h in self.hosts):
            time.sleep(0.1)

    def collect(self) -> tuple[list[Session], list[str]]:
        """(sessions of all reachable hosts, names of the offline ones)."""
        sessions: list[Session] = []
        offline: list[str] = []
        for host in self.hosts:
            found = host.sessions()
            if found is None:
                offline.append(host.label)
            else:
                sessions.extend(found)
        return sessions, offline
