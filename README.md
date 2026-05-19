# SIEM Toolkit — SentinelOne AI-SIEM

A self-hosted troubleshooting and visibility tool for SentinelOne AI-SIEM SecOps engineers. Runs as a Docker Compose stack against your SentinelOne demo or production tenant and gives you real-time insight into parser coverage, ingest volume, and data quality without leaving a single UI.

---

## What's inside

| Page | Purpose |
|---|---|
| **Parser Coverage Map** | Which active data sources have a parser? Which don't? |
| **Ingest Dashboard** | Event volume, top sources, cost projection, filter simulator |
| **Parser Quality** | Live event sampler, field population rate, parser test runner |
| **Onboarding Accelerator** | Prompt template for onboarding new log sources with Claude Code |
| **Settings** | Manage your `.env` credentials from the UI |

---

## Architecture

```
browser → nginx (port 3001) → single-page HTML/JS app
                ↓ API calls
          FastAPI backend (port 8001)
                ↓
    ┌───────────────────────────┐
    │  PostgreSQL (SQLAlchemy)  │  parsed rules, parser fields, active sources
    └───────────────────────────┘
                ↓
    ┌───────────────────────────┐
    │  SentinelOne APIs         │
    │  • Management API (STAR)  │  demo.sentinelone.net
    │  • Scalyr XDR PowerQuery  │  xdr.us1.sentinelone.net
    └───────────────────────────┘
```

All services run via Docker Compose. The `parsers/` directory is volume-mounted into the backend so SDL parser files can be loaded without rebuilding the image.

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/mickbrowns1/SIEM-Toolkit.git
cd SIEM-Toolkit
cp .env.example .env
```

Edit `.env` with your credentials:

```env
S1_BASE_URL=https://demo.sentinelone.net       # Your console URL
S1_API_TOKEN=eyJ...                             # Service user API token
SDL_XDR_URL=https://xdr.us1.sentinelone.net    # Scalyr XDR endpoint
SDL_LOG_READ_KEY=1j2IU0S...                     # Data Lake read key
ANTHROPIC_API_KEY=                              # Optional — Onboarding page only
```

**S1_API_TOKEN** — generate at *Settings → Users → Service Users* in the console.  
**SDL_LOG_READ_KEY** — found at *Settings → Integrations → Data Lake API Keys*.

### 2. Add parser files (optional but recommended)

Drop SDL parser JSON files into `parsers/`. The backend reads them directly — no rebuild needed.

```bash
cp ~/my-parsers/*.json parsers/
```

### 3. Start the stack

```bash
docker-compose up -d --build
```

Open **http://localhost:3001** in your browser.

---

## Features

### Parser Coverage Map

Answers: *does each active data source have a parser running?*

**How it works:**

1. **Sync Live Sources** — runs a PowerQuery against your data lake to pull every `dataSource.name` seen in the last 7 days, along with event counts.
2. **Load SDL Parsers** — reads parser files from `parsers/`, extracts the `dataSource.name` attribute from each, and stores the field list.
3. **Load STAR Rules** — pulls your STAR detection rules from the management API and indexes which data sources each rule references.

**Matching logic (three-tier):**
1. Exact `dataSource.name` match between active source and parser attribute
2. Normalized substring match (ignores spaces, dashes, case) between active source name and parser's `dataSource.name`
3. Normalized substring match against the parser filename — catches files where the `dataSource.name` attribute is wrong or missing

**Parser detection from data:** During sync, a parallel PowerQuery checks whether each source has events with `event.type` populated in the data lake. If yes, a parser is confirmed running — the source is marked **Covered** even without a local parser file. This handles built-in and cloud-managed parsers that aren't in your `parsers/` folder.

**Status values:**
- 🟢 **Covered** — custom parser confirmed (local file or detected via parsed events in data)
- 🔴 **Parser Needed** — no parser found, or only a grok/dottedJson format (which typically signals an incomplete parser)

**Expected results:** After syncing sources and loading parsers, sources with active SDL parsers show as Covered. Sources sending raw unparsed data (only `message` and `timestamp` in the data lake) show as Parser Needed.

---

### Ingest Dashboard

Answers: *where is my event volume coming from, and what would happen if I filtered some of it?*

**Time range:** 1h (default), 3d, 5d, 7d

**Daily Event Volume** — bar chart of total events per day. In 1h mode, switches to a by-source breakdown of the current hour.

**Top Sources** — table of the 25 highest-volume `dataSource.name` values with event count and estimated GB (based on 0.5 GB per million events).

**Filter Simulator** — enter a source name and optional event type, hit Simulate. The backend runs a live PowerQuery counting matching events and projects:
- Matched events in the period
- Estimated GB saved in the period  
- Projected monthly events and GB if the filter were applied

This is read-only — no filter is created. Use the results to inform an exclusion rule you apply manually in the console.

**Expected results:** Top sources reflect what you see in the SentinelOne console PowerQuery. The filter simulator gives a reasonable GB estimate assuming uniform event size.

---

### Parser Quality

Three tools for diagnosing parser extraction failures.

#### Live Event Sampler

Pulls raw events from a selected source directly from the data lake and renders every field that came back. The `message` column is pinned to the right and has a **⎘ copy** button on each row for quick extraction.

- **Empty fields** show as `∅` in gray — immediately highlights fields the parser isn't populating
- **Expected result on a healthy source:** Many fields populated (`src.ip`, `user.name`, `event.type`, etc.), `message` present as raw log backup
- **Expected result on an unhealthy source:** Only `timestamp` and `message` populated — the parser isn't extracting anything

#### Field Population Rate

Samples up to 500 events from a source and measures what percentage of them have each field populated. Sorted worst-first.

When you select a source, the tool auto-discovers what fields exist in that source's events and pre-fills the field list — merged with SDL schema defaults. You can edit the list before running.

**Colour coding:**
- 🟢 ≥ 80% — healthy extraction
- 🟡 40–79% — partial extraction, check regex patterns
- 🔴 < 40% — field is rarely populated; parser likely not matching this log format

**Expected result on a working parser:** Key fields like `src.ip`, `event.type`, `user.name` should be 70–100%. Niche fields like `src.process.cmdline` or `tgt.file.path` will naturally be lower (not every event type produces them).

**Expected result on a broken parser:** All SDL fields at 0%, only `timestamp` and `message` visible in the "fields seen in sample" chip list at the bottom.

#### Parser Test Runner

Paste a raw log line, select a loaded parser, hit Test. The backend extracts SDL `$field=pattern$` format strings from the parser file, converts them to Python named-group regex, and tries each against your log line.

- **Matched:** shows the format string that matched and every field extracted with its value
- **No match:** means none of the parser's format strings apply to this log line — the log may have a format variant the parser doesn't cover

> Note: only parsers using SDL custom format strings are testable here. Grok and dottedJson parsers are not currently supported by the test runner.

---

### Onboarding Accelerator

A prompt template for using Claude Code to onboard a new log source. Copy the template, paste a sample of raw log lines, and Claude Code will generate:

- An SDL parser skeleton in augmented-JSON format
- Field mappings to the SDL common schema
- 2–3 starter STAR detection rules
- 5 parser test assertions

No Anthropic API key required — this uses Claude Code directly.

---

### Settings

Read and write your `.env` credentials from the UI. Secret fields (API tokens, keys) are masked by default with show/hide toggle. Changes are written to the mounted `.env` file and take effect after restarting the backend:

```bash
docker-compose up -d --build backend
```

---

## Rebuilding

```bash
# Full rebuild
docker-compose up -d --build

# Backend only (after Python changes)
docker-compose up -d --build backend

# Frontend only (after HTML/JS changes)
docker-compose up -d --build frontend

# Reset the database
curl -X DELETE http://localhost:8001/api/coverage/reset
```

---

## Project layout

```
.
├── backend/
│   ├── main.py                  # FastAPI app, router registration
│   ├── db.py                    # SQLAlchemy models
│   ├── routers/
│   │   ├── coverage.py          # Parser coverage map endpoints
│   │   ├── ingest.py            # Ingest dashboard + filter simulator
│   │   ├── quality.py           # Parser quality tools
│   │   └── settings.py          # .env read/write
│   └── services/
│       ├── s1_client.py         # SentinelOne + Scalyr API client
│       └── rule_parser.py       # SDL/Sigma/STAR field extraction
├── frontend/
│   └── index.html               # Single-page app (Tailwind, vanilla JS)
├── parsers/                     # SDL parser files (volume-mounted)
├── db/
│   └── init.sql                 # Postgres init (tables created by SQLAlchemy)
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Notes

- The backend queries your **demo tenant** (`demo.sentinelone.net`) — not usea1-purple or any other tenant. Keep your `S1_BASE_URL` and `SDL_LOG_READ_KEY` pointed at the same tenant.
- Parser files in `parsers/` are read at query time, not on startup — add or update files without rebuilding.
- The filter simulator is read-only and makes no changes to your tenant configuration.
