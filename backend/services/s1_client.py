import os
import asyncio
import httpx
from datetime import datetime, timezone

BASE_URL = os.environ.get("S1_BASE_URL", "https://demo.sentinelone.net").rstrip("/")
TOKEN = os.environ.get("S1_API_TOKEN", "")

# Scalyr/XDR PowerQuery credentials — from SDL_XDR_URL + SDL_LOG_READ_KEY
# in the SentinelOne console: Settings → Integrations → Data Lake API Keys
SDL_XDR_URL = os.environ.get("SDL_XDR_URL", "https://xdr.us1.sentinelone.net").rstrip("/")
SDL_LOG_READ_KEY = os.environ.get("SDL_LOG_READ_KEY", "")

# PowerQuery timeout (seconds). Grouped queries on busy tenants can take
# 2-5 minutes; default 600s, override via SDL_PQ_TIMEOUT env.
SDL_PQ_TIMEOUT = float(os.environ.get("SDL_PQ_TIMEOUT", "600"))
# How many times to retry on a ReadTimeout. Default 1 (so a single retry).
SDL_PQ_TIMEOUT_RETRIES = int(os.environ.get("SDL_PQ_TIMEOUT_RETRIES", "1"))

# Management Console API uses ApiToken auth
HEADERS = {
    "Authorization": f"ApiToken {TOKEN}",
    "Content-Type": "application/json",
}


def _iso_to_epoch_ms(iso_str: str) -> int:
    """Convert ISO-8601 UTC string to epoch milliseconds for Scalyr API."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


async def get_star_rules(page_size: int = 100) -> list:
    """Fetch custom STAR rules from /cloud-detection/rules, paginating via cursor."""
    all_rules = []
    cursor = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {"limit": page_size}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(
                f"{BASE_URL}/web/api/v2.1/cloud-detection/rules",
                headers=HEADERS,
                params=params,
            )
            resp.raise_for_status()
            body = resp.json()
            all_rules.extend(body.get("data", []))
            cursor = body.get("pagination", {}).get("nextCursor")
            if not cursor:
                break
    return all_rules


async def get_library_rules(page_size: int = 100) -> list:
    """
    Fetch Detection Library (OOTB/Platform) rules from /web/api/v2.1/detection-library/rules.
    Requires an account-level or higher API token — site-scoped tokens will receive a 400.
    Returns an empty list gracefully if the token lacks sufficient scope.
    """
    all_rules = []
    cursor = None
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            params: dict = {"limit": page_size}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(
                f"{BASE_URL}/web/api/v2.1/detection-library/rules",
                headers=HEADERS,
                params=params,
            )
            # 400 typically means site-scoped token — return empty rather than crash
            if resp.status_code == 400:
                return []
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("data", [])
            all_rules.extend(batch)
            cursor = body.get("pagination", {}).get("nextCursor")
            if not cursor:
                break

    results = []
    for rule in all_rules:
        results.append({
            "id": str(rule.get("id", "")),
            "name": rule.get("name", "unnamed"),
            "s1ql": rule.get("s1ql") or rule.get("query", ""),
            "queryType": rule.get("queryType", "events"),
            "severity": rule.get("severity", ""),
            "description": rule.get("description", ""),
            "gdlRuleId": rule.get("id", ""),
            "creator": "SentinelOne",
            "expirationMode": rule.get("expirationMode", "Permanent"),
        })
    return results


async def run_powerquery(query: str, from_date: str, to_date: str) -> dict:
    """
    Run a PowerQuery against the Singularity Data Lake via the Scalyr XDR API.
    Uses SDL_XDR_URL + SDL_LOG_READ_KEY (Scalyr readlog token).
    The Scalyr PowerQuery API is synchronous — results return in one request.
    """
    if not SDL_LOG_READ_KEY:
        return {"events": [], "error": "SDL_LOG_READ_KEY not configured — add it to .env"}

    start_ms = _iso_to_epoch_ms(from_date)
    end_ms = _iso_to_epoch_ms(to_date)

    payload = {
        "token": SDL_LOG_READ_KEY,
        "query": query,
        "startTime": start_ms,
        "endTime": end_ms,
        "maxCount": 1000,
    }

    timeout_cfg = httpx.Timeout(SDL_PQ_TIMEOUT, connect=15.0)
    max_attempts = 3 + SDL_PQ_TIMEOUT_RETRIES  # 3 for 429 retries + N for ReadTimeout
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        last_err: Exception | None = None
        for attempt in range(max_attempts):
            try:
                resp = await client.post(
                    f"{SDL_XDR_URL}/api/powerQuery",
                    json=payload,
                )
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                body = (e.response.text or "<empty body>")[:500]
                raise RuntimeError(
                    f"HTTP {e.response.status_code} from {e.request.url}: {body}"
                ) from e
            except httpx.ReadTimeout as e:
                last_err = e
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"ReadTimeout after {SDL_PQ_TIMEOUT}s talking to "
                    f"{SDL_XDR_URL}/api/powerQuery (attempts={attempt + 1}). "
                    f"Try a shorter time window or raise SDL_PQ_TIMEOUT in .env. "
                    f"Query was: {query[:200]}"
                ) from e
            except httpx.RequestError as e:
                raise RuntimeError(
                    f"network error talking to {SDL_XDR_URL}/api/powerQuery: "
                    f"{type(e).__name__}: {e or 'no detail'}"
                ) from e

        try:
            data = resp.json()
        except Exception as e:
            return {"events": [], "error":
                    f"non-JSON response (HTTP {resp.status_code}): "
                    f"{(resp.text or '<empty>')[:400]}"}

        status = data.get("status", "")

        if status != "success":
            # Surface the full response so callers can show a real error.
            return {"events": [], "error":
                    f"PowerQuery status={status or '<missing>'}: {str(data)[:400]}"}

        # Scalyr PowerQuery returns: {"status":"success","columns":[{"name":"..."},...], "values":[[...],...],...}
        raw_cols = data.get("columns", [])
        values = data.get("values", [])

        if raw_cols and values:
            # columns may be list of strings or list of {"name":...} dicts
            col_names = [
                c["name"] if isinstance(c, dict) else c
                for c in raw_cols
            ]
            rows = [dict(zip(col_names, row)) for row in values]
            return {"events": rows}

        # Fallback: return raw matches array
        matches = data.get("matches", [])
        return {"events": matches}


async def list_sdl_parsers() -> list[str]:
    """List all parser filenames under /logParsers/ in SDL."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/api/v1/files/logParsers",
            headers=HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response is a list of file objects or a dict with 'files' key
        if isinstance(data, list):
            return [f.get("name") or f.get("path", "") for f in data if isinstance(f, dict)]
        return [f.get("name") or f.get("path", "") for f in data.get("files", [])]


async def get_sdl_parser(filename: str) -> dict:
    """Fetch a single SDL parser file by name."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/api/v1/files/logParsers/{filename}",
            headers=HEADERS,
        )
        resp.raise_for_status()
        return resp.json()


async def get_account_id() -> str | None:
    """Return the first account ID visible to the current token."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/web/api/v2.1/accounts",
            headers=HEADERS,
            params={"limit": 1},
        )
        resp.raise_for_status()
        accounts = resp.json().get("data", [])
        return str(accounts[0]["id"]) if accounts else None


async def get_platform_rules(page_size: int = 1000) -> list:
    """
    Fetch all Detection Library platform rules from /detection-library/platform-rules.
    Requires scopeLevel + scopeId — uses account scope with the first visible account.
    Returns list of rules, each with a 'sources' list (authoritative data source names).
    """
    account_id = await get_account_id()
    if not account_id:
        return []

    all_rules: list = []
    cursor: str = ""
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            params: dict = {
                "scopeLevel": "account",
                "scopeId": account_id,
                "limit": page_size,
                "cursor": cursor,
            }
            resp = await client.get(
                f"{BASE_URL}/web/api/v2.1/detection-library/platform-rules",
                headers=HEADERS,
                params=params,
            )
            if resp.status_code == 400:
                return []
            resp.raise_for_status()
            body = resp.json()
            all_rules.extend(body.get("data", []))
            cursor = body.get("pagination", {}).get("nextCursor") or ""
            if not cursor:
                break
    return all_rules


async def get_sites() -> list:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/web/api/v2.1/sites",
            headers=HEADERS,
            params={"limit": 100},
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("sites", [])
