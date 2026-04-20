"""
Calendar-based overnight charging decision.

Fetches Google Calendar events for tomorrow via private iCalendar URLs,
finds events with locations, geocodes them via Nominatim (straight-line
distance from home), and returns whether total estimated driving exceeds
the configured threshold.

Why: if tomorrow requires >DRIVING_THRESHOLD_MILES of driving, the car
must charge overnight on the TOU schedule so it isn't sitting empty in
the morning waiting for the sun.
"""

import asyncio
import logging
import math
import os
from datetime import date, datetime, timedelta

import httpx
import pytz
from icalendar import Calendar

log = logging.getLogger(__name__)

ARIZONA = pytz.timezone("America/Phoenix")

HOME_LAT              = float(os.getenv("HOME_LAT",               "33.6388"))
HOME_LON              = float(os.getenv("HOME_LON",              "-112.0740"))
DRIVING_THRESHOLD_MI  = float(os.getenv("DRIVING_THRESHOLD_MILES", "80"))


# ── Geometry ──────────────────────────────────────────────────────────────────

def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance between two lat/lon points in miles."""
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# ── Geocoding ─────────────────────────────────────────────────────────────────

async def _geocode(address: str, client: httpx.AsyncClient) -> tuple[float, float] | None:
    """
    Geocode an address via Nominatim (OpenStreetMap). Returns (lat, lon) or None.
    Nominatim requires a descriptive User-Agent and allows max 1 req/sec.
    """
    try:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "enphase-juicebox-coordinator/1.0 (home automation)"},
            timeout=10.0,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as exc:
        log.warning("[calendar_check] Geocoding failed for '%s': %s", address, exc)
    return None


# ── iCal parsing ──────────────────────────────────────────────────────────────

def _tomorrow_az() -> date:
    return (datetime.now(ARIZONA) + timedelta(days=1)).date()


def _parse_event_date(dtstart) -> date | None:
    """Extract a date from a DTSTART value (handles all-day and timed events)."""
    if dtstart is None:
        return None
    val = dtstart.dt
    if isinstance(val, datetime):
        # Timed event — convert to Arizona date
        if val.tzinfo is None:
            val = ARIZONA.localize(val)
        return val.astimezone(ARIZONA).date()
    return val  # already a date (all-day event)


async def _fetch_ical_events(url: str, target_date: date,
                              client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch an iCal feed and return events on target_date that have a location.
    """
    events = []
    try:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            event_date = _parse_event_date(component.get("DTSTART"))
            if event_date != target_date:
                continue
            location = str(component.get("LOCATION", "")).strip()
            if not location:
                continue
            events.append({
                "summary":  str(component.get("SUMMARY", "(no title)")),
                "location": location,
                "date":     str(event_date),
            })
    except Exception as exc:
        log.warning("[calendar_check] Failed to fetch/parse iCal feed: %s", exc)
    return events


# ── Main entry point ──────────────────────────────────────────────────────────

async def check_tomorrow_driving(ical_urls: list[str]) -> dict:
    """
    Check all iCal feeds for tomorrow's events with locations.

    Returns a dict with:
      overnight_charging_needed  bool
      total_miles                float   — sum of round-trip distances
      threshold_miles            float
      events_with_location       list
      reasoning                  str
    """
    tomorrow = _tomorrow_az()
    log.info("[calendar_check] Checking calendars for %s", tomorrow)

    async with httpx.AsyncClient() as client:
        # Collect all events across calendars
        all_events: list[dict] = []
        for url in ical_urls:
            fetched = await _fetch_ical_events(url, tomorrow, client)
            all_events.extend(fetched)
            log.info("[calendar_check] Feed returned %d event(s) with locations", len(fetched))

        if not all_events:
            return {
                "overnight_charging_needed": False,
                "total_miles":               0.0,
                "threshold_miles":           DRIVING_THRESHOLD_MI,
                "events_with_location":      [],
                "reasoning":                 "No events with locations found for tomorrow — surplus-only charging.",
            }

        # Geocode each location and accumulate round-trip miles
        total_miles    = 0.0
        geocoded_events: list[dict] = []

        for i, event in enumerate(all_events):
            if i > 0:
                await asyncio.sleep(1.1)  # Nominatim rate limit: 1 req/sec

            coords = await _geocode(event["location"], client)
            if coords is None:
                log.warning("[calendar_check] Could not geocode '%s' — skipping", event["location"])
                geocoded_events.append({**event, "one_way_miles": None, "round_trip_miles": None,
                                        "note": "geocoding failed"})
                continue

            one_way    = _haversine_miles(HOME_LAT, HOME_LON, coords[0], coords[1])
            round_trip = one_way * 2
            total_miles += round_trip

            log.info("[calendar_check] '%s' @ %s → %.1f mi one-way (%.1f RT)",
                     event["summary"], event["location"], one_way, round_trip)
            geocoded_events.append({
                **event,
                "one_way_miles":    round(one_way, 1),
                "round_trip_miles": round(round_trip, 1),
            })

    needed = total_miles >= DRIVING_THRESHOLD_MI
    return {
        "overnight_charging_needed": needed,
        "total_miles":               round(total_miles, 1),
        "threshold_miles":           DRIVING_THRESHOLD_MI,
        "events_with_location":      geocoded_events,
        "reasoning": (
            f"Tomorrow's estimated driving: {total_miles:.0f} mi total "
            f"({'≥' if needed else '<'} {DRIVING_THRESHOLD_MI:.0f} mi threshold) — "
            f"overnight TOU charging {'enabled' if needed else 'skipped'}."
        ),
    }
