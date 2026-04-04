"""
Wall-clock wait until a Unix epoch millisecond target.

Used by mint.py for genesis timing; injectable clock/sleep for tests.
"""

import time
from collections.abc import Callable


def fmt_duration(ms: int) -> str:
    """Format milliseconds as a human-readable duration like '3 hours, 21 minutes, 30 seconds'."""
    total_secs = ms // 1000
    if total_secs <= 0:
        return "0 seconds"

    days, rem = divmod(total_secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs or not parts:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return ", ".join(parts)


def next_sleep_seconds(remaining_ms: int) -> float | None:
    """
    How long to sleep before re-checking the clock, or None if the target
    instant has passed (remaining_ms <= 0).
    """
    if remaining_ms <= 0:
        return None
    if remaining_ms > 60_000:
        return min(remaining_ms / 1000 - 30, 60)
    if remaining_ms > 5_000:
        return 1.0
    return remaining_ms / 1000


def wait_until_unix_ms(
    target_ms: int,
    *,
    now_ms: Callable[[], int] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """
    Block until ``now_ms() >= target_ms`` (wall clock, best effort).

    Uses the same adaptive sleep strategy as before: long sleeps until ~30s
    before the target, 1s ticks under a minute, then one final sleep for the
    last ≤5s so we do not wake hundreds of times.

    Inject ``now_ms`` and ``sleep_fn`` for tests (fake clock).
    """
    _now_ms = now_ms if now_ms is not None else lambda: int(time.time() * 1000)
    _sleep = sleep_fn if sleep_fn is not None else time.sleep
    _log = log if log is not None else print

    while True:
        remaining = target_ms - _now_ms()
        secs = next_sleep_seconds(remaining)
        if secs is None:
            return
        if remaining > 5_000:
            _log(f"    T-{fmt_duration(remaining)}")
        _sleep(secs)
