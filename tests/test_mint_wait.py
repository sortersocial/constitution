"""
Tests for mint_wait: fmt_duration, next_sleep_seconds, wait_until_unix_ms.

Uses a fake clock so we verify we reach target_ms without relying on wall time.
"""

import pytest

from mint_wait import fmt_duration, next_sleep_seconds, wait_until_unix_ms


class FakeClock:
    """Monotonic ms clock advanced only by sleep_fn (for testing wait_until_unix_ms)."""

    def __init__(self, start_ms: int):
        self.t = float(start_ms)

    def now_ms(self) -> int:
        return int(self.t)

    def sleep(self, secs: float) -> None:
        self.t += secs * 1000


@pytest.mark.parametrize(
    "remaining_ms, expected",
    [
        (-1, None),
        (0, None),
        (1, 0.001),
        (5000, 5.0),
        (5001, 1.0),
        (60_000, 1.0),
        (60_001, 30.001),  # min(60001/1000 - 30, 60)
        (90_000, 60.0),
        (65_000, 35.0),
    ],
)
def test_next_sleep_seconds(remaining_ms, expected):
    got = next_sleep_seconds(remaining_ms)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_wait_until_hits_target_short_final_sleep():
    """Last ≤5s: one sleep of remaining_ms/1000 lands exactly on target."""
    clock = FakeClock(start_ms=0)
    target = 2500
    wait_until_unix_ms(target, now_ms=clock.now_ms, sleep_fn=clock.sleep, log=lambda _: None)
    assert clock.now_ms() >= target
    assert clock.now_ms() == target


def test_wait_until_hits_target_past_returns_immediately():
    clock = FakeClock(start_ms=10_000)
    target = 1000
    sleeps: list[float] = []

    def record_sleep(s: float):
        sleeps.append(s)
        clock.sleep(s)

    wait_until_unix_ms(target, now_ms=clock.now_ms, sleep_fn=record_sleep, log=lambda _: None)
    assert clock.now_ms() == 10_000
    assert sleeps == []


def test_wait_until_hits_target_ten_second_window():
    """5s < remaining ≤ 60s: 1s steps then final ≤5s sleep."""
    clock = FakeClock(start_ms=0)
    target = 10_000
    wait_until_unix_ms(target, now_ms=clock.now_ms, sleep_fn=clock.sleep, log=lambda _: None)
    assert clock.now_ms() >= target
    assert clock.now_ms() == target


def test_wait_until_hits_target_after_long_coarse_sleep():
    """remaining > 60s: first chunk uses min(remaining/1000 - 30, 60)."""
    clock = FakeClock(start_ms=0)
    target = 100_000  # 100s — first sleep 60s, then 1s ticks, then final ≤5s
    wait_until_unix_ms(target, now_ms=clock.now_ms, sleep_fn=clock.sleep, log=lambda _: None)
    assert clock.now_ms() >= target
    assert clock.now_ms() == target


def test_wait_until_sum_of_sleeps_equals_delta():
    """Total simulated time matches target - start (no missing or extra ms)."""
    clock = FakeClock(start_ms=123)
    target = 123 + 99_999
    wait_until_unix_ms(target, now_ms=clock.now_ms, sleep_fn=clock.sleep, log=lambda _: None)
    assert clock.now_ms() == target


def test_wait_until_logs_human_readable_durations():
    """Log messages use human-readable format like '1 minute, 40 seconds'."""
    clock = FakeClock(start_ms=0)
    target = 100_000
    logs: list[str] = []
    wait_until_unix_ms(target, now_ms=clock.now_ms, sleep_fn=clock.sleep, log=logs.append)
    assert any("minute" in msg or "second" in msg for msg in logs)
    assert not any("T-" in msg and msg.endswith("s") and "second" not in msg for msg in logs)


# ---------------------------------------------------------------------------
# fmt_duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ms, expected",
    [
        (0, "0 seconds"),
        (999, "0 seconds"),
        (1_000, "1 second"),
        (2_000, "2 seconds"),
        (60_000, "1 minute"),
        (61_000, "1 minute, 1 second"),
        (3_600_000, "1 hour"),
        (3_661_000, "1 hour, 1 minute, 1 second"),
        (86_400_000, "1 day"),
        (90_061_000, "1 day, 1 hour, 1 minute, 1 second"),
        (12_090_000, "3 hours, 21 minutes, 30 seconds"),
    ],
)
def test_fmt_duration(ms, expected):
    assert fmt_duration(ms) == expected
