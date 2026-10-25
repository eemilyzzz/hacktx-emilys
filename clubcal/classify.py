"""Separate event announcements from retrospective social posts."""
import re
import unicodedata

RECAP_REASON = "Past-event recap or thank-you post; no upcoming event announcement found."

RECAP = re.compile(
    r"\b(?:recap|throwback|highlights\s+(?:from|of))\b"
    r"|\bwhat\s+an?\s+(?:\w+\s+){0,3}(?:evening|night|day|event|session|workshop)\b"
    r"|\b(?:was|were)\s+(?:held|hosted)\b"
    r"|\b(?:we|everyone)\s+(?:gathered|joined|attended|hosted|enjoyed|met|celebrated)\b"
    r"|\b(?:our|the|this)\s+(?:\w+\s+){0,5}(?:event|workshop|meeting|session|conference)\s+(?:brought|was|had)\b"
    r"|\bthank(?:s|\s+you)\b[^.!?\n]{0,100}\bfor\s+(?:joining|attending|coming|hosting|making|supporting)\b"
    r"|\b(?:last\s+(?:night|week|weekend|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|yesterday)\b",
    re.I,
)

UPCOMING = re.compile(
    r"\bsave\s+(?:the|this)\s+date\b"
    r"|\b(?:next|upcoming)\s+(?:(?:\w+[ -]){0,3})(?:event|meeting|meet[ -]?up|workshop|session|talk|social|coffee|walk)\b"
    r"|\b(?:join\s+us|see\s+you|meet\s+us)\b"
    r"|\b(?:register|rsvp|sign[ -]?up|get\s+(?:your\s+)?tickets)\b"
    r"|\bregistration\s+(?:is\s+open|opens|closes)\b"
    r"|\b(?:we\s+(?:will|are)|we're)\s+(?:be\s+)?(?:hosting|holding|meeting|gathering)\b"
    r"|\b(?:will\s+(?:be\s+held|take\s+place)|tomorrow)\b",
    re.I,
)


def recap_reason(text: str) -> str | None:
    text = unicodedata.normalize("NFKC", text)
    if RECAP.search(text) and not UPCOMING.search(text):
        return RECAP_REASON
    return None


def announcement_text(text: str) -> str:
    """Keep the future invitation in a mixed recap/announcement caption."""
    normalized = unicodedata.normalize("NFKC", text)
    future = UPCOMING.search(normalized)
    if not RECAP.search(normalized) or not future:
        return normalized
    boundaries = list(re.finditer(r"(?:[.!?;]\s+|\n+)", normalized[:future.start()]))
    start = boundaries[-1].end() if boundaries else 0
    # A single sentence can contain both 'we met' and 'join us next time'.
    if RECAP.search(normalized[start:future.start()]):
        start = future.start()
    return normalized[start:]
