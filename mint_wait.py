"""
Wall-clock wait until a Unix epoch millisecond target.

Used by mint.py for genesis timing; injectable clock/sleep for tests.
"""

import time
from collections.abc import Callable


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
        if remaining > 60_000:
            _log(f"    T-{remaining / 1000:.0f}s")
        elif remaining > 5_000:
            _log(f"    T-{remaining / 1000:.1f}s")
        _sleep(secs)
