"""
Integration tests for the calendar_events module MCP tools.

Covers get_calendar_events, get_calendar_event, get_primary_event,
create_calendar_event and delete_calendar_event using FastMCP integration with a
mocked Garmin client. No real Garmin account or network access is used.
"""
import json

import pytest
from mcp.server.fastmcp import FastMCP

from garmin_mcp import calendar_events
from garmin_mcp.calendar_events import (
    _build_event_payload,
    _curate_event,
)


@pytest.fixture
def app_with_calendar(mock_garmin_client):
    """Create a FastMCP app with the calendar_events tools registered."""
    calendar_events.configure(mock_garmin_client)
    app = FastMCP("Test Calendar Events")
    app = calendar_events.register_tools(app)
    return app


def _result_text(result):
    """Extract the text payload from a FastMCP call_tool result."""
    return result[0][0].text


def _raw_event(**overrides):
    raw = {
        "id": 42,
        "eventName": "Spring 10K",
        "date": "2026-05-01",
        "eventType": "running",
        "race": True,
        "eventTimeLocal": {"startTimeHhMm": "08:00", "timeZoneId": "America/New_York"},
        "completionTarget": {"value": 10.0, "unit": "kilometer", "unitType": "distance"},
        "location": "Boston, MA, USA",
        "courseId": None,
        "note": "goal race",
        "url": "",
        "eventPrivacy": {"label": "PRIVATE", "isShareable": False, "isDiscoverable": False},
        "eventCustomization": {"isPrimaryEvent": True},
    }
    raw.update(overrides)
    return raw


# --- get_calendar_events ------------------------------------------------------

@pytest.mark.asyncio
async def test_get_calendar_events_curates_and_passes_params(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.return_value = [_raw_event()]

    result = await app_with_calendar.call_tool(
        "get_calendar_events", {"days_forward": 90, "limit": 25}
    )

    data = json.loads(_result_text(result))
    assert data["count"] == 1
    event = data["events"][0]
    assert event["event_id"] == 42
    assert event["name"] == "Spring 10K"
    assert event["distance"] == 10.0
    assert event["distance_unit"] == "kilometer"
    assert event["is_primary"] is True
    mock_garmin_client.client.connectapi.assert_called_once_with(
        "/calendar-service/events/upcoming",
        params={"numDaysForward": 90, "limit": 25},
    )


@pytest.mark.asyncio
async def test_get_calendar_events_empty(app_with_calendar, mock_garmin_client):
    mock_garmin_client.client.connectapi.return_value = []

    result = await app_with_calendar.call_tool("get_calendar_events", {})

    data = json.loads(_result_text(result))
    assert data["count"] == 0
    assert data["events"] == []


@pytest.mark.asyncio
async def test_get_calendar_events_error_is_caught(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.side_effect = Exception("boom")

    result = await app_with_calendar.call_tool("get_calendar_events", {})

    assert "Error listing calendar events" in _result_text(result)


# --- get_calendar_event -----------------------------------------------------

@pytest.mark.asyncio
async def test_get_calendar_event_curates_one(app_with_calendar, mock_garmin_client):
    mock_garmin_client.client.connectapi.return_value = _raw_event(
        completionTarget={"value": 21600.0, "unit": "second", "unitType": "time"}
    )

    result = await app_with_calendar.call_tool(
        "get_calendar_event", {"event_id": 42}
    )

    data = json.loads(_result_text(result))
    assert data["event_id"] == 42
    assert data["goal_time_seconds"] == 21600.0
    assert "distance" not in data
    mock_garmin_client.client.connectapi.assert_called_once_with(
        "/calendar-service/event/42"
    )


@pytest.mark.asyncio
async def test_get_calendar_event_error_is_caught(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.side_effect = Exception("nope")

    result = await app_with_calendar.call_tool(
        "get_calendar_event", {"event_id": 7}
    )

    assert "Error retrieving calendar event 7" in _result_text(result)


# --- get_primary_event ----------------------------------------------------

@pytest.mark.asyncio
async def test_get_primary_event_returns_curated(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.return_value = _raw_event()

    result = await app_with_calendar.call_tool("get_primary_event", {})

    data = json.loads(_result_text(result))
    assert data["event_id"] == 42
    assert data["is_primary"] is True


@pytest.mark.asyncio
async def test_get_primary_event_none_set(app_with_calendar, mock_garmin_client):
    mock_garmin_client.client.connectapi.side_effect = Exception("API Error 404 - NotFoundException")

    result = await app_with_calendar.call_tool("get_primary_event", {})

    data = json.loads(_result_text(result))
    assert data["primary_event"] is None
    assert "No primary event" in data["message"]


@pytest.mark.asyncio
async def test_get_primary_event_other_error_is_caught(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.side_effect = Exception("API Error 500")

    result = await app_with_calendar.call_tool("get_primary_event", {})

    assert "Error retrieving primary event" in _result_text(result)


# --- create_calendar_event ------------------------------------------------

@pytest.mark.asyncio
async def test_create_calendar_event_happy_path(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.return_value = []  # idempotency pre-check
    mock_garmin_client.client.post.return_value = _raw_event(id=1001)

    result = await app_with_calendar.call_tool(
        "create_calendar_event",
        {
            "name": "Spring 10K",
            "date": "2026-05-01",
            "event_type": "running",
            "start_time": "08:00",
            "timezone": "America/New_York",
            "distance": 10,
            "distance_unit": "kilometer",
            "location": "Boston, MA",
            "race": True,
        },
    )

    data = json.loads(_result_text(result))
    assert data["status"] == "created"
    assert data["event_id"] == 1001

    post_call = mock_garmin_client.client.post.call_args
    assert post_call.args[0] == "connectapi"
    assert post_call.args[1] == "/calendar-service/event"
    payload = post_call.kwargs["json"]
    assert payload["date"] == "2026-05-01"
    assert payload["eventName"] == "Spring 10K"
    assert payload["eventType"] == "running"
    assert payload["race"] is True
    assert payload["eventTimeLocal"] == {
        "startTimeHhMm": "08:00",
        "timeZoneId": "America/New_York",
    }
    assert payload["completionTarget"] == {
        "value": 10,
        "unit": "kilometer",
        "unitType": "distance",
    }
    assert payload["location"] == "Boston, MA"


@pytest.mark.asyncio
async def test_create_calendar_event_defaults_race_false(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.return_value = []
    mock_garmin_client.client.post.return_value = _raw_event(id=1002, race=False)

    await app_with_calendar.call_tool(
        "create_calendar_event",
        {"name": "Weekend LR", "date": "2026-05-02", "event_type": "running"},
    )

    payload = mock_garmin_client.client.post.call_args.kwargs["json"]
    assert payload["race"] is False


@pytest.mark.asyncio
async def test_create_calendar_event_is_idempotent(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.return_value = [
        _raw_event(id=555, eventName="spring 10k", date="2026-05-01")
    ]

    result = await app_with_calendar.call_tool(
        "create_calendar_event",
        {"name": "Spring 10K", "date": "2026-05-01", "event_type": "running"},
    )

    data = json.loads(_result_text(result))
    assert data["status"] == "already_exists"
    assert data["event_id"] == 555
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_calendar_event_rejects_unknown_event_type(
    app_with_calendar, mock_garmin_client
):
    result = await app_with_calendar.call_tool(
        "create_calendar_event",
        {"name": "X", "date": "2026-05-01", "event_type": "quidditch"},
    )

    assert "Unknown event_type" in _result_text(result)
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_calendar_event_rejects_bad_date(
    app_with_calendar, mock_garmin_client
):
    result = await app_with_calendar.call_tool(
        "create_calendar_event",
        {"name": "X", "date": "05/01/2026", "event_type": "running"},
    )

    assert "Invalid date" in _result_text(result)
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_calendar_event_rejects_bad_start_time(
    app_with_calendar, mock_garmin_client
):
    result = await app_with_calendar.call_tool(
        "create_calendar_event",
        {
            "name": "X",
            "date": "2026-05-01",
            "event_type": "running",
            "start_time": "8am",
        },
    )

    assert "Invalid start_time" in _result_text(result)
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_calendar_event_rejects_distance_and_goal_time_together(
    app_with_calendar, mock_garmin_client
):
    result = await app_with_calendar.call_tool(
        "create_calendar_event",
        {
            "name": "X",
            "date": "2026-05-01",
            "event_type": "running",
            "distance": 10,
            "goal_time_seconds": 3600,
        },
    )

    assert "not both" in _result_text(result)
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_calendar_event_error_is_caught(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.connectapi.return_value = []
    mock_garmin_client.client.post.side_effect = Exception("API Error 500")

    result = await app_with_calendar.call_tool(
        "create_calendar_event",
        {"name": "X", "date": "2026-05-01", "event_type": "running"},
    )

    assert "Error creating calendar event" in _result_text(result)


# --- delete_calendar_event ----------------------------------------------------

@pytest.mark.asyncio
async def test_delete_calendar_event_success(app_with_calendar, mock_garmin_client):
    result = await app_with_calendar.call_tool(
        "delete_calendar_event", {"event_id": 999}
    )

    data = json.loads(_result_text(result))
    assert data["status"] == "success"
    assert data["event_id"] == 999
    mock_garmin_client.client.delete.assert_called_once_with(
        "connectapi", "/calendar-service/event/999"
    )


@pytest.mark.asyncio
async def test_delete_calendar_event_error_is_caught(
    app_with_calendar, mock_garmin_client
):
    mock_garmin_client.client.delete.side_effect = Exception("nope")

    result = await app_with_calendar.call_tool(
        "delete_calendar_event", {"event_id": 999}
    )

    assert "Error deleting calendar event 999" in _result_text(result)


# --- pure helpers -----------------------------------------------------------

def test_build_event_payload_minimal():
    payload = _build_event_payload(
        name="Local 5K",
        date="2026-06-15",
        event_type="Running",
        start_time=None,
        timezone=None,
        distance=None,
        distance_unit="mile",
        goal_time_seconds=None,
        location=None,
        note=None,
        url=None,
        race=False,
    )
    assert payload == {
        "date": "2026-06-15",
        "eventName": "Local 5K",
        "eventType": "running",
        "race": False,
    }


def test_build_event_payload_time_based_target():
    payload = _build_event_payload(
        name="6-Hour Ultra",
        date="2026-06-15",
        event_type="running",
        start_time="07:30",
        timezone=None,
        distance=None,
        distance_unit="mile",
        goal_time_seconds=21600,
        location=None,
        note=None,
        url=None,
        race=True,
    )
    assert payload["completionTarget"] == {
        "value": 21600,
        "unit": "second",
        "unitType": "time",
    }
    assert payload["eventTimeLocal"] == {
        "startTimeHhMm": "07:30",
        "timeZoneId": None,
    }


def test_build_event_payload_rejects_long_name():
    with pytest.raises(ValueError):
        _build_event_payload(
            name="x" * 255,
            date="2026-06-15",
            event_type="running",
            start_time=None,
            timezone=None,
            distance=None,
            distance_unit="mile",
            goal_time_seconds=None,
            location=None,
            note=None,
            url=None,
            race=True,
        )


def test_build_event_payload_rejects_unknown_distance_unit():
    with pytest.raises(ValueError):
        _build_event_payload(
            name="X",
            date="2026-06-15",
            event_type="running",
            start_time=None,
            timezone=None,
            distance=5,
            distance_unit="furlong",
            goal_time_seconds=None,
            location=None,
            note=None,
            url=None,
            race=True,
        )


def test_curate_event_distance_target():
    curated = _curate_event(_raw_event())
    assert curated["distance"] == 10.0
    assert curated["distance_unit"] == "kilometer"
    assert curated["start_time"] == "08:00"
    assert curated["privacy"] == "PRIVATE"


def test_curate_event_handles_missing_nested_objects():
    curated = _curate_event({"id": 1, "eventName": "Bare", "date": "2026-01-01"})
    assert curated["event_id"] == 1
    assert curated["start_time"] is None
    assert curated["privacy"] is None
    assert "distance" not in curated
    assert "goal_time_seconds" not in curated
