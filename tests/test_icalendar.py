"""
Tests for the iCalendar Home Assistant custom component.

HOW TO RUN
----------
1. Install dependencies (once):
       pip install pytest pytest-asyncio aiohttp

2. Place this file in a `tests/` folder next to your `custom_components/` folder:
       your_project/
           custom_components/
               icalendar/
                   __init__.py
                   const.py
           tests/
               test_icalendar.py       <- this file

3. Run from your project root:
       python -m pytest tests/ -v

HOW TESTS WORK (quick primer)
------------------------------
- Each function that starts with `test_` is one test case.
- `pytest.fixture` creates reusable objects (like a fake HA instance).
- `MagicMock` replaces real objects with fakes you control.
- `AsyncMock` does the same for async functions.
- `@pytest.mark.asyncio` is needed for any test that uses `await`.
- `assert` is how you check that things are correct — if the assertion
  fails, the test fails and pytest tells you exactly what went wrong.
"""

import sys
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ---------------------------------------------------------------------------
# Bootstrap: mock the homeassistant package so we can import our component
# without a running HA instance.
# ---------------------------------------------------------------------------

class _FakeHAView:
    """Minimal stand-in for HomeAssistantView so inheritance works."""
    requires_auth = True


_mock_http = MagicMock()
_mock_http.HomeAssistantView = _FakeHAView

sys.modules.setdefault("homeassistant", MagicMock())
sys.modules.setdefault("homeassistant.components", MagicMock())
sys.modules["homeassistant.components.http"] = _mock_http
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules.setdefault("homeassistant.helpers", MagicMock())
sys.modules.setdefault("homeassistant.helpers.typing", MagicMock())

# Now we can safely import our own code.
from custom_components.icalendar import iCalendarView  # noqa: E402


# ---------------------------------------------------------------------------
# FakeResponse — replaces aiohttp's web.Response in tests.
#
# The real web.Response expects `body` to be bytes, and when given a string
# it runs internal aiohttp conversions that crash in a test environment
# (no running event loop). FakeResponse simply stores whatever it receives
# so our tests can inspect `response.body` and `response.status` directly.
# ---------------------------------------------------------------------------

class FakeResponse:
    """A minimal web.Response stand-in that keeps body as a plain string."""

    def __init__(self, body=None, status=200, content_type=None, **kwargs):
        if isinstance(body, str):
            self.body = body
        elif isinstance(body, bytes):
            self.body = body.decode("utf-8")
        else:
            self.body = "" if body is None else str(body)
        self.status = int(status)   # HTTPStatus enums compare equal to ints


@pytest.fixture(autouse=True)
def patch_web_response():
    """
    Replace aiohttp's web.Response with FakeResponse for every test.

    `autouse=True` means this fixture runs automatically — you don't need
    to list it in every test function's parameters.

    The component imports `from aiohttp import web` and calls `web.Response(...)`,
    so patching `aiohttp.web.Response` intercepts those calls correctly.
    """
    with patch("aiohttp.web.Response", FakeResponse):
        yield  # tests run here; the patch is removed afterwards


# ---------------------------------------------------------------------------
# Shared fixtures — created fresh for every test that requests them.
# ---------------------------------------------------------------------------

@pytest.fixture
def hass():
    """A fake HomeAssistant instance with the bits our view actually uses."""
    fake_hass = MagicMock()
    fake_hass.states = MagicMock()
    fake_hass.services.async_call = AsyncMock()
    return fake_hass


@pytest.fixture
def calendars():
    """A minimal calendar config list (mirrors configuration.yaml structure)."""
    return [
        {"entity_id": "calendar.birthdays", "secret": "supersecret"}
    ]


@pytest.fixture
def view(hass, calendars):
    """An iCalendarView ready to handle requests."""
    return iCalendarView(hass, calendars, colours=None)


def make_request(secret=None):
    """Helper: build a fake aiohttp Request with an optional ?s= query param."""
    request = MagicMock()
    params = {"s": secret} if secret is not None else {}
    request.query.get = lambda key, default=None: params.get(key, default)
    return request


def make_state(friendly_name="Birthdays"):
    """Helper: build a fake HA state object for a calendar entity."""
    state = MagicMock()
    state.attributes = {"friendly_name": friendly_name}
    return state


def make_events(hass, entity_id, events):
    """
    Tell the fake hass.services.async_call to return a specific list of events.
    The view calls this service and unpacks the result, so the shape must match.
    """
    hass.services.async_call.return_value = {
        entity_id: {"events": events}
    }


# ---------------------------------------------------------------------------
# Authentication & access-control tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_secret_returns_403(view):
    """A request with no ?s= param must be rejected immediately."""
    response = await view.get(make_request(secret=None), "calendar.birthdays")
    assert response.status == 403


@pytest.mark.asyncio
async def test_wrong_secret_returns_401(view):
    """A request with the wrong secret must be rejected as unauthorized."""
    response = await view.get(make_request(secret="wrongpassword"), "calendar.birthdays")
    assert response.status == 401


@pytest.mark.asyncio
async def test_unlisted_calendar_returns_403(view):
    """
    Requesting an entity not in the `calendars:` config must be rejected,
    even with a valid secret — it was never authorized.
    """
    response = await view.get(make_request(secret="supersecret"), "calendar.unknown")
    assert response.status == 403


@pytest.mark.asyncio
async def test_non_calendar_entity_returns_403(view, hass, calendars):
    """
    Only `calendar.*` entities are allowed. A sensor or switch must be
    rejected even if it somehow passes the config check.
    """
    calendars.append({"entity_id": "sensor.temperature", "secret": "supersecret"})
    response = await view.get(make_request(secret="supersecret"), "sensor.temperature")
    assert response.status == 403


@pytest.mark.asyncio
async def test_unknown_entity_returns_404(view, hass):
    """
    If the entity_id passes all checks but doesn't exist in HA's state
    machine, we should get a 404.
    """
    hass.states.get.return_value = None
    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")
    assert response.status == 404


# ---------------------------------------------------------------------------
# iCal output tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_response_is_valid_ical(view, hass):
    """A well-formed iCal response must start and end with the right markers."""
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    assert response.body.startswith("BEGIN:VCALENDAR")
    assert response.body.endswith("END:VCALENDAR")


@pytest.mark.asyncio
async def test_calendar_name_in_output(view, hass):
    """The calendar's friendly name must appear in NAME and X-WR-CALNAME."""
    hass.states.get.return_value = make_state(friendly_name="My Birthdays")
    make_events(hass, "calendar.birthdays", [])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    assert "NAME:My Birthdays" in response.body
    assert "X-WR-CALNAME:My Birthdays" in response.body

@pytest.mark.asyncio
async def test_no_line_exceeds_75_characters(view, hass):
    """RFC 5545 requires every iCal line to be at most 75 characters.
    We use intentionally long values to force the issue."""
    hass.states.get.return_value = make_state(friendly_name="A" * 80)
    make_events(hass, "calendar.birthdays", [
        {
            "start": "2026-05-14",
            "end": "2026-05-15",
            "summary": "S" * 80,
            "description": "D" * 200,
            "location": "L" * 80,
        }
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    for line in response.body.splitlines():
        assert len(line) <= 75, f"Line too long ({len(line)} chars): {line[:80]!r}"

# ---------------------------------------------------------------------------
# All-day vs timed event tests — this is where the bug we fixed lives.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_day_event_uses_date_value_type(view, hass):
    """
    All-day events (plain date strings, no time) must produce
    DTSTART;VALUE=DATE and DTEND;VALUE=DATE — NOT a datetime with a timezone.

    This is the exact bug that caused the '00:00 to 00:00' display issue.
    """
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {"start": "2026-05-14", "end": "2026-05-15", "summary": "Arthur's Birthday"}
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    assert "DTSTART;VALUE=DATE:20260514" in response.body
    assert "DTEND;VALUE=DATE:20260515" in response.body
    assert "DTSTART:20260514T" not in response.body   # no datetime version


@pytest.mark.asyncio
async def test_timed_event_uses_utc_datetime(view, hass):
    """
    Timed events must produce a UTC datetime like DTSTART:20260514T120000Z,
    with no VALUE=DATE qualifier.
    """
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {
            "start": "2026-05-14T09:00:00-03:00",   # Sao Paulo time (UTC-3)
            "end":   "2026-05-14T10:00:00-03:00",
            "summary": "Team Meeting",
        }
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    # 09:00-03:00 == 12:00 UTC
    assert "DTSTART:20260514T120000Z" in response.body
    assert "DTEND:20260514T130000Z" in response.body
    assert "VALUE=DATE" not in response.body


# ---------------------------------------------------------------------------
# Event field tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_appears_in_event(view, hass):
    """SUMMARY must appear for events that have one."""
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {"start": "2026-06-01", "end": "2026-06-02", "summary": "Dad's Birthday"}
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")
    assert "SUMMARY:Dad's Birthday" in response.body


@pytest.mark.asyncio
async def test_description_appears_when_present(view, hass):
    """DESCRIPTION must appear when the event has one."""
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {
            "start": "2026-06-01", "end": "2026-06-02",
            "summary": "Team Offsite",
            "description": "Bring your laptop",
        }
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")
    assert "DESCRIPTION:Bring your laptop" in response.body


@pytest.mark.asyncio
async def test_description_absent_when_missing(view, hass):
    """DESCRIPTION must NOT appear when the event doesn't have one."""
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {"start": "2026-06-01", "end": "2026-06-02", "summary": "No details event"}
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")
    assert "DESCRIPTION:" not in response.body


@pytest.mark.asyncio
async def test_location_appears_when_present(view, hass):
    """LOCATION must appear when the event has one."""
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {
            "start": "2026-06-01T14:00:00Z", "end": "2026-06-01T15:00:00Z",
            "summary": "Doctor Appointment",
            "location": "Rua das Flores, 100",
        }
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")
    assert "LOCATION:Rua das Flores, 100" in response.body


@pytest.mark.asyncio
async def test_uid_is_deterministic(view, hass):
    """
    Two identical events must produce the same UID.
    This ensures calendar clients de-duplicate correctly instead of
    creating duplicates on every sync.
    """
    event = {"start": "2026-07-04", "end": "2026-07-05", "summary": "Independence Day"}

    hass.states.get.return_value = make_state()

    make_events(hass, "calendar.birthdays", [event])
    response1 = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    make_events(hass, "calendar.birthdays", [event])
    response2 = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    uid1 = next(line for line in response1.body.splitlines() if line.startswith("UID:"))
    uid2 = next(line for line in response2.body.splitlines() if line.startswith("UID:"))

    assert uid1 == uid2


@pytest.mark.asyncio
async def test_multiple_events_all_present(view, hass):
    """All events returned by HA must appear in the output as VEVENT blocks."""
    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {"start": "2026-01-01", "end": "2026-01-02", "summary": "New Year"},
        {"start": "2026-12-25", "end": "2026-12-26", "summary": "Christmas"},
    ])

    response = await view.get(make_request(secret="supersecret"), "calendar.birthdays")

    assert response.body.count("BEGIN:VEVENT") == 2
    assert "SUMMARY:New Year" in response.body
    assert "SUMMARY:Christmas" in response.body


# ---------------------------------------------------------------------------
# Colour tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_calendar_colour_in_output(hass, calendars):
    """A `colour` on the calendar config must appear as a COLOR property."""
    calendars[0]["colour"] = "teal"
    v = iCalendarView(hass, calendars, colours=None)

    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [])

    response = await v.get(make_request(secret="supersecret"), "calendar.birthdays")
    assert "COLOR:teal" in response.body


@pytest.mark.asyncio
async def test_event_colour_applied_by_summary(hass, calendars):
    """Events whose SUMMARY matches a colours config entry get a COLOR field."""
    colours = [{"name": "Arthur's Birthday", "colour": "red"}]
    v = iCalendarView(hass, calendars, colours=colours)

    hass.states.get.return_value = make_state()
    make_events(hass, "calendar.birthdays", [
        {"start": "2026-05-14", "end": "2026-05-15", "summary": "Arthur's Birthday"}
    ])

    response = await v.get(make_request(secret="supersecret"), "calendar.birthdays")
    assert "COLOR:red" in response.body