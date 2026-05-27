"""Background pre-warmer for the Ingest Dashboard cache.

Opt-in via env: INGEST_PREWARM=1
Tunable via env: INGEST_PREWARM_INTERVAL_SECONDS (default 240, just under TTL)
                 INGEST_PREWARM_HOURS  (default "1,24,168")
                 INGEST_PREWARM_DAYS   (default "7")
                 INGEST_PREWARM_DAILY_VOLUME_DAYS (default "5")

The pre-warmer re-runs the heavy Ingest Dashboard queries every ~4 min so the
in-process TTL cache is always populated. First user hit then returns from
cache (sub-millisecond) instead of waiting 30-60s for SDL.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time

# Use the uvicorn logger so messages show up in `docker logs` alongside requests.
log = logging.getLogger("uvicorn.error")
_PREFIX = "prewarmer:"


def _flag_enabled() -> bool:
    return os.environ.get("INGEST_PREWARM", "").lower() in ("1", "true", "yes", "on")


def _int_list(env: str, default: str) -> list[int]:
    raw = os.environ.get(env, default)
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok and tok.isdigit():
            out.append(int(tok))
    return out


async def _warm_once() -> dict:
    """Run all configured warm-up queries once. Returns timing summary."""
    # Local import to avoid circular dependency with FastAPI router module.
    from routers.ingest import (
        _top_sources_cached,
        _by_event_type_cached,
        _daily_volume_cached,
    )

    hours_list = _int_list("INGEST_PREWARM_HOURS", "1,24,168")
    days_list = _int_list("INGEST_PREWARM_DAYS", "7")
    dv_days = _int_list("INGEST_PREWARM_DAILY_VOLUME_DAYS", "5") or [5]

    tasks: list[tuple[str, asyncio.Task]] = []
    for h in hours_list:
        tasks.append((f"top-sources hours={h}",
                      asyncio.create_task(_top_sources_cached(h, nocache=True))))
    for d in days_list:
        tasks.append((f"by-event-type days={d}",
                      asyncio.create_task(_by_event_type_cached(d, nocache=True))))
    for d in dv_days:
        tasks.append((f"daily-volume days={d}",
                      asyncio.create_task(_daily_volume_cached(d, nocache=True))))

    summary: dict[str, str] = {}
    for label, task in tasks:
        t0 = time.monotonic()
        try:
            await task
            summary[label] = f"OK in {time.monotonic() - t0:.1f}s"
        except Exception as e:
            summary[label] = f"ERR ({e.__class__.__name__}: {str(e)[:120]})"
    return summary


async def _loop():
    interval = int(os.environ.get("INGEST_PREWARM_INTERVAL_SECONDS", "240"))
    log.info("%s starting (interval=%ds)", _PREFIX, interval)
    # Tiny initial delay so we don't compete with startup work.
    await asyncio.sleep(5)
    while True:
        try:
            summary = await _warm_once()
            for label, status in summary.items():
                log.info("%s %s -> %s", _PREFIX, label, status)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s cycle failed: %s", _PREFIX, e)
        await asyncio.sleep(interval)


def start_if_enabled() -> asyncio.Task | None:
    """Spawn the pre-warm background task if INGEST_PREWARM is enabled.
    Returns the task handle, or None if disabled."""
    if not _flag_enabled():
        log.info("%s disabled (set INGEST_PREWARM=1 to enable)", _PREFIX)
        return None
    log.info("%s scheduling background task", _PREFIX)
    return asyncio.create_task(_loop(), name="ingest-prewarmer")
