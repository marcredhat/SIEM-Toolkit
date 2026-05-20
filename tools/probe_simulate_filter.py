#!/usr/bin/env python3
"""Probe /api/ingest/simulate-filter using small 1-day windows + long client
timeouts to avoid urllib aborting before the SDL query returns.

Run one case at a time and print elapsed time so we can tell whether failures
are HTTP errors or slow tenant queries.
"""
from __future__ import annotations
import json
import sys
import time
import urllib.request
import urllib.error

URL = "http://localhost:8001/api/ingest/simulate-filter"
TIMEOUT = 600  # seconds — generous; SDL queries on large tenants can take >60s

# Smallest windows first so cheap calls succeed before we try the expensive ones.
CASES = [
    ("empty body, 1d",            {"days": 1}),
    ("bogus source, 1d",          {"source": "definitely-no-such-source", "days": 1}),
    ("source only, 1d",           {"source": "Avelios Medical", "days": 1}),
    ("source only, 7d",           {"source": "Avelios Medical", "days": 7}),
    ("event_type only, 1d",       {"event_type": "login", "days": 1}),
    ("source + event_type, 7d",   {"source": "Avelios Medical", "event_type": "login", "days": 7}),
]


def hit(body: dict) -> tuple[int, str, float]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode(), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), time.monotonic() - t0
    except Exception as e:
        return -1, f"{type(e).__name__}: {e or 'no detail'}", time.monotonic() - t0


# Allow narrowing via CLI: `python3 probe_simulate_filter.py 2 3` runs cases 2 & 3.
indices = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else range(len(CASES))

for i in indices:
    if i >= len(CASES):
        continue
    label, body = CASES[i]
    print("=" * 78)
    print(f"[{i}] {label:<32} body={body}")
    sys.stdout.flush()
    status, payload, elapsed = hit(body)
    print(f"    HTTP {status}    elapsed={elapsed:.1f}s")
    try:
        parsed = json.loads(payload)
        print("    " + json.dumps(parsed, indent=2).replace("\n", "\n    "))
    except Exception:
        print(f"    raw: {payload[:800]}")
