"""
Shared test helper — exists specifically because the same bug has now
been made three times across this project's test suite: hardcoding a
wall-clock timestamp (e.g. "2026-08-07T12:00:00+00:00") for a synthetic
pair-creation date, which silently drifts out of whatever age window a
test depends on as real session time passes, flipping test results for
reasons that have nothing to do with the code being tested.

Use `relative_iso_timestamp(minutes_ago)` instead of a hardcoded ISO
string anywhere a test needs "a pair created N minutes ago" — it's
always correct relative to when the test actually runs, not a specific
calendar date.
"""

from datetime import datetime, timedelta, timezone


def relative_iso_timestamp(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
