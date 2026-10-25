"""Re-read a saved post's media without replacing human corrections."""
from dataclasses import replace

from .models import Candidate, Post


def source_post(candidate: Candidate, collected: list[Post]) -> Post:
    return next((post for post in collected if post.url.rstrip("/") == candidate.source_url.rstrip("/")),
                Post(candidate.source_url, candidate.organization, candidate.description,
                     candidate.source_url, candidate.source_timestamp))


def enrich_from_images(current: Candidate, fresh: Candidate) -> Candidate:
    if current.reviewed:
        return current
    return replace(current,
        start_date=current.start_date or fresh.start_date,
        start_time=current.start_time or fresh.start_time,
        end_date=current.end_date or fresh.end_date,
        end_time=current.end_time or fresh.end_time,
        all_day=current.all_day if current.start_time else fresh.all_day,
        location=current.location or fresh.location,
        evidence=fresh.evidence, warnings=fresh.warnings,
    )
