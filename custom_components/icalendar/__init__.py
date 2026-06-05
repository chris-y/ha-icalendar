"""Export calendar domain entity state via iCalendar using the API."""

import logging

from typing import Optional
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

def fold_line(line: str) -> str:
    """
    Fold a long iCal line per RFC 5545 section 3.1.
    Lines over 75 chars are split with a newline + leading space,
    which calendar clients automatically unfold when reading.
    """
    if len(line) <= 75:
        return line
    chunks = []
    while len(line) > 75:
        chunks.append(line[:75])
        line = " " + line[75:]   # continuation lines start with a space
    chunks.append(line)
    return "\n".join(chunks)

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
        response += "PRODID:-//Home Assistant//iCal Subscription 2.0//EN\n"
        response += "CALSCALE:GREGORIAN\n"
        response += "METHOD:PUBLISH\n"
        response += fold_line(f"ORGANIZER;CN=\"{self._state.attributes['friendly_name']}\":MAILTO:{entity_id}@homeassistant.local") + "\n"
        response += fold_line(f"NAME:{self._state.attributes['friendly_name']}") + "\n"
        response += fold_line(f"X-WR-CALNAME:{self._state.attributes['friendly_name']}") + "\n"

        if calendar_colour is not None:
            response += f"COLOR:{calendar_colour}\n"

        # Generate the variables
        dtstamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # Iterate through all the events
        for e in events:
            if "T" in e["start"] or ":" in e["start"]:
                # Timed event
                start = datetime.fromisoformat(e["start"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                end = datetime.fromisoformat(e["end"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                start_prop = f"DTSTART:{start}"
                end_prop = f"DTEND:{end}"
            else:
                # All-day event
                start = datetime.strptime(e["start"], "%Y-%m-%d").strftime("%Y%m%d")
                end = datetime.strptime(e["end"], "%Y-%m-%d").strftime("%Y%m%d")
                start_prop = f"DTSTART;VALUE=DATE:{start}"
                end_prop = f"DTEND;VALUE=DATE:{end}"
            
            # Create and hash the UID
            if ("summary" in e and e["summary"] is not None):
                summary = e['summary']
            else:
                summary = None

            uid = f"{entity_id}-{start}-{end}-{summary}"
            uid = hashlib.sha256(uid.encode('utf-8')).hexdigest()

            response += "BEGIN:VEVENT\n"

            response += f"UID:{uid}\n"
            response += f"DTSTAMP:{dtstamp}\n"
            response += f"{start_prop}\n"
            response += f"{end_prop}\n"

            # Add available optional attributes to the iCalendar response
            if summary is not None:
                response += fold_line(f"SUMMARY:{summary.replace(chr(10), chr(92)+'n').replace(chr(13), '').rstrip()}") + "\n"
            if (
                "description" in e
                and e["description"] is not None
            ):
                response += fold_line(f"DESCRIPTION:{e['description'].replace(chr(10), chr(92)+'n').replace(chr(13), '').rstrip()}") + "\n"
                
            if (
                "location" in e
                and e["location"] is not None
            ):
                response += fold_line(f"LOCATION:{e['location'].replace(chr(10), chr(92)+'n').replace(chr(13), '').rstrip()}") + "\n"

            # Set colour for event, defined in config as per below:
            # colours:
            #   - name: "Calendar Event Summary"
            #     colour: css3 colour name
            if self.colours:
                for c in self.colours:
                    if ("name" in c) and (c['name'] == summary):
                        response += f"COLOR:{c['colour']}\n"

            # Finish up this calendar entry
            response += "END:VEVENT\n"

        # Finish up the iCalendar response
        response += "END:VCALENDAR"

        # Return the iCalendar response
        return web.Response(body=response, content_type=CONTENT_TYPE_ICAL)
