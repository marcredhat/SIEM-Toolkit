from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
from services import s1_client
import re

router = APIRouter()


def _date_range_hours(hours: int) -> tuple[str, str]:
    now = datetime.utcnow()
    return (
        (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SampleEventsRequest(BaseModel):
    source: str
    limit: int = 20
    hours: int = 1


class FieldPopulationRequest(BaseModel):
    source: str
    hours: int = 24
    fields: list[str] = [
        "src.ip",
        "src.port",
        "dst.ip",
        "dst.port",
        "user.name",
        "event.type",
        "src.process.name",
        "src.process.cmdline",
        "tgt.file.path",
        "network.direction",
        "dataSource.name",
    ]


class TestParserRequest(BaseModel):
    parser_name: str
    log_line: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten_event(event: dict) -> dict:
    """Return a flat field→value dict from a PowerQuery result row."""
    if isinstance(event, dict):
        return {k: v for k, v in event.items()}
    return {}


def _extract_format_strings(content: str) -> list[str]:
    """
    Extract SDL format string values from augmented-JSON parser content.
    Matches:  "format": "..."  (double-quoted value, supports escaped quotes).
    """
    pattern = re.compile(r'"format"\s*:\s*"((?:[^"\\]|\\.)*)"')
    return pattern.findall(content)


def _sdl_format_to_regex(fmt: str) -> tuple[re.Pattern, dict[str, str]]:
    """
    Convert an SDL format string to a compiled Python regex.

    Returns (compiled_pattern, py_group_to_sdl_field) mapping so callers can
    translate group names back to the original SDL field names.

    Raises re.error if the resulting pattern cannot be compiled.
    """
    # Split on $...$ tokens
    token_pattern = re.compile(r'\$([^$]+)\$')
    parts = token_pattern.split(fmt)
    # parts alternates: literal, token, literal, token, ...

    regex_parts: list[str] = []
    py_group_to_sdl: dict[str, str] = {}
    seen_groups: dict[str, int] = {}

    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Literal text
            regex_parts.append(re.escape(part))
        else:
            # Token: either "field.name=PATTERN" or just "field.name"
            if '=' in part:
                field_name, pattern = part.split('=', 1)
            else:
                field_name = part
                pattern = r'[^\s]+'

            # Build a valid Python group name
            safe = re.sub(r'[.\-]', '_', field_name)
            if safe in seen_groups:
                seen_groups[safe] += 1
                safe = f"{safe}_{seen_groups[safe]}"
            else:
                seen_groups[safe] = 0

            py_group_to_sdl[safe] = field_name
            regex_parts.append(f'(?P<{safe}>{pattern})')

    compiled = re.compile(''.join(regex_parts), re.IGNORECASE)
    return compiled, py_group_to_sdl


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/sample-events")
async def sample_events(req: SampleEventsRequest):
    """Return a sample of raw events from a given data source."""
    query = f'| filter dataSource.name = "{req.source}" | limit {req.limit}'
    from_dt, to_dt = _date_range_hours(req.hours)

    result = await s1_client.run_powerquery(query, from_dt, to_dt)

    rows = result if isinstance(result, list) else (result.get("rows") or result.get("events") or [])
    events = [_flatten_event(row) for row in rows]

    return {
        "source": req.source,
        "events": events,
        "count": len(events),
        "hours": req.hours,
    }


@router.post("/field-population")
async def field_population(req: FieldPopulationRequest):
    """
    Analyse how consistently each requested field is populated across a sample
    of events from a data source.
    """
    query = f'| filter dataSource.name = "{req.source}" | limit 500'
    from_dt, to_dt = _date_range_hours(req.hours)

    result = await s1_client.run_powerquery(query, from_dt, to_dt)

    rows = result if isinstance(result, list) else (result.get("rows") or result.get("events") or [])
    events = [_flatten_event(row) for row in rows]

    if not events:
        raise HTTPException(status_code=404, detail=f"No events found for source '{req.source}' in the last {req.hours} hours.")

    total = len(events)
    _empty = {None, "", "null"}

    # Collect all field names seen across the sample (useful for surfacing what IS there)
    all_seen_fields = sorted({k for ev in events for k in ev})

    field_stats = []
    for field in req.fields:
        # dataSource.name is always 100% — we filtered by it; Scalyr just doesn't echo it back
        if field == "dataSource.name":
            populated = total
        else:
            populated = sum(1 for ev in events if ev.get(field) not in _empty)
        rate = round((populated / total) * 100, 1)
        field_stats.append({
            "field": field,
            "populated": populated,
            "total": total,
            "rate": rate,
        })

    # Sort ascending by rate (worst coverage first)
    field_stats.sort(key=lambda x: x["rate"])

    return {
        "source": req.source,
        "total_sampled": total,
        "hours": req.hours,
        "fields": field_stats,
        "fields_seen_in_sample": all_seen_fields,
    }


@router.post("/test-parser")
async def test_parser(req: TestParserRequest):
    """
    Test a parser against a raw log line by extracting and matching SDL format
    strings found in the parser file.
    """
    parser_path = f"/app/parsers/{req.parser_name}"

    try:
        with open(parser_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Parser file not found: {req.parser_name}")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read parser file: {exc}")

    format_strings = _extract_format_strings(content)

    for fmt in format_strings:
        try:
            compiled, py_to_sdl = _sdl_format_to_regex(fmt)
        except re.error:
            # Skip unparseable format strings
            continue

        match = compiled.search(req.log_line)
        if match:
            fields = [
                {"field": py_to_sdl.get(group, group), "value": value}
                for group, value in match.groupdict().items()
                if value is not None
            ]
            return {
                "parser_name": req.parser_name,
                "matched": True,
                "format_matched": fmt,
                "fields": fields,
            }

    return {
        "parser_name": req.parser_name,
        "matched": False,
        "message": "No format pattern matched",
        "fields": [],
    }
