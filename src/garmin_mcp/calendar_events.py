"""
Calendar event (race / personal event) management for Garmin Connect MCP Server.

python-garminconnect has no calendar-event support. These endpoints are
undocumented; the module reverse-engineers the flow used by the Connect web
calendar's "Add an Event" dialog:

    POST   /calendar-service/event             create; returns the event incl. id
    GET    /calendar-service/event/{id}        one event
    DELETE /calendar-service/event/{id}        delete (HTTP 204)
    GET    /calendar-service/events/upcoming   list upcoming events
    GET    /calendar-service/event/primary     the primary/goal race (404 if none)

Only ``date``, ``eventName`` and ``eventType`` are required. The server defaults
privacy to PRIVATE and ``race`` to false. Unlike the web UI, the API does not
require a geocoded location, a distance, or a future start time. All calls use
the same OAuth2 bearer as the rest of the MCP.
"""
import json
import re
from typing import Any, Dict, List, Optional

# The garmin_client will be set by the main file
garmin_client = None


def configure(client):
    """Configure the module with the Garmin client instance"""
    global garmin_client
    garmin_client = client


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_MAX_NAME_LEN = 254

# eventType keys accepted by POST /calendar-service/event (verified with live
# probes). Unknown keys make the endpoint return HTTP 500, so we reject early.
EVENT_TYPES = {
    "running",
    "trail_running",
    "cycling",
    "gravel_cycling",
    "mountain_biking",
    "swimming",
    "multi_sport",
    "hiking",
    "walking",
    "fitness_equipment",
    "winter_sports",
    "other",
}

DISTANCE_UNITS = {"mile", "kilometer", "meter", "yard"}


def _validate_date(value: str, field: str = "date") -> None:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"Invalid {field} '{value}': expected YYYY-MM-DD")


def _curate_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Curate a raw Garmin calendar event into a compact shape."""
    target = raw.get("completionTarget") or {}
    time_local = raw.get("eventTimeLocal") or {}
    customization = raw.get("eventCustomization") or {}

    curated: Dict[str, Any] = {
        "event_id": raw.get("id"),
        "name": raw.get("eventName"),
        "date": raw.get("date"),
        "event_type": raw.get("eventType"),
        "race": raw.get("race"),
        "start_time": time_local.get("startTimeHhMm"),
        "time_zone": time_local.get("timeZoneId"),
        "location": raw.get("location"),
        "course_id": raw.get("courseId"),
        "note": raw.get("note"),
        "url": raw.get("url"),
        "privacy": (raw.get("eventPrivacy") or {}).get("label"),
        "is_primary": customization.get("isPrimaryEvent"),
    }

    unit_type = target.get("unitType")
    if unit_type == "distance":
        curated["distance"] = target.get("value")
        curated["distance_unit"] = target.get("unit")
    elif unit_type == "time":
        curated["goal_time_seconds"] = target.get("value")

    return curated


def _build_event_payload(
    name: str,
    date: str,
    event_type: str,
    start_time: Optional[str],
    timezone: Optional[str],
    distance: Optional[float],
    distance_unit: str,
    goal_time_seconds: Optional[int],
    location: Optional[str],
    note: Optional[str],
    url: Optional[str],
    race: bool,
) -> Dict[str, Any]:
    """Validate inputs and construct the create-event JSON body."""
    if not name or not name.strip():
        raise ValueError("name is required")
    if len(name) > _MAX_NAME_LEN:
        raise ValueError(
            f"name exceeds the maximum length of {_MAX_NAME_LEN} characters"
        )

    _validate_date(date)

    key = (event_type or "").strip().lower()
    if key not in EVENT_TYPES:
        raise ValueError(
            f"Unknown event_type '{event_type}'. Supported: "
            f"{', '.join(sorted(EVENT_TYPES))}."
        )

    payload: Dict[str, Any] = {
        "date": date,
        "eventName": name,
        "eventType": key,
        "race": bool(race),
    }

    if start_time is not None:
        if not _TIME_RE.match(start_time):
            raise ValueError(
                f"Invalid start_time '{start_time}': expected 24-hour HH:MM"
            )
        payload["eventTimeLocal"] = {
            "startTimeHhMm": start_time,
            "timeZoneId": timezone,
        }

    if goal_time_seconds is not None and distance is not None:
        raise ValueError(
            "Pass either distance or goal_time_seconds, not both"
        )
    if goal_time_seconds is not None:
        if goal_time_seconds <= 0:
            raise ValueError("goal_time_seconds must be positive")
        payload["completionTarget"] = {
            "value": goal_time_seconds,
            "unit": "second",
            "unitType": "time",
        }
    elif distance is not None:
        if distance <= 0:
            raise ValueError("distance must be positive")
        unit = (distance_unit or "").strip().lower()
        if unit not in DISTANCE_UNITS:
            raise ValueError(
                f"Unknown distance_unit '{distance_unit}'. Supported: "
                f"{', '.join(sorted(DISTANCE_UNITS))}."
            )
        payload["completionTarget"] = {
            "value": distance,
            "unit": unit,
            "unitType": "distance",
        }

    if location is not None:
        payload["location"] = location
    if note is not None:
        payload["note"] = note
    if url is not None:
        payload["url"] = url

    return payload


def _find_existing_event(name: str, date: str) -> Optional[Dict[str, Any]]:
    """Return an upcoming event with the same name (case-insensitive) and date.

    Used to keep create_calendar_event idempotent: the POST endpoint is not,
    so a repeated call would otherwise create a duplicate calendar entry.
    """
    try:
        events = garmin_client.client.connectapi(
            "/calendar-service/events/upcoming",
            params={"numDaysForward": 365, "limit": 100},
        )
        if not isinstance(events, list):
            return None
        wanted = name.strip().lower()
        for event in events:
            if (
                str(event.get("eventName", "")).strip().lower() == wanted
                and event.get("date") == date
            ):
                return event
    except Exception:
        # If the pre-check fails, fall through to the normal POST path so we
        # do not block a legitimate create.
        return None
    return None


def register_tools(app):
    """Register calendar event tools"""

    @app.tool()
    async def get_calendar_events(days_forward: int = 365, limit: int = 100) -> str:
        """List upcoming events on the Garmin Connect calendar.

        Covers user-created races/events and events added from Garmin's event
        database. Returns a curated list; use get_calendar_event for the full
        detail of one entry.

        Args:
            days_forward: How many days ahead to include (default 365).
            limit: Maximum number of events to return (default 100).
        """
        try:
            events = garmin_client.client.connectapi(
                "/calendar-service/events/upcoming",
                params={"numDaysForward": days_forward, "limit": limit},
            )
            if not isinstance(events, list):
                return json.dumps(events, indent=2)
            curated = [_curate_event(e) for e in events]
            return json.dumps({"count": len(curated), "events": curated}, indent=2)
        except Exception as e:
            return f"Error listing calendar events: {str(e)}"

    @app.tool()
    async def get_calendar_event(event_id: int) -> str:
        """Get one Garmin Connect calendar event by id.

        Args:
            event_id: Event id (from get_calendar_events or create_calendar_event).
        """
        try:
            raw = garmin_client.client.connectapi(
                f"/calendar-service/event/{event_id}"
            )
            return json.dumps(_curate_event(raw), indent=2)
        except Exception as e:
            return f"Error retrieving calendar event {event_id}: {str(e)}"

    @app.tool()
    async def get_primary_event() -> str:
        """Get the primary (goal) race on the Garmin Connect calendar.

        The primary event is the one race predictions and training readiness
        count down to. Returns a message if no primary event is set.
        """
        try:
            raw = garmin_client.client.connectapi(
                "/calendar-service/event/primary"
            )
            return json.dumps(_curate_event(raw), indent=2)
        except Exception as e:
            message = str(e)
            if "404" in message or "NotFound" in message:
                return json.dumps(
                    {"primary_event": None, "message": "No primary event is set."},
                    indent=2,
                )
            return f"Error retrieving primary event: {message}"

    @app.tool()
    async def create_calendar_event(
        name: str,
        date: str,
        event_type: str,
        start_time: Optional[str] = None,
        timezone: Optional[str] = None,
        distance: Optional[float] = None,
        distance_unit: str = "mile",
        goal_time_seconds: Optional[int] = None,
        location: Optional[str] = None,
        note: Optional[str] = None,
        url: Optional[str] = None,
        race: bool = False,
    ) -> str:
        """Create an event (race) on the Garmin Connect calendar.

        Idempotent: if an upcoming event with the same name and date already
        exists, it is returned unchanged and no new entry is created.

        Args:
            name: Event name (max 254 characters).
            date: Event date in YYYY-MM-DD format.
            event_type: One of running, trail_running, cycling, gravel_cycling,
                mountain_biking, swimming, multi_sport, hiking, walking,
                fitness_equipment, winter_sports, other. Use multi_sport for a
                triathlon or duathlon.
            start_time: Optional local start time as 24-hour HH:MM.
            timezone: Optional IANA time zone id for start_time
                (e.g. "America/New_York").
            distance: Optional target distance. Mutually exclusive with
                goal_time_seconds.
            distance_unit: Unit for distance: mile (default), kilometer, meter,
                or yard.
            goal_time_seconds: Optional target duration in seconds, for
                time-based events. Mutually exclusive with distance.
            location: Optional free-text location (e.g. "Boston, MA").
            note: Optional note shown on the event.
            url: Optional event website URL.
            race: Whether this event is a race (default False). Set True to mark
                it as a race rather than a general calendar entry.
        """
        try:
            payload = _build_event_payload(
                name=name,
                date=date,
                event_type=event_type,
                start_time=start_time,
                timezone=timezone,
                distance=distance,
                distance_unit=distance_unit,
                goal_time_seconds=goal_time_seconds,
                location=location,
                note=note,
                url=url,
                race=race,
            )
        except ValueError as e:
            return f"Error creating calendar event: {str(e)}"

        try:
            existing = _find_existing_event(name, date)
            if existing is not None:
                curated = _curate_event(existing)
                curated["status"] = "already_exists"
                curated["message"] = (
                    f"An event named {name!r} already exists on {date} "
                    "— no action taken"
                )
                return json.dumps(curated, indent=2)

            raw = garmin_client.client.post(
                "connectapi", "/calendar-service/event", json=payload, api=True
            )
            curated = _curate_event(raw or {})
            curated["status"] = "created"
            return json.dumps(curated, indent=2)
        except Exception as e:
            return f"Error creating calendar event: {str(e)}"

    @app.tool()
    async def delete_calendar_event(event_id: int) -> str:
        """Delete an event from the Garmin Connect calendar.

        Args:
            event_id: Event id (from get_calendar_events or create_calendar_event).
        """
        try:
            garmin_client.client.delete(
                "connectapi", f"/calendar-service/event/{event_id}"
            )
            return json.dumps(
                {
                    "status": "success",
                    "event_id": event_id,
                    "message": f"Calendar event {event_id} deleted",
                },
                indent=2,
            )
        except Exception as e:
            return f"Error deleting calendar event {event_id}: {str(e)}"

    return app
