"""Rate limiting for the two endpoints anyone on the internet can reach.

`/auth/login` and `/auth/redeem-invite` are the only unauthenticated routes,
and neither had anything slowing a guesser down. An invite code is twelve
characters from a 32-letter alphabet, which is far too many to guess at human
speed and not obviously too many to guess at a few thousand attempts a second.

In-process, deliberately. The free tier runs exactly one instance, so a
dictionary is accurate here and costs no database round trip on the hot path.
If this is ever scaled out, the counters need to move into the database or a
cache - each instance would otherwise allow the full quota on its own.

Only failures are counted. Someone signing in correctly all day is not
attacking anything, and a candidate who mistypes their password twice should
not be locked out by a shared exam-centre IP that is also being used properly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class Limit:
    attempts: int
    per_seconds: int

    @property
    def window_minutes(self) -> int:
        return max(1, self.per_seconds // 60)


# A candidate who has forgotten which password they used gets a fair number of
# tries; a script gets nowhere. Invite redemption is tighter because a wrong
# code is never an honest mistake repeated ten times.
LOGIN_LIMIT = Limit(attempts=10, per_seconds=15 * 60)
INVITE_LIMIT = Limit(attempts=10, per_seconds=60 * 60)


@dataclass
class _Counter:
    """Failure timestamps for one key, oldest first."""

    hits: list[float] = field(default_factory=list)


class Throttle:
    def __init__(self, limit: Limit) -> None:
        self._limit = limit
        self._counters: dict[str, _Counter] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Refuse with 429 if `key` has already used up its failures."""
        now = time.monotonic()
        with self._lock:
            counter = self._counters.get(key)
            if counter is None:
                return
            self._expire(counter, now)
            if not counter.hits:
                del self._counters[key]
                return
            if len(counter.hits) >= self._limit.attempts:
                retry_after = int(counter.hits[0] + self._limit.per_seconds - now) + 1
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Too many attempts. Try again in "
                        f"{max(1, retry_after // 60)} minute(s)."
                    ),
                    headers={"Retry-After": str(max(1, retry_after))},
                )

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            counter = self._counters.setdefault(key, _Counter())
            self._expire(counter, now)
            counter.hits.append(now)
            self._sweep(now)

    def clear(self, key: str) -> None:
        """A success wipes the slate, so a mistyped password costs nothing."""
        with self._lock:
            self._counters.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()

    def _expire(self, counter: _Counter, now: float) -> None:
        cutoff = now - self._limit.per_seconds
        counter.hits = [hit for hit in counter.hits if hit > cutoff]

    def _sweep(self, now: float) -> None:
        """Drop keys that have gone quiet.

        Without this the dictionary is an unbounded record of every address
        that ever mistyped a password. Sweeping on write is enough: the map
        only grows on write, and the work is proportional to what is in it.
        """
        if len(self._counters) < 512:
            return
        cutoff = now - self._limit.per_seconds
        self._counters = {
            key: counter
            for key, counter in self._counters.items()
            if counter.hits and counter.hits[-1] > cutoff
        }


login_throttle = Throttle(LOGIN_LIMIT)
invite_throttle = Throttle(INVITE_LIMIT)


def client_key(request: Request, *, scope: str) -> str:
    """Identify the caller for throttling.

    Render terminates TLS at its proxy, so `request.client.host` is the proxy
    for every caller and would throttle the whole site as one. The first entry
    in `X-Forwarded-For` is the original client. It is forgeable, which is why
    the login limit is also applied per email address - forging the header
    moves you to a fresh bucket for the address but not for the account you are
    trying to break into.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    address = forwarded.split(",")[0].strip() if forwarded else ""
    if not address:
        address = request.client.host if request.client else "unknown"
    return f"{scope}:{address}"
