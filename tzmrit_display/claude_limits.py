"""Claude account rate-limit budget (session = 5h, weekly = 7d).

Claude Code stores an OAuth access token in `~/.claude/.credentials.json`
(`claudeAiOauth.accessToken`, with an `expiresAt` epoch-ms sibling). With it,
the account's usage against its rate limits can be read from a single endpoint:

    GET https://api.anthropic.com/api/oauth/usage?at_wall=1&skip_spend=1
    Authorization: Bearer <token>
    anthropic-beta: oauth-2025-04-20

The response carries a `limits[]` array of typed windows plus flat
`five_hour`/`seven_day` buckets; the array is preferred, the buckets are the
fallback. This module keeps the network and the parsing strictly apart:
`parse_usage()` is pure (a dict in, a `Limits` out) so tests run against a
captured fixture, and `fetch()` is the only thing that touches the wire.

Two hard rules, because this runs inside a ~1 fps render loop:

  * The fetch never runs per frame. `get_limits(ttl)` returns the cached value
    immediately and refreshes in a background thread at most once per effective
    interval, never on the render thread. The caller supplies the TTL from how
    active the machine is (usage moves over hours, so a quiet board is polled
    rarely); on top of that, a run of failed refreshes backs the interval off
    exponentially (see `_backoff_interval`) so a persistent error cannot turn
    into one request per TTL forever.
  * Everything fails silent. A missing or expired token, an offline host, a
    non-200, malformed JSON -- all return None. The panel keeps running and
    simply renders nothing here. We never refresh the OAuth token, and we
    never log or otherwise expose it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage?at_wall=1&skip_spend=1"
_BETA = "oauth-2025-04-20"

# The session window is 5 hours, the weekly (all models) window is 7 days.
# `weekly_scoped` is a third, model-scoped weekly window the account may carry
# (one per scoped model, so there can be more than one).
_SESSION_KIND = "session"
_WEEKLY_KIND = "weekly_all"
_SCOPED_KIND = "weekly_scoped"

# Every window on this panel is a Claude window and the neighbouring bars read
# "Session" / "Weekly", so the vendor word in a scope display name ("Claude
# Opus") is the one part of the label carrying no information.
_VENDOR_PREFIX = "Claude "


@dataclass
class Limit:
    """One rate-limit window: how full it is and when it resets."""

    label: str
    percent: int
    severity: str = "normal"
    resets_at: datetime | None = None

    def reset_text(self, now: datetime | None = None) -> str:
        """Human 'time until reset', locale-independent (e.g. '1h20m', '2d')."""
        if self.resets_at is None:
            return ""
        now = now or datetime.now(timezone.utc)
        return _fmt_reset((self.resets_at - now).total_seconds())


@dataclass
class Limits:
    session: Limit | None = None
    weekly: Limit | None = None
    scoped: list[Limit] = field(default_factory=list)
    # A reading that could not be renewed is kept and marked instead of being
    # dropped: bars that blink out on every failed poll say less than old ones
    # drawn as old. `as_of` is the wall-clock time the reading was taken.
    stale: bool = False
    as_of: float | None = None

    def age_text(self, now: float | None = None) -> str:
        """How old the reading is ('12m', '2h05m'); '' when unknown."""
        if self.as_of is None:
            return ""
        age = (time.time() if now is None else now) - self.as_of
        return "<1m" if age < 60 else _fmt_reset(age)

    @property
    def rows(self) -> list[Limit]:
        """The bars to draw, widest window last: session, weekly, then scoped.

        The order is the reading order on the panel and is deliberate - the
        5h window is the one that bites first, the model-scoped weeklies are
        the specialisation, so they close the row.
        """
        return [x for x in (self.session, self.weekly) if x is not None] + list(self.scoped)


@dataclass
class AccountLimits:
    """The limits of one Claude account, titled with who it belongs to.

    One board can show sessions of several accounts (remote_hosts); each then
    gets its own block of bars. `limits` is None while nothing could be read -
    the block keeps its title so the layout does not jump.
    """

    name: str
    limits: Limits | None = None


_ACCOUNT_TTL = 600.0
_account: dict[str, object] = {"at": float("-inf"), "value": ("", "")}


def local_account(path: Path | None = None) -> tuple[str, str]:
    """(email, display name) of the account Claude Code is logged into here.

    Read from `~/.claude.json` (`oauthAccount`), which holds no secret. Both
    are "" when it cannot be read. Cached: the file is large and the answer
    changes only on /login.
    """
    now = time.monotonic()
    if path is None and now - float(_account["at"]) < _ACCOUNT_TTL:
        return _account["value"]  # type: ignore[return-value]
    value = ("", "")
    try:
        data = json.loads((path or Path.home() / ".claude.json").read_text(encoding="utf-8"))
        account = data.get("oauthAccount")
        if isinstance(account, dict):
            value = (str(account.get("emailAddress") or ""),
                     str(account.get("displayName") or ""))
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    if path is None:
        _account["at"], _account["value"] = now, value
    return value


def account_title(email: str, name: str) -> str:
    """Short block title for an account: its display name, else the mailbox."""
    return (name or email.split("@")[0] or "Limits").strip().upper()


# -- parsing (pure) ------------------------------------------------------

def parse_usage(data: object) -> Limits | None:
    """Turn a usage response into Limits, or None if nothing is usable.

    Prefers the typed `limits[]` array; falls back to the flat
    `five_hour`/`seven_day` buckets for either window independently. The
    model-scoped weeklies come only from the array - the flat buckets have no
    equivalent - and there may be none, one or several of them.
    """
    if not isinstance(data, dict):
        return None
    session = _from_limits(data, _SESSION_KIND) or _from_bucket(data.get("five_hour"))
    weekly = _from_limits(data, _WEEKLY_KIND) or _from_bucket(data.get("seven_day"))
    scoped = _scoped_limits(data)
    if session is not None:
        session.label = "Session"
    if weekly is not None:
        weekly.label = "Weekly"
    if session is None and weekly is None and not scoped:
        return None
    return Limits(session=session, weekly=weekly, scoped=scoped)


def _from_limits(data: dict, kind: str) -> Limit | None:
    limits = data.get("limits")
    if not isinstance(limits, list):
        return None
    for item in limits:
        if not isinstance(item, dict) or item.get("kind") != kind:
            continue
        return Limit(
            label=kind,
            percent=_as_percent(item.get("percent")),
            severity=str(item.get("severity") or "normal"),
            resets_at=_parse_iso(item.get("resets_at")),
        )
    return None


def _scoped_limits(data: dict) -> list[Limit]:
    """Every model-scoped weekly window in the payload, in payload order."""
    limits = data.get("limits")
    if not isinstance(limits, list):
        return []
    return [
        Limit(
            label=_scope_label(item),
            percent=_as_percent(item.get("percent")),
            severity=str(item.get("severity") or "normal"),
            resets_at=_parse_iso(item.get("resets_at")),
        )
        for item in limits
        if isinstance(item, dict) and item.get("kind") == _SCOPED_KIND
    ]


def _scope_label(item: dict) -> str:
    """Bar label for a model-scoped window, read off the payload.

    Never a hardcoded model name: which model an account's weekly is scoped to
    moves with the plan (it was Opus, it is Fable here), so the name comes from
    `scope.model.display_name`, with the redundant vendor prefix stripped
    ("Claude Opus" -> "Opus"). Falls back to the `group` slug, and finally to a
    generic word - a bar with an empty label reads as a bug, not as a bar.
    """
    scope = item.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str) and name.strip():
        name = name.strip()
        if name.startswith(_VENDOR_PREFIX) and len(name) > len(_VENDOR_PREFIX):
            name = name[len(_VENDOR_PREFIX):].strip()
        if name:
            return name
    group = item.get("group")
    if isinstance(group, str) and group.strip():
        group = group.strip()
        # Only lift the first letter: .capitalize() would flatten "MiniMax".
        return group[:1].upper() + group[1:]
    return "Model"


def _from_bucket(bucket: object) -> Limit | None:
    if not isinstance(bucket, dict) or "utilization" not in bucket:
        return None
    return Limit(
        label="",
        percent=_as_percent(bucket.get("utilization")),
        severity="normal",
        resets_at=_parse_iso(bucket.get("resets_at")),
    )


def _as_percent(value: object) -> int:
    try:
        return int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fmt_reset(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, rest = divmod(seconds, 3600)
        return f"{hours}h{rest // 60:02d}m"
    days, rest = divmod(seconds, 86400)
    return f"{days}d {rest // 3600}h"


# -- fetching (network) --------------------------------------------------

def _read_token(now_ms: float | None = None) -> str | None:
    """The OAuth access token, or None if absent or already expired.

    We never refresh: an expired token is treated as no token. The value is
    returned only, never logged.
    """
    try:
        data = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not token or not isinstance(token, str):
        return None
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)):
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        if expires <= now_ms:
            return None
    return token


def fetch() -> Limits | None:
    """Read the account usage from the API. None on any failure."""
    token = _read_token()
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _BETA,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        # Offline, DNS, TLS, 4xx/5xx, malformed JSON -- all fail silent, and
        # the broad catch also keeps a token out of any propagating traceback.
        return None
    limits = parse_usage(payload)
    if limits is not None:
        _remember(payload)
    return limits


# -- the last reading, kept across restarts ------------------------------
#
# A freshly started dashboard has nothing to show until its first fetch lands,
# and a restart is exactly when the endpoint tends to answer 429. The last good
# response is therefore kept on disk and shown, marked stale, until a new one
# arrives. It holds percentages and reset times, never the token.

_STORE_NAME = "limits.json"
_STORE_MAX_AGE = 7 * 86400.0   # every window has reset by then: nothing to show


def _store_path() -> Path:
    from .runtime import runtime_dir
    return runtime_dir() / _STORE_NAME


def _remember(payload: object) -> None:
    try:
        _store_path().write_text(
            json.dumps({"at": time.time(), "usage": payload}), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def _recall(now: float | None = None) -> Limits | None:
    """The last stored reading as stale Limits, or None."""
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        taken = float(data["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    now = time.time() if now is None else now
    if not 0 <= now - taken <= _STORE_MAX_AGE:
        return None
    limits = parse_usage(data.get("usage"))
    if limits is not None:
        limits.stale, limits.as_of = True, taken
    return limits


# -- cached, off-thread access ------------------------------------------

_TTL = 60.0
# Exponential backoff after failed refreshes. The base doubles each consecutive
# failure up to the cap, so a persistent error settles to one request per cap.
_BACKOFF_BASE = 60.0     # first extra spacing after one failure
_BACKOFF_CAP = 1800.0    # 30 min ceiling (n>=7 sits here)
_lock = threading.Lock()
# "Never fetched". Not 0.0: time.monotonic() counts from boot, so for the first
# TTL seconds after a (re)boot 0.0 would look fresh and suppress the fetch.
_NEVER = float("-inf")
_cache: dict[str, object] = {"at": _NEVER, "value": None}
_fetching = False
_fail_count = 0          # consecutive refreshes that returned None


def _backoff_interval(fail_count: int) -> float:
    """Extra minimum spacing (seconds) after `fail_count` failed refreshes.

    A failed refresh is any that yields None -- an offline host or malformed
    JSON, but the dominant case is the persistent HTTP 429 the oauth/usage
    endpoint has been returning (upstream bug anthropics/claude-code #30930).
    Its `Retry-After: 0` is bogus, so we ignore it and impose our own schedule:
    the interval doubles from the base each consecutive failure and is capped,
    so a 429 storm settles to one request per cap rather than one per TTL.
    """
    if fail_count <= 0:
        return 0.0
    return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (fail_count - 1)))


def _refresh() -> None:
    global _fetching, _fail_count
    try:
        value = fetch()
    except Exception:
        value = None
    with _lock:
        _cache["at"] = time.monotonic()
        if value is None:
            _fail_count += 1     # back off; the common cause is the 429 above
            # Keep the last good reading, marked stale. A new object, so a
            # frame already holding the old one is not changed under it.
            last = _cache["value"]
            if isinstance(last, Limits) and not last.stale:
                _cache["value"] = replace(last, stale=True)
            elif last is None:
                _cache["value"] = _recall()   # nothing yet: the stored one
        else:
            _fail_count = 0      # a good fetch clears the backoff at once
            if isinstance(value, Limits) and value.as_of is None:
                value.as_of = time.time()
            _cache["value"] = value
        _fetching = False


def get_limits(ttl: float = _TTL) -> Limits | None:
    """Most recent limits, refreshing in the background. Never blocks.

    Returns the cached value at once (None until the first fetch lands). After
    a failed refresh that is the last good reading with `stale` set. When
    the cache is older than the effective interval and no fetch is already
    running, a daemon thread is spawned to refresh it -- so the render loop is
    never held up by the HTTP round-trip. The effective interval is the larger
    of the caller's `ttl` and the current failure backoff, so a run of errors
    can only slow the polling, never speed it up.
    """
    now = time.monotonic()
    global _fetching
    spawn = False
    with _lock:
        value = _cache["value"]
        effective = max(ttl, _backoff_interval(_fail_count))
        if now - float(_cache["at"]) >= effective and not _fetching:
            _fetching = True
            spawn = True
    if spawn:
        threading.Thread(target=_refresh, name="claude-limits", daemon=True).start()
    return value  # type: ignore[return-value]
