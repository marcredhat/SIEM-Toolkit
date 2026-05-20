# Changes vs upstream `mickbrowns1/SIEM-Toolkit`

All edits are confined to four files; everything else is untouched.

## `backend/services/s1_client.py`

- PowerQuery timeout is now configurable via env (`SDL_PQ_TIMEOUT`,
  default **600s** — was hardcoded 120s, which was failing on grouped
  queries against busy tenants).
- Added `SDL_PQ_TIMEOUT_RETRIES` for retrying transient `ReadTimeout`s.
- Separate connect (15s) vs read timeouts via `httpx.Timeout(..., connect=15)`.
- All raised exceptions now include the request body / status / query so
  the UI never shows a blank `"PowerQuery error: "`.
- Non-JSON responses (HTML 5xx gateway pages) surface as a readable error
  string instead of crashing on `resp.json()`.

## `backend/routers/ingest.py`

- `/api/ingest/simulate-filter`:
  * Rebuilt the query into valid SDL syntax — was generating
    `| group events=count()` (dangling pipe) for empty bodies; now uses a
    proper base expression and falls back to `dataSource.name!=''` baseline.
  * Field name corrected from `src.name` → `dataSource.name`.
  * Surfaces both `result["error"]` and exception text so blank
    `"PowerQuery error: "` messages are gone.

## `backend/routers/quality.py`

- Added `GET /api/quality/parsers`: lists actual parser filenames in
  `/app/parsers/` (drives the Test Runner dropdown).
- `_flatten_event`: when a PowerQuery row only carries a JSON-stringified
  payload in `message` (i.e. the parser isn't applied at query time), parse
  and flatten that JSON inline so the Field Population tool can measure real
  coverage.
- `POST /api/quality/test-parser`:
  * Detects SDL JSON-mode parsers (`$=json{parse=json}$`) and parses log
    lines as JSON.
  * Applies parser `rewrites: [{input,output,match,replace}]` blocks with
    correct `$0/$N` backreference translation (`$0` was being mangled to a
    null byte).
  * Accepts single JSON object, JSON array, or NDJSON multi-line input.
  * Returns mode badge data + per-payload counters for the UI.

## `frontend/index.html`

- Parser Test Runner dropdown now loads from `/api/quality/parsers` instead
  of filtering the coverage map (which only has `detected in data`
  placeholders).
- Field Population and Sample Events: added **Last 7d** lookback option.
- Parser Test Runner UI: mode badge (`JSON auto-extract` vs `regex format`),
  payload counter for multi-line input, separate tables for extracted vs
  derived/rewritten fields.

## New helper scripts (`tools/`)

- `sync_sdl_parsers.py` — pull all `/logParsers/*` from the tenant.
- `probe_pq_syntax.py` — probe which PowerQuery syntaxes the tenant accepts.
- `probe_avelios{,_wide,_fields}.py` — inspect a source's event presence,
  columns, and embedded JSON fields.
- `test_avelios_parser.py`, `test_avelios_multi.py` — smoke-test the patched
  `/api/quality/test-parser` endpoint with single-line and multi-line input.
- `probe_simulate_filter.py` — smoke-test the patched
  `/api/ingest/simulate-filter` endpoint with progressively larger windows.
- `sdl_config.example.json` — template config (the toolkit's `.env` is
  separate from the SDL config used by these helper scripts).

## New `.env` knobs

```bash
SDL_PQ_TIMEOUT=600              # PowerQuery read timeout in seconds (default 600)
SDL_PQ_TIMEOUT_RETRIES=1        # extra retries on ReadTimeout  (default 1)
```
