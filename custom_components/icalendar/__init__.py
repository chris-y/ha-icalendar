"""Export calendar domain entity state via iCalendar using the API."""

import logging

from typing import Optional
from html import escape
from http import HTTPStatus

from aiohttp import web
from datetime import datetime, timezone, timedelta
import hashlib

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, CONTENT_TYPE_ICAL


_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the iCalendar component."""
    colours = None
    calendars = None

    for name, value in config[DOMAIN].items():
        if name == "colours":
            colours = value
        # Find the secret from the config file
        if name == "calendars":
            calendars = value

    # Register the iCalendar HTTP view
    if calendars is not None:
        hass.http.register_view(iCalendarView(hass, calendars, colours))
        return True

    return False


class iCalendarView(HomeAssistantView):
    """Define the iCalendar view."""

    name = f"{DOMAIN}"
    url = "/api/ics/{entity_id}"

    def __init__(self, hass: HomeAssistant, calendars: dict, colours: Optional[dict]) -> None:
        """Initialize the iCalendar view."""
        self.hass = hass
        self.calendars = calendars
        self.colours = colours
        self.requires_auth = False

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        """Handle an iCalendar view request."""
        # Forbid empty secrets
        if request.query.get("s") is None:
            _LOGGER.error("Request was sent for entity '%s' without secret", entity_id)
            return web.Response(body="403: Forbidden", status=HTTPStatus.FORBIDDEN)

        # Find the calendar in config. Should be defined as per below or it will get denied.
        # calendars:
        #   - entity_id: calendar.entity
        #     secret: secretpassword
        valid_calendar = False
        calendar_colour = None
        for cal in self.calendars:
            if (("entity_id" in cal) and (cal['entity_id'] == entity_id)) and ("secret" in cal):
                valid_calendar = True
                secret = cal['secret']
                if("colour" in cal):
                    calendar_colour = cal['colour']
                break
        
        if valid_calendar is not True:
            _LOGGER.error("Request was sent for entity '%s' which is not allowed by config", entity_id)
            return web.Response(body="403: Forbidden", status=HTTPStatus.FORBIDDEN)

        # Only return anything with the secret supplied
        if str(request.query.get("s")) != str(secret):
            _LOGGER.error(
                "Request was sent for entity '%s' with invalid secret", entity_id
            )
            return web.Response(
                body="401: Unauthorized", status=HTTPStatus.UNAUTHORIZED
            )

        # Only return calendars
        if not entity_id.startswith("calendar."):
            _LOGGER.error("Entity '%s' is not a calendar", entity_id)
            return web.Response(body="403: Forbidden", status=HTTPStatus.FORBIDDEN)

        # Check if the calendar entity exists
        self._state = self.hass.states.get(entity_id)
        if self._state is None:
            _LOGGER.error("Entity '%s' could not be found", entity_id)
            return web.Response(body="404: Not Found", status=HTTPStatus.NOT_FOUND)

        # Calculate the start and end timeframe for our calendar
        # We output 4 weeks history and 52 weeks into the future
        start = (datetime.now() - timedelta(weeks=4)).strftime("%Y-%m-%d %H:%M:%S")
        end = (datetime.now() + timedelta(weeks=52)).strftime("%Y-%m-%d %H:%M:%S")

        events = await self.hass.services.async_call('calendar', 'get_events',
              { "entity_id": entity_id,
                "start_date_time": start,
                "end_date_time": end
              }, blocking=True, return_response=True)

        if(events is None) or (entity_id not in events):
            _LOGGER.error("Entity '%s' has no events", entity_id)
            return web.Response(body="404: Not Found", status=HTTPStatus.NOT_FOUND)

        events = events[entity_id]['events']

        # Craft the iCalendar response
        response = "BEGIN:VCALENDAR\n"
        response += "VERSION:2.0\n"
        response += "PRODID:-//Home Assistant//iCal Subscription 1.1//EN\n"
        response += "CALSCALE:GREGORIAN\n"
        response += "METHOD:PUBLISH\n"
        response += f"ORGANIZER;CN=\"{escape(self._state.attributes['friendly_name'])}\":MAILTO:{entity_id}@homeassistant.local\n"
        response += f"NAME:{escape(self._state.attributes['friendly_name'])}\n"
        response += f"X-WR-CALNAME:{escape(self._state.attributes['friendly_name'])}\n"
        if calendar_colour is not None:
            response += f"COLOR:{calendar_colour}\n"

        # Generate the variables
        entity_id = escape(entity_id)
        dtstamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # Iterate through all the events
        for e in events:
            event_start = e["start"]
            event_end = e["end"]

            # Home Assistant returns all-day events as YYYY-MM-DD.
            # Export those as RFC 5545 DATE values to avoid timezone conversion.
            is_all_day = (
                isinstance(event_start, str)
                and isinstance(event_end, str)
                and len(event_start) == 10
                and len(event_end) == 10
                and "T" not in event_start
                and "T" not in event_end
            )

            if is_all_day:
                start = datetime.strptime(
                    event_start, "%Y-%m-%d"
                ).strftime("%Y%m%d")
                end = datetime.strptime(
                    event_end, "%Y-%m-%d"
                ).strftime("%Y%m%d")
            else:
                start = datetime.fromisoformat(
                    event_start
                ).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                end = datetime.fromisoformat(
                    event_end
                ).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

            # Create and hash the UID
            if ("summary" in e and e["summary"] is not None):
                summary = escape(e['summary'])
            else:
                summary = None

            uid = f"{entity_id}-{start}-{end}-{summary}"
            uid = hashlib.sha256(uid.encode('utf-8')).hexdigest()

            response += "BEGIN:VEVENT\n"

            response += f"UID:{uid}\n"
            response += f"DTSTAMP:{dtstamp}\n"
            if is_all_day:
                response += f"DTSTART;VALUE=DATE:{start}\n"
                response += f"DTEND;VALUE=DATE:{end}\n"
            else:
                response += f"DTSTART:{start}\n"
                response += f"DTEND:{end}\n"

            # Add available optional attributes to the iCalendar response
            if summary is not None:
                response += f"SUMMARY:{summary.replace('\n', '\n ').rstrip()}\n"

            if (
                "description" in e
                and e["description"] is not None
            ):
                response += (
                    f"DESCRIPTION:{escape(e['description']).replace('\n', '\n ').rstrip()}\n"
                )

            if (
                "location" in e
                and e["location"] is not None
            ):
                response += f"LOCATION:{escape(e['location']).replace('\n', '\n ').rstrip()}\n"

            # Set colour for event, defined in config as per below:
            # colours:
            #   - name: "Calendar Event Summary"
            #     colour: css3 colour name
            for c in self.colours:
                if ("name" in c) and (c['name'] == summary):
                    response += f"COLOR:{c['colour']}\n"

            # Finish up this calendar entry
            response += "END:VEVENT\n"

        # Finish up the iCalendar response
        response += "END:VCALENDAR"

        # Return the iCalendar response
        return web.Response(body=response, content_type=CONTENT_TYPE_ICAL)
