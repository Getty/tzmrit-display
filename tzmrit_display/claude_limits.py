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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage?at_wall=1&skip_spend=1"
_BETA = "oauth-2025-04-20"

# The session window is 5 hours, the weekly (all models) window is 7 days.
_SESSION_KIND = "session"
_WEEKLY_KIND = "weekly_all"


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

    @property
    def rows(self) -> list[Limit]:
        return [x for x in (self.session, self.weekly) if x is not None]


# -- parsing (pure) ------------------------------------------------------

def parse_usage(data: object) -> Limits | None:
    """Turn a usage response into Limits, or None if nothing is usable.

    Prefers the typed `limits[]` array; falls back to the flat
    `five_hour`/`seven_day` buckets for either window independently.
    """
    if not isinstance(data, dict):
        return None
    session = _from_limits(data, _SESSION_KIND) or _from_bucket(data.get("five_hour"))
    weekly = _from_limits(data, _WEEKLY_KIND) or _from_bucket(data.get("seven_day"))
    if session is not None:
        session.label = "Session"
    if weekly is not None:
        weekly.label = "Weekly"
    if session is None and weekly is None:
        return None
    return Limits(session=session, weekly=weekly)


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
    return f"{seconds // 86400}d"


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
    return parse_usage(payload)


# -- cached, off-thread access ------------------------------------------

_TTL = 60.0
# Exponential backoff after failed refreshes. The base doubles each consecutive
# failure up to the cap, so a persistent error settles to one request per cap.
_BACKOFF_BASE = 60.0     # first extra spacing after one failure
_BACKOFF_CAP = 1800.0    # 30 min ceiling (n>=7 sits here)
_lock = threading.Lock()
_cache: dict[str, object] = {"at": 0.0, "value": None}
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
        _cache["value"] = value
        if value is None:
            _fail_count += 1     # back off; the common cause is the 429 above
        else:
            _fail_count = 0      # a good fetch clears the backoff at once
        _fetching = False


def get_limits(ttl: float = _TTL) -> Limits | None:
    """Most recent limits, refreshing in the background. Never blocks.

    Returns the cached value at once (None until the first fetch lands). When
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
