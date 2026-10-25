from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from io import StringIO

from icalendar import Calendar, Event

from .models import Candidate, local_datetime, validate


def approved_events(candidates: list[Candidate]) -> list[Candidate]:
    return [item for item in candidates if item.reviewed and not validate(item)]


def export_ics(candidates: list[Candidate], name: str = "ClubCal") -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//ClubCal//Instagram Events//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-calname", name)
    for item in approved_events(candidates):
        event = Event()
        event.add("uid", f"{item.id}@clubcal.local")
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("summary", item.title)
        event.add("location", item.location)
        description = f"Organization: {item.organization}\n\n{item.description}"
        if item.source_url:
            description += f"\n\nOriginal post: {item.source_url}"
            if item.source_url.startswith("https://www.instagram.com/") or item.source_url.startswith("https://instagram.com/"):
                event.add("url", item.source_url)
        event.add("description", description)
        if item.all_day:
            event.add("dtstart", date.fromisoformat(item.start_date))
            # UI end_date is inclusive; RFC 5545 all-day DTEND is exclusive.
            event.add("dtend", date.fromisoformat(item.end_date or item.start_date) + timedelta(days=1))
        else:
            start = local_datetime(item.start_date, item.start_time, item.timezone)
            # UTC serialization keeps Google Calendar DST handling unambiguous.
            event.add("dtstart", start.astimezone(timezone.utc))
            if item.end_time:
                end = local_datetime(item.end_date or item.start_date, item.end_time, item.timezone)
                event.add("dtend", end.astimezone(timezone.utc))
        calendar.add_component(event)
    return calendar.to_ical()


def export_csv(candidates: list[Candidate]) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Name", "Start", "End", "All day", "Timezone", "Location", "Organization", "Description", "Instagram"])
    def safe(value: str) -> str:
        # Keep arbitrary caption text from becoming a spreadsheet formula.
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
    for item in approved_events(candidates):
        start = item.start_date
        end = item.end_date or (item.start_date if item.all_day or item.end_time else "")
        if not item.all_day:
            start = local_datetime(item.start_date, item.start_time, item.timezone).isoformat()
            if item.end_time:
                end = local_datetime(item.end_date or item.start_date, item.end_time, item.timezone).isoformat()
        writer.writerow([safe(str(value)) for value in [item.title, start, end, item.all_day, item.timezone, item.location, item.organization, item.description, item.source_url]])
    return output.getvalue()
