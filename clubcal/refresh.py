"""Repair previously extracted drafts without discarding review work."""
from dataclasses import asdict, fields, replace

from .classify import recap_reason
from .extract import ALL_DAY_WARNING, extract_rules
from .models import Candidate, Post

PARSER_VERSION = 4


def partition_recaps(candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    active, skipped = [], []
    for item in candidates:
        reason = recap_reason(item.description or item.evidence)
        if not item.reviewed and item.method in {"rules", "Meta AI"} and reason:
            skipped.append(item)
        else:
            active.append(item)
    return active, skipped


def refresh_drafts(candidates: list[Candidate]) -> tuple[list[Candidate], set[str]]:
    updated, changed = [], set()
    allowed = {field.name for field in fields(Candidate)}
    for stored in candidates:
        # Streamlit retains instances of the OLD dataclass across hot reloads.
        # Rebuild them with the current defaults before accessing new fields.
        original = Candidate(**{key: value for key, value in asdict(stored).items() if key in allowed})
        item = original
        if not item.reviewed:
            if item.method == "rules" and item.evidence:
                post = Post(item.id, item.organization, item.evidence, item.source_url, getattr(item, "source_timestamp", ""))
                parsed = extract_rules(post, "", item.timezone)
                if parsed:
                    extracted = parsed[0]
                    item = replace(
                        item, start_date=item.start_date or extracted.start_date,
                        start_time=item.start_time or extracted.start_time,
                        end_time=item.end_time or extracted.end_time,
                        warnings=extracted.warnings + [warning for warning in item.warnings if warning.startswith(("Image ", "OCR ", "Only ", "Some flyer ", "No image URLs"))],
                    )
            if not item.start_time:
                item = replace(item, all_day=True, end_time=None)
                if ALL_DAY_WARNING not in item.warnings:
                    item = replace(item, warnings=item.warnings + [ALL_DAY_WARNING])
        if item != original:
            changed.add(item.id)
        updated.append(item)
    return updated, changed


def name_unnamed_imports(candidates: list[Candidate], name: str) -> list[Candidate]:
    if not name.strip():
        raise ValueError("Enter the organization's name.")
    return [replace(item, organization=name.strip()) if item.organization == "Imported club" else item for item in candidates]
