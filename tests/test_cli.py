"""Tests for the CLI composition helpers.

The one thing here that carries real logic is `usage_poll_interval`: the pure
map from the live session list to how often the usage endpoint should be polled.
It lives in cli.py (the composition layer) on purpose - claude_limits.py must
stay ignorant of claude_sessions.
"""

import time

from tzmrit_display.claude_sessions import Session
from tzmrit_display.cli import (
    POLL_ACTIVE,
    POLL_IDLE,
    POLL_RECENT,
    usage_poll_interval,
)


def _sess(status, inactive):
    """A session whose transcript clock reads `inactive` seconds ago."""
    return Session(1, "n", "/x", status, "i", active_at=time.time() - inactive)


class TestUsagePollInterval:
    def test_no_sessions_is_idle(self):
        assert usage_poll_interval([]) == POLL_IDLE

    def test_recent_turn_polls_fast(self):
        # A turn moments ago (transcript ~5s old) -> poll every minute.
        assert usage_poll_interval([_sess("busy", 5)]) == POLL_ACTIVE

    def test_activity_within_minutes_polls_medium(self):
        # 2 min since the last turn, nothing waiting/working now -> 3 min.
        assert usage_poll_interval([_sess("idle", 120)]) == POLL_RECENT

    def test_quiet_board_polls_slow(self):
        # Everything quiet for 10 min -> back off to 10 min.
        assert usage_poll_interval([_sess("idle", 600)]) == POLL_IDLE

    def test_waiting_session_keeps_medium_even_when_old(self):
        # A session waiting for a human writes no transcript, so its clock runs
        # up; but a human is expected any moment, so don't sink to fully idle.
        assert usage_poll_interval([_sess("waiting", 600)]) == POLL_RECENT

    def test_working_session_floors_at_medium(self):
        # A genuinely-working (busy + fresh) session with an oddly old clock
        # still counts as activity -> at least medium cadence.
        s = _sess("busy", 3)  # fresh -> working True, and min_inactive < 60
        assert usage_poll_interval([s]) == POLL_ACTIVE

    def test_min_across_sessions_wins(self):
        # One quiet session, one active: the most-recent activity drives it.
        sessions = [_sess("idle", 600), _sess("busy", 4)]
        assert usage_poll_interval(sessions) == POLL_ACTIVE

    def test_ordering_of_the_three_intervals(self):
        assert POLL_ACTIVE < POLL_RECENT < POLL_IDLE
