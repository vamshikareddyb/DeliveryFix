"""Injectable clock so scenarios and tests are deterministic."""
from datetime import datetime, timezone
from typing import Callable

_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def now() -> datetime:
    return _now()


def set_clock(fn: Callable[[], datetime]) -> None:
    global _now
    _now = fn


def freeze(at: datetime) -> None:
    set_clock(lambda: at)
