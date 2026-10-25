from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class Post:
    id: str
    username: str
    caption: str = ""
    url: str = ""
    timestamp: str = ""
    image_urls: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    id: str
    title: str
    organization: str
    start_date: str | None = None
    start_time: str | None = None
    end_date: str | None = None
    end_time: str | None = None
    all_day: bool = False
    timezone: str = "America/Chicago"
    location: str = ""
    description: str = ""
    source_url: str = ""
    evidence: str = ""
    warnings: list[str] = field(default_factory=list)
    reviewed: bool = False
    method: str = "rules"
    source_timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def candidate_id(post_id: str, index: int = 0) -> str:
    return sha256(f"{post_id}:{index}".encode()).hexdigest()[:24]


def local_datetime(day: str, clock: str, zone: str) -> datetime:
    """Reject DST gaps and ambiguous wall times rather than shifting an event."""
    naive = datetime.combine(date.fromisoformat(day), time.fromisoformat(clock))
    if naive.tzinfo:
        raise ValueError("Enter a local time without a UTC offset.")
    tz = ZoneInfo(zone)
    possible = []
    for fold in (0, 1):
        value = naive.replace(tzinfo=tz, fold=fold)
        restored = value.astimezone(timezone.utc).astimezone(tz)
        if restored.replace(tzinfo=None) == naive:
            possible.append(value)
    if not possible:
        raise ValueError("This time falls in the daylight-saving clock change. Choose a valid time.")
    if len({value.utcoffset() for value in possible}) > 1:
        raise ValueError("This time occurs twice during the daylight-saving change. Use UTC for this event.")
    return possible[0]


def validate(candidate: Candidate) -> list[str]:
    problems = []
    if not candidate.title.strip():
        problems.append("Add an event title.")
    try:
        ZoneInfo(candidate.timezone)
        if not candidate.start_date:
            problems.append("Choose an event date.")
            return problems
        start_day = date.fromisoformat(candidate.start_date)
        end_day = date.fromisoformat(candidate.end_date or candidate.start_date)
        if candidate.all_day:
            if end_day < start_day:
                problems.append("The last day must be on or after the first day.")
        elif not candidate.start_time:
            problems.append("Choose a start time, or explicitly mark this event all-day.")
        else:
            start = local_datetime(candidate.start_date, candidate.start_time, candidate.timezone)
            if candidate.end_date and not candidate.end_time:
                problems.append("Add an end time or clear the end date.")
            if candidate.end_time:
                end = local_datetime(end_day.isoformat(), candidate.end_time, candidate.timezone)
                if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
                    problems.append("The end must be after the start. Set the end date for overnight events.")
    except (ValueError, ZoneInfoNotFoundError) as exc:
        problems.append(str(exc))
    return problems


def merge_candidates(existing: list[Candidate], incoming: list[Candidate]) -> tuple[list[Candidate], int]:
    """Preserve reviewed edits on repeated imports and collapse same-club reposts."""
    result = list(existing)
    ids = {item.id for item in result}
    def key(item):
        return (item.organization.casefold(), " ".join(item.title.casefold().split()), item.start_date, item.start_time)
    keys = {key(item) for item in result if item.start_date}
    added = 0
    for item in incoming:
        if item.id in ids or (item.start_date and key(item) in keys):
            continue
        result.append(item)
        ids.add(item.id)
        if item.start_date:
            keys.add(key(item))
        added += 1
    return result, added
