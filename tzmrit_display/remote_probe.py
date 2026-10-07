"""Report this machine's Claude Code sessions as JSON lines on stdout.

This file is not imported by the dashboard - remote_hosts pipes its *source*
into `ssh <host> python3 -u -`, so it runs on the other machine with whatever
python3 happens to be there. That is why it is self-contained: standard library
only, no imports from this package, no syntax newer than Python 3.6. Nothing
has to be installed on a host for it to show up on the panel.

It reads the same two things claude_sessions reads locally - the session files
and the transcript tail - and deliberately repeats that logic instead of
sharing it; the price of needing no install. Memory comes from /proc rather
than psutil, counting child processes the same way (MCP servers are most of a
session's footprint).

The first argument is the email of the Claude account the dashboard itself
uses. When this machine is logged into a different one, the probe also reads
that account's usage here and sends the response along - the OAuth token never
leaves the machine, only the percentages do. Same endpoint and same restraint
as claude_limits: never refresh the token, back off on failure, fail silent.

One line per INTERVAL, forever. The loop ends when stdout breaks, which is what
happens when the ssh connection goes away - no process is left behind.
"""

import glob
import json
import os
import socket
import sys
import time

try:
    import urllib.request as urlrequest
except ImportError:  # pragma: no cover - a python3 without urllib
    urlrequest = None

INTERVAL = 2.0
# A subagent counts as working while its transcript was written this recently
# (claude_sessions.WORKING_ACTIVE_WINDOW).
ACTIVE_WINDOW = 120.0
ACCOUNT_TTL = 600.0
USAGE_TTL = 180.0
USAGE_BACKOFF_CAP = 1800.0
USAGE_URL = "https://api.anthropic.com/api/oauth/usage?at_wall=1&skip_spend=1"
MODEL_TTL = 60.0
MODEL_TAIL = 64 * 1024

HOME = os.path.expanduser("~")
SESSION_DIR = os.path.join(HOME, ".claude", "sessions")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
HAS_PROC = os.path.isdir("/proc/self")

try:
    PAGE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):
    PAGE = 4096


def stat_fields(pid):
    """Fields of /proc/<pid>/stat after the process name, or None.

    The name may contain spaces and parentheses, so the split happens after
    the last closing parenthesis: index 1 is the ppid, index 19 the start time.
    """
    try:
        with open("/proc/%d/stat" % pid) as fh:
            raw = fh.read()
        return raw[raw.rindex(")") + 2:].split()
    except (OSError, ValueError):
        return None


def is_live(pid, expected_start):
    if HAS_PROC:
        fields = stat_fields(pid)
        if fields is None or len(fields) < 20:
            return False
        return expected_start is None or str(expected_start) == fields[19]
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def child_map():
    """ppid -> [pid] for every process, one /proc walk per tick."""
    children = {}
    if not HAS_PROC:
        return children
    try:
        names = os.listdir("/proc")
    except OSError:
        return children
    for name in names:
        if not name.isdigit():
            continue
        fields = stat_fields(int(name))
        if fields and len(fields) > 1:
            try:
                children.setdefault(int(fields[1]), []).append(int(name))
            except ValueError:
                pass
    return children


def rss(pid):
    try:
        with open("/proc/%d/statm" % pid) as fh:
            return int(fh.read().split()[1]) * PAGE
    except (OSError, ValueError, IndexError):
        return 0


def memory(pid, children):
    """(bytes including all descendants, number of descendants)."""
    total, count, todo, seen = rss(pid), 0, list(children.get(pid, ())), {pid}
    while todo:
        child = todo.pop()
        if child in seen:
            continue
        seen.add(child)
        total += rss(child)
        count += 1
        todo.extend(children.get(child, ()))
    return total, count


_transcripts = {}


def transcript(session_id):
    """Path of the session's transcript jsonl, or None. Cached once found."""
    path = _transcripts.get(session_id)
    if path and os.path.exists(path):
        return path
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    if not hits:
        return None
    _transcripts[session_id] = hits[0]
    return hits[0]


def tail_model(path):
    """Model id of the last real assistant turn in the transcript's tail.

    Subagent turns (isSidechain) and Claude Code's own `<synthetic>` lines are
    stepped over, as in claude_sessions._tail_model_id.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - MODEL_TAIL))
            chunk = fh.read()
    except OSError:
        return ""
    lines = chunk.decode("utf-8", "replace").splitlines()
    if size > MODEL_TAIL and lines:
        lines.pop(0)
    for line in reversed(lines):
        if '"model"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and not model.startswith("<"):
            return model
    return ""


_models = {}


def model(session_id, path):
    now = time.time()
    hit = _models.get(session_id)
    if hit and now - hit[0] < MODEL_TTL:
        return hit[1]
    value = tail_model(path) if path else ""
    _models[session_id] = (now, value)
    return value


def subagents(log):
    """(subagents working now, mtime of the latest subagent write)."""
    if not log:
        return 0, 0.0
    count, newest, now = 0, 0.0, time.time()
    for path in glob.glob(os.path.join(log[:-len(".jsonl")], "subagents", "agent-*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        newest = max(newest, mtime)
        if now - mtime < ACTIVE_WINDOW:
            count += 1
    return count, newest


_account = [0.0, {"email": "", "name": ""}]


def account():
    """Email and display name of the account logged in here (no secret)."""
    now = time.time()
    if now - _account[0] < ACCOUNT_TTL:
        return _account[1]
    value = {"email": "", "name": ""}
    try:
        with open(os.path.join(HOME, ".claude.json"), encoding="utf-8") as fh:
            data = json.load(fh).get("oauthAccount")
        if isinstance(data, dict):
            value = {"email": str(data.get("emailAddress") or ""),
                     "name": str(data.get("displayName") or "")}
    except (OSError, ValueError, AttributeError):
        pass
    _account[0], _account[1] = now, value
    return value


def fetch_usage():
    """The raw usage response of this machine's account, or None."""
    if urlrequest is None:
        return None
    try:
        with open(os.path.join(HOME, ".claude", ".credentials.json"), encoding="utf-8") as fh:
            oauth = json.load(fh).get("claudeAiOauth")
        token = oauth.get("accessToken")
        expires = oauth.get("expiresAt")
        if not token or (isinstance(expires, (int, float)) and expires <= time.time() * 1000):
            return None
        req = urlrequest.Request(USAGE_URL, headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
        })
        resp = urlrequest.urlopen(req, timeout=10)
        try:
            data = json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()
        return data if isinstance(data, dict) else None
    except Exception:
        # Offline, 4xx/5xx, malformed JSON: all silent, and the broad catch
        # keeps the token out of any traceback.
        return None


_usage = {"at": None, "value": None, "fails": 0, "good_at": None}


def usage():
    """Cached usage, refreshed every USAGE_TTL, slower after failures.

    A failed refresh keeps the last good value: stale percentages beat a block
    that blinks out for half an hour.
    """
    now = time.time()
    wait = USAGE_TTL
    if _usage["fails"]:
        wait = max(wait, min(USAGE_BACKOFF_CAP, 60.0 * 2 ** (_usage["fails"] - 1)))
    if _usage["at"] is None or now - _usage["at"] >= wait:
        value = fetch_usage()
        _usage["at"] = now
        if value is None:
            _usage["fails"] += 1
        else:
            _usage["fails"] = 0
            _usage["value"] = value
            _usage["good_at"] = now
    return _usage["value"]


def snapshot(viewer_email=""):
    sessions = []
    children = child_map()
    for path in sorted(glob.glob(os.path.join(SESSION_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not is_live(pid, data.get("procStart")):
            continue
        session_id = str(data.get("sessionId") or "")
        log = transcript(session_id) if session_id else None
        try:
            active_at = os.path.getmtime(log) if log else 0.0
        except OSError:
            active_at = 0.0
        agents, agents_at = subagents(log)
        active_at = max(active_at, agents_at)
        stamp = data.get("statusUpdatedAt") or data.get("updatedAt") or 0
        total, count = memory(pid, children)
        sessions.append({
            "pid": pid,
            "name": str(data.get("name") or "pid %d" % pid),
            "cwd": str(data.get("cwd") or ""),
            "status": str(data.get("status") or "idle"),
            "kind": str(data.get("kind") or ""),
            "session_id": session_id,
            "status_since": float(stamp) / 1000.0 if stamp else 0.0,
            "rss": total,
            "child_count": count,
            "active_at": active_at,
            "model": model(session_id, log),
            "subagents": agents,
        })
    out = {"v": 1, "host": socket.gethostname(), "now": time.time(),
           "account": account(), "sessions": sessions}
    email = out["account"]["email"]
    if email and email.lower() != viewer_email.lower():
        out["usage"] = usage()
        # Lets the panel draw a reading that could not be renewed as old.
        out["usage_at"] = _usage.get("good_at")
        out["usage_stale"] = bool(_usage["fails"])
    return out


def main():
    viewer_email = sys.argv[1] if len(sys.argv) > 1 else ""
    while True:
        try:
            sys.stdout.write(json.dumps(snapshot(viewer_email)) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            return  # the ssh connection is gone
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
