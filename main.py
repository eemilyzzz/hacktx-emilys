"""Run with: python -m streamlit run main.py"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, datetime, time
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st
from dotenv import load_dotenv

from clubcal.classify import RECAP_REASON
from clubcal.export import approved_events, export_csv, export_ics
from clubcal.extract import AISettings
from clubcal.ingest import fetch_apify, normalize_posts
from clubcal.models import Candidate, Post, candidate_id, merge_candidates, validate
from clubcal.pipeline import process_post
from clubcal.refresh import PARSER_VERSION, name_unnamed_imports, partition_recaps, refresh_drafts

load_dotenv()
ROOT = Path(__file__).parent
st.set_page_config(page_title="ClubCal · Posts to plans", page_icon="📅", layout="wide")

if "candidates" not in st.session_state:
    st.session_state.candidates = []
if "skipped_recaps" not in st.session_state:
    st.session_state.skipped_recaps = []

# Preserve the current browser session when the parser is upgraded.
if st.session_state.get("parser_version", 0) < PARSER_VERSION:
    st.session_state.candidates, skipped = partition_recaps(st.session_state.candidates)
    previous = {item.id for item in st.session_state.skipped_recaps}
    st.session_state.skipped_recaps.extend(item for item in skipped if item.id not in previous)
    st.session_state.candidates, changed = refresh_drafts(st.session_state.candidates)
    for identity in changed:
        for prefix in ("sd", "st", "ed", "et"):
            st.session_state.pop(f"{prefix}-{identity}", None)
    st.session_state.parser_version = PARSER_VERSION

with st.sidebar:
    st.markdown("## 📅 ClubCal")
    st.caption("FROM YOUR FEED TO YOUR WEEK")
    st.divider()
    timezone = st.text_input("Campus timezone", os.getenv("DEFAULT_TIMEZONE", "America/Chicago"))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        st.error("Use an IANA timezone, such as America/Chicago or Europe/London.")
        st.stop()
    apify_token = st.text_input("Apify token", os.getenv("APIFY_TOKEN", ""), type="password")
    if st.button("Test Apify connection", help="Checks your token and connection without starting a scraper run."):
        try:
            from clubcal.ingest import check_apify_connection
            check_apify_connection(apify_token)
            st.success("Connected to Apify. Token accepted; no scraper run was started.")
        except ImportError:
            st.error("Restart ClubCal to load the updated Apify connection checks.")
        except ValueError as exc:
            st.error(str(exc))
    use_ai = st.toggle("Interpret with Meta AI", value=bool(os.getenv("META_API_KEY")))
    ai = None
    if use_ai:
        meta_key = st.text_input("Meta AI API key", os.getenv("META_API_KEY", ""), type="password")
        choices = ["https://api.meta.ai/v1", "https://api.llama.com/v1"]
        default_base = os.getenv("META_BASE_URL", choices[0])
        base_url = st.selectbox("Meta endpoint", choices, index=choices.index(default_base) if default_base in choices else 0)
        model = st.text_input("Model from your Meta dashboard", os.getenv("META_MODEL", "muse-spark-1.3"))
        ai = AISettings(meta_key, base_url, model)
        st.caption("The caption and OCR text are sent to Meta for interpretation. Images stay local.")
    else:
        st.caption("Basic mode uses local OCR and a conservative date parser. No AI key needed.")
    use_ocr = st.checkbox("Read text from images", value=True)
    max_images = st.slider("Max carousel images per post", 1, 20, 10)
    st.divider()
    st.caption("Keys stay in this local app session. To remember them, copy .env.example to .env.")
    st.caption("This version collects posts when you ask. It does not monitor accounts in the background.")

st.markdown("# Posts become plans.")
st.markdown("Turn club flyers and Instagram captions into a calendar you can actually use.")
st.caption("COLLECT POSTS  →  READ FLYERS  →  REVIEW EVENTS  →  EXPORT YOUR CALENDAR")

all_items = st.session_state.candidates
ready = approved_events(all_items)
a, b, c, d = st.columns(4)
found_metric, ready_metric, review_metric, skipped_metric = a.empty(), b.empty(), c.empty(), d.empty()
st.divider()


def add_candidates(items: list[Candidate]):
    merged, added = merge_candidates(st.session_state.candidates, items)
    st.session_state.candidates = merged
    return added


def process(posts: list[Post], images: dict[str, list[bytes]] | None = None):
    if ai and not ai.api_key.strip():
        st.error("Add a Meta API key or switch off Meta AI to use the basic parser.")
        return
    found, errors, skipped_count = 0, 0, 0
    with st.status("Reading posts…", expanded=True) as status:
        bar = st.progress(0)
        message = st.empty()
        for index, post in enumerate(posts):
            try:
                items, warnings = process_post(
                    post, timezone, ai, (images or {}).get(post.id), use_ocr,
                    max_images, progress=message.caption,
                )
                found += add_candidates(items)
                if not items and RECAP_REASON in warnings:
                    skipped_count += 1
                    if candidate_id(post.id) not in {item.id for item in st.session_state.skipped_recaps}:
                        title = next((line.strip() for line in post.caption.splitlines() if line.strip()), "Event recap")[:160]
                        st.session_state.skipped_recaps.append(Candidate(
                            candidate_id(post.id), title, post.username, description=post.caption,
                            source_url=post.url, evidence=post.caption, source_timestamp=post.timestamp,
                        ))
                for warning in warnings:
                    if warning == RECAP_REASON:
                        st.caption(f"Skipped @{post.username}: past-event recap.")
                    else:
                        st.warning(f"@{post.username}: {warning}")
            except Exception as exc:
                errors += 1
                # Do not echo HTTP request objects or credentials into the page.
                if isinstance(exc, ValueError):
                    st.error(f"@{post.username}: {exc}")
                else:
                    st.error(f"@{post.username}: processing failed ({type(exc).__name__}). Check your connection or try basic mode.")
            bar.progress((index + 1) / len(posts))
        status.update(label=f"Added {found} new event candidates · {skipped_count} recaps skipped · {errors} posts need retrying", state="error" if errors else "complete")
    st.info("Open Review events to check and approve candidates. Re-imports preserve your saved edits.")


collect, review, export = st.tabs(["1 · Add posts", "2 · Review events", "3 · Export calendar"])

with collect:
    mode = st.radio("Choose a source", ["Instagram accounts", "Upload flyer / caption", "Import Apify JSON"], horizontal=True)
    if mode == "Instagram accounts":
        st.subheader("Keep up with your clubs")
        accounts = st.text_area("Instagram usernames or profile URLs, one per line", placeholder="@your_campus_club\nhttps://www.instagram.com/another_club/")
        left, right = st.columns(2)
        limit = left.number_input("Recent posts per account", 1, 30, 5)
        days = right.number_input("Look back this many days", 1, 365, 30)
        st.caption("Public posts only · Up to 10 accounts · Fetching starts a billable Apify run using your token.")
        if st.button("Fetch public posts", type="primary"):
            names = [value.strip() for value in accounts.splitlines() if value.strip()]
            with st.spinner("Collecting posts through Apify…"):
                progress = st.empty()
                try:
                    posts = fetch_apify(names, apify_token, limit, f"{days} days", progress.caption)
                    st.session_state.collected_posts = posts
                    if not posts:
                        st.info("No posts returned. Check the account names, public visibility, and date window.")
                    else:
                        st.success(f"Collected {len(posts)} posts. Process them below; retries reuse these posts.")
                except Exception as exc:
                    st.error(str(exc) if isinstance(exc, ValueError) else "Apify request failed. Check Apify Console for an existing run before fetching again.")
        if st.session_state.get("collected_posts"):
            st.caption(f"{len(st.session_state.collected_posts)} collected posts available in this session.")
            if st.button("Extract events from collected posts"):
                process(st.session_state.collected_posts)
    elif mode == "Upload flyer / caption":
        st.subheader("A flyer is all you need")
        with st.form("upload"):
            org = st.text_input("Club / organization", placeholder="Campus Design Club")
            caption = st.text_area("Post caption", placeholder="Paste the caption here. You can also extract from an image alone.")
            files = st.file_uploader("Flyer or carousel images", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
            col1, col2 = st.columns(2)
            posted = col1.date_input("Post publication date (helps interpret ‘tomorrow’)", value=None)
            source = col2.text_input("Original Instagram post URL (optional)")
            submitted = st.form_submit_button("Read flyer & find events", type="primary")
        if submitted:
            if not caption.strip() and not files:
                st.error("Add an image or a caption first.")
            else:
                blobs = [file.getvalue() for file in files]
                identity = sha256((org + caption + source + str(posted)).encode() + b"".join(blobs)).hexdigest()
                post = Post(identity, org.strip() or "My club", caption, source, posted.isoformat() if posted else "")
                process([post], {identity: blobs})
    else:
        st.subheader("Bring an existing Apify export")
        st.caption("Export an Instagram Post Scraper dataset as JSON. Images must have fresh Instagram CDN URLs for OCR.")
        uploaded = st.file_uploader("Apify dataset JSON", type=["json"])
        import_organization = st.text_input("Organization name if missing from JSON (optional)", placeholder="SWE")
        if st.button("Import & extract events", disabled=uploaded is None):
            try:
                posts = normalize_posts(json.loads(uploaded.getvalue()), import_organization)
                if posts:
                    process(posts)
                else:
                    st.info("This dataset has no posts.")
            except (ValueError, TypeError, AttributeError) as exc:
                st.error(f"Could not import this dataset: {exc}")
    st.divider()
    left, right = st.columns([1, 2])
    if left.button("Try fictional demo posts"):
        posts = normalize_posts(json.loads((ROOT / "examples" / "posts.json").read_text(encoding="utf-8")))
        flyer = ROOT / "examples" / "demo_flyer.png"
        process(posts, {"demo-flyer": [flyer.read_bytes()]} if flyer.exists() else {})
    right.caption("The demo includes two captions and an image-only flyer. It uses real local OCR when image reading is enabled.")

with review:
    st.subheader("A quick check before your calendar fills up")
    st.caption("Compare the extracted text with the original post. Save each event to approve it for export.")
    if any(item.organization == "Imported club" for item in st.session_state.candidates):
        with st.expander("Name imported organizations"):
            st.caption("Use this when an older JSON export did not include the account name. Only events labeled Imported club will change.")
            with st.form("name-imports"):
                import_name = st.text_input("Organization name for unnamed imports", placeholder="SWE")
                apply_name = st.form_submit_button("Apply organization name")
            if apply_name:
                try:
                    st.session_state.candidates = name_unnamed_imports(st.session_state.candidates, import_name)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if st.button("Add event manually"):
        stamp = datetime.now().isoformat()
        add_candidates([Candidate(candidate_id(stamp), "New event", "My club", timezone=timezone)])
    if not st.session_state.candidates:
        st.info("Add posts or try the demo to get started.")
    for item in list(st.session_state.candidates):
        status = "Approved" if item.reviewed and not validate(item) else "Needs review"
        with st.expander(f"{status} · {item.title} · {item.start_date or 'Date unknown'}", expanded=False):
            st.caption(f"{item.organization} · {item.method}")
            for warning in item.warnings:
                st.warning(warning)
            if item.source_url.startswith(("https://www.instagram.com/", "https://instagram.com/")):
                st.link_button("View original Instagram post", item.source_url)
            st.text_area("Caption & recognized flyer text", item.evidence, disabled=True, key=f"evidence-{item.id}")
            with st.form(f"review-{item.id}"):
                title = st.text_input("Event title", item.title)
                organization = st.text_input("Organization", item.organization)
                col1, col2 = st.columns(2)
                start_day = col1.date_input("Event date", date.fromisoformat(item.start_date) if item.start_date else None, key=f"sd-{item.id}")
                start_clock = col2.time_input("Start time", time.fromisoformat(item.start_time) if item.start_time else None, key=f"st-{item.id}")
                col3, col4 = st.columns(2)
                end_day = col3.date_input("End date / last day (optional)", date.fromisoformat(item.end_date) if item.end_date else None, key=f"ed-{item.id}")
                end_clock = col4.time_input("End time (optional)", time.fromisoformat(item.end_time) if item.end_time else None, key=f"et-{item.id}")
                all_day = st.checkbox("All-day event", item.all_day)
                st.caption("No start time? This event will be saved as all-day. To set a specific start time, turn off All-day event.")
                event_zone = st.text_input("Event timezone", item.timezone)
                location = st.text_input("Location", item.location)
                description = st.text_area("Calendar description", item.description)
                approve = st.form_submit_button("Save & approve", type="primary")
                save = st.form_submit_button("Save draft")
            if approve or save:
                all_day = all_day or start_clock is None
                updated = replace(
                    item, title=title, organization=organization,
                    start_date=start_day.isoformat() if start_day else None,
                    start_time=start_clock.strftime("%H:%M") if start_clock and not all_day else None,
                    end_date=end_day.isoformat() if end_day else None,
                    end_time=end_clock.strftime("%H:%M") if end_clock and not all_day else None,
                    all_day=all_day, timezone=event_zone, location=location, description=description, reviewed=False,
                )
                problems = validate(updated)
                if approve and problems:
                    for problem in problems:
                        st.error(problem)
                else:
                    updated.reviewed = bool(approve)
                    st.session_state.candidates = [updated if event.id == item.id else event for event in st.session_state.candidates]
                    st.rerun()
            if st.button("Remove candidate", key=f"remove-{item.id}"):
                st.session_state.candidates = [event for event in st.session_state.candidates if event.id != item.id]
                st.rerun()

    if st.session_state.skipped_recaps:
        with st.expander(f"Skipped recap posts ({len(st.session_state.skipped_recaps)})"):
            st.caption("These posts describe past events. They are kept here for this session and excluded from calendar exports.")
            for item in list(st.session_state.skipped_recaps):
                st.markdown(f"**{item.title}**")
                st.caption(f"{item.organization} · {RECAP_REASON}")
                st.text_area("Original post text", item.evidence, disabled=True, key=f"skipped-{item.id}")
                if st.button("Review as event", key=f"restore-recap-{item.id}"):
                    restored_item = replace(item, method="manual", reviewed=False, warnings=["Restored from skipped posts. Confirm an upcoming event date."])
                    add_candidates([restored_item])
                    st.session_state.skipped_recaps = [event for event in st.session_state.skipped_recaps if event.id != item.id]
                    st.rerun()

with export:
    st.subheader("Make room for something good")
    ready = approved_events(st.session_state.candidates)
    st.write(f"{len(ready)} approved events are ready to go.")
    st.caption("Only saved, approved events are exported. Submit any pending edits in Review events first.")
    calendar_name = st.text_input("Calendar name", "My club events")
    left, right = st.columns(2)
    left.download_button("Download calendar · .ics", export_ics(ready, calendar_name), "club-events.ics", "text/calendar", disabled=not ready, type="primary")
    right.download_button("Download Notion database · .csv", export_csv(ready).encode("utf-8-sig"), "club-events.csv", "text/csv", disabled=not ready)
    st.markdown("**Google Calendar:** On desktop, open Settings → Import & export → Import, select the `.ics` file, then choose a calendar.")
    st.markdown("**Notion Calendar:** Connect the Google account containing the imported calendar and enable that calendar.")
    st.markdown("**Notion database:** Import the `.csv` file, then set the Start column to a Date property and add a calendar view. Map the End column separately if needed.")
    st.info("File import is a snapshot. Fetching again and downloading another file does not sync changes or cancellations automatically. Repeated imports may create duplicates in the destination.")
    st.divider()
    st.markdown("**Save your review session**")
    st.caption("Your list lives in this browser session. Download a backup before restarting the app.")
    backup = json.dumps([event.to_dict() for event in st.session_state.candidates], indent=2)
    st.download_button("Download session backup", backup, "clubcal-session.json", "application/json")
    restored = st.file_uploader("Restore a ClubCal session backup", type=["json"], key="restore")
    if st.button("Restore candidates", disabled=restored is None):
        try:
            rows = json.loads(restored.getvalue())
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError("Expected an array of at most 1,000 events.")
            items = [Candidate(**row) for row in rows]
            for event in items:
                if not isinstance(event.id, str) or not isinstance(event.title, str):
                    raise ValueError("Invalid event identifier or title.")
                # Dates must be parseable even for unapproved drafts to render the editor.
                for value in (event.start_date, event.end_date):
                    if value:
                        date.fromisoformat(value)
                for value in (event.start_time, event.end_time):
                    if value:
                        time.fromisoformat(value)
                if event.reviewed and validate(event):
                    event.reviewed = False
            add_candidates(items)
            st.session_state.parser_version = 0
            st.rerun()
        except (TypeError, ValueError, AttributeError):
            st.error("This is not a valid ClubCal session backup.")

found_metric.metric("Events found", len(st.session_state.candidates))
ready_metric.metric("Ready to export", len(approved_events(st.session_state.candidates)))
review_metric.metric("Awaiting review", len(st.session_state.candidates) - len(approved_events(st.session_state.candidates)))
skipped_metric.metric("Skipped recaps", len(st.session_state.skipped_recaps))
