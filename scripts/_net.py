"""Shared polite HTTP fetch.

Enforces a minimum gap between consecutive outbound requests across the
whole process so we don't trip publisher rate-limit bans. One shared
timestamp; the last call's start time gates the next call's start time.
"""
import threading
import time
import urllib.request

_LAST_FETCH = [0.0]
_LOCK = threading.Lock()
DEFAULT_GAP = 1.0


def polite_urlopen(req_or_url, timeout=30, min_gap=DEFAULT_GAP):
    """Throttled urllib.request.urlopen.

    Sleeps any remaining time so at least `min_gap` seconds have elapsed
    since the previous polite_urlopen call (process-wide). Records the
    timestamp BEFORE the network call, so the gap covers
    "request-start" -> "next-request-start" rather than body-completion
    times -- a slow server already eats some of the gap on its own.

    Returns the same HTTPResponse object as urlopen; usable as a context
    manager.
    """
    with _LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_FETCH[0]
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        _LAST_FETCH[0] = time.monotonic()
    return urllib.request.urlopen(req_or_url, timeout=timeout)
