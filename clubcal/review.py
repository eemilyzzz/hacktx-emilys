"""Batch approval applies the same validation and all-day defaults as the editor."""
from dataclasses import replace

from .models import Candidate, validate


def approve_selected(candidates: list[Candidate], selected: set[str]):
    updated, approved, already_approved, problems = [], [], [], {}
    for item in candidates:
        if item.id not in selected:
            updated.append(item)
            continue
        all_day = item.all_day or not item.start_time
        reviewed = replace(item, all_day=all_day,
                           start_time=None if all_day else item.start_time,
                           end_time=None if all_day else item.end_time)
        errors = validate(reviewed)
        if errors:
            problems[item.id] = {"title": item.title, "errors": errors}
            updated.append(replace(item, reviewed=False) if item.reviewed else item)
        elif item.reviewed:
            already_approved.append(item.id)
            updated.append(reviewed)
        else:
            approved.append(item.id)
            updated.append(replace(reviewed, reviewed=True))
    return updated, approved, already_approved, problems
