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
from clubcal.ingest import fetch_apify, normalize_posts, username_from_input
from clubcal.models import Candidate, Post, candidate_id, merge_candidates, validate
from clubcal.pipeline import process_post
from clubcal.refresh import PARSER_VERSION, name_unnamed_imports, partition_recaps, refresh_drafts
from clubcal.persistence import (begin_import, cached_result, finish_import, flush_library,
    initialize_library, processing_key, queue_post, render_saved_data, render_storage_status, saved_rerun)
from clubcal.review import approve_selected
from clubcal.media import enrich_from_images, source_post

load_dotenv()
ROOT = Path(__file__).parent
st.set_page_config(page_title="ClubCal · Posts to plans", page_icon="📅", layout="wide")

if "candidates" not in st.session_state:
    st.session_state.candidates = []
if "skipped_recaps" not in st.session_state:
    st.session_state.skipped_recaps = []
initialize_library()


def sync_schedule_widgets(item: Candidate):
    for prefix, value, parse in (
        ("sd", item.start_date, date.fromisoformat), ("st", item.start_time, time.fromisoformat),
        ("ed", item.end_date, date.fromisoformat), ("et", item.end_time, time.fromisoformat),
    ):
        st.session_state[f"{prefix}-{item.id}"] = parse(value) if value else None


# Preserve the current browser session when the parser is upgraded.
if st.session_state.get("parser_version", 0) < PARSER_VERSION:
    st.session_state.candidates, skipped = partition_recaps(st.session_state.candidates)
    previous = {item.id for item in st.session_state.skipped_recaps}
    st.session_state.skipped_recaps.extend(item for item in skipped if item.id not in previous)
    st.session_state.candidates, changed = refresh_drafts(st.session_state.candidates)
    for item in st.session_state.candidates:
        if item.id in changed:
            sync_schedule_widgets(item)
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
    render_storage_status()

st.markdown("# Posts become plans.")
st.markdown("Turn club flyers and Instagram captions into a calendar you can actually use.")
st.caption("COLLECT POSTS  →  READ FLYERS  →  REVIEW EVENTS  →  EXPORT YOUR CALENDAR")

all_items = st.session_state.candidates
ready = approved_events(all_items)
a, b, c, d = st.columns(4)
found_metric, ready_metric, review_metric, skipped_metric = a.empty(), b.empty(), c.empty(), d.empty()
st.divider()


def add_candidates(items: list[Candidate]):
    merged, added = merge_candidates(st.session_state.candidates, [item for item in items if item.id not in st.session_state.removed_event_ids])
    st.session_state.candidates = merged
    return added


def process(posts: list[Post], images: dict[str, list[bytes]] | None = None, source="Extraction"):
    if ai and not ai.api_key.strip():
        st.error("Add a Meta API key or switch off Meta AI to use the basic parser.")
        return
    found, detected, existing, non_events, errors, skipped_count, reused = 0, 0, 0, 0, 0, 0, 0
    deleted_ids = set()
    import_id = begin_import(source, [post.username for post in posts])
    with st.status("Reading posts…", expanded=True) as status:
        bar = st.progress(0)
        message = st.empty()
        for index, post in enumerate(posts):
            blobs = (images or {}).get(post.id)
            key = processing_key(post, timezone, ai, use_ocr, max_images)
            try:
                cached = cached_result(post, key, blobs)
                if cached is not None:
                    items, warnings = cached
                    reused += 1
                    message.caption(f"Reusing saved extraction for {post.username}")
                else:
                    items, warnings = process_post(
                        post, timezone, ai, blobs, use_ocr,
                        max_images, progress=message.caption,
                    )
                    queue_post(post, key, items, warnings, blobs)
                detected += len(items)
                deleted = {item.id for item in items if item.id in st.session_state.removed_event_ids}
                deleted_ids.update(deleted)
                added = add_candidates(items)
                found += added
                existing += len(items) - len(deleted) - added
                if not items and RECAP_REASON in warnings:
                    skipped_count += 1
                    if candidate_id(post.id) not in {item.id for item in st.session_state.skipped_recaps} | st.session_state.removed_event_ids:
                        title = next((line.strip() for line in post.caption.splitlines() if line.strip()), "Event recap")[:160]
                        st.session_state.skipped_recaps.append(Candidate(
                            candidate_id(post.id), title, post.username, description=post.caption,
                            source_url=post.url, evidence=post.caption, source_timestamp=post.timestamp,
                        ))
                elif not items:
                    non_events += 1
                for warning in warnings:
                    if warning == RECAP_REASON:
                        st.caption(f"Skipped @{post.username}: past-event recap.")
                    elif warning.startswith("Image OCR:"):
                        st.caption(f"{post.username}: {warning}")
                    else:
                        st.warning(f"@{post.username}: {warning}")
            except Exception as exc:
                errors += 1
                queue_post(post, key, [], [], blobs, failed=True)
                # Do not echo HTTP request objects or credentials into the page.
                if isinstance(exc, ValueError):
                    st.error(f"@{post.username}: {exc}")
                else:
                    st.error(f"@{post.username}: processing failed ({type(exc).__name__}). Check your connection or try basic mode.")
            bar.progress((index + 1) / len(posts))
        status.update(label=f"Detected {detected} event candidates · {found} new · {existing} already saved · {len(deleted_ids)} previously deleted",
                      state="error" if errors else "complete", expanded=bool(errors))
    st.session_state.extraction_summary = {
        "posts": len(posts), "detected": detected, "added": found, "existing": existing,
        "deleted_ids": sorted(deleted_ids), "restored": 0, "non_events": non_events,
        "recaps": skipped_count, "errors": errors, "cached": reused,
    }
    finish_import(import_id, posts=len(posts), events=found, cached=reused, skipped=skipped_count,
                  error="processing_failed" if errors else "", detected=detected,
                  existing=existing, deleted=len(deleted_ids), non_events=non_events)


def render_extraction_result():
    summary = st.session_state.get("extraction_summary")
    if not summary:
        return
    st.info(f"Detected {summary['detected']} event candidates · {summary['added']} new · {summary['existing']} already saved · {summary['restored']} restored")
    st.caption(f"{summary['cached']} saved results reused · {summary['recaps']} recaps skipped · {summary['non_events']} posts without event announcements · {summary['errors']} posts need retrying")
    deleted = set(summary["deleted_ids"]) & st.session_state.removed_event_ids
    if deleted:
        st.warning(f"{len(deleted)} detected event{'s were' if len(deleted) != 1 else ' was'} previously deleted. Restore below to add {'them' if len(deleted) != 1 else 'it'} back to Review events.")
        st.caption("Restoring keeps your saved edits and marks the events as drafts for review. No new Apify run is started.")
        if st.button(f"Restore {len(deleted)} deleted event{'s' if len(deleted) != 1 else ''}", key="restore-imported-events"):
            from clubcal.persistence import restore_imported_events
            restored = restore_imported_events(sorted(deleted))
            if restored is not None:
                for item in restored:
                    sync_schedule_widgets(item)
                summary["restored"] += len(restored)
                summary["deleted_ids"] = sorted(set(summary["deleted_ids"]) - {item.id for item in restored})
                st.rerun()
            else:
                st.error("Could not restore the events. Check the storage connection, then try Restore again.")
    if summary["restored"]:
        st.success(f"Restored {summary['restored']} event{'s as drafts' if summary['restored'] != 1 else ' as a draft'}. Open Review events to check and approve.")
    elif summary["added"] or summary["existing"]:
        st.caption("Open Review events to check and approve candidates. Re-imports preserve your saved edits.")
    elif not summary["detected"] and not summary["errors"]:
        st.warning("No event announcements were detected in these posts. Check the captions and image-reading messages above.")


collect, review, export, saved_data = st.tabs(["1 · Add posts", "2 · Review events", "3 · Export calendar", "4 · Saved data"])

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
            import_id = None
            with st.spinner("Collecting posts through Apify…"):
                progress = st.empty()
                try:
                    names = [username_from_input(name) for name in names]
                    import_id = begin_import("Apify collection", names)
                    posts = fetch_apify(names, apify_token, limit, f"{days} days", progress.caption)
                    st.session_state.collected_posts = posts
                    st.session_state.pop("extraction_summary", None)
                    for post in posts:
                        queue_post(post, "collected", [], [], failed=True)
                    finish_import(import_id, posts=len(posts))
                    if not posts:
                        st.info("No posts returned. Check the account names, public visibility, and date window.")
                    else:
                        st.success(f"Collected {len(posts)} posts. Process them below; retries reuse these posts.")
                except Exception as exc:
                    if import_id:
                        finish_import(import_id, error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
                    st.error(str(exc) if isinstance(exc, ValueError) else "Apify request failed. Check Apify Console for an existing run before fetching again.")
        if st.session_state.get("collected_posts"):
            st.caption(f"{len(st.session_state.collected_posts)} collected posts available from your saved library. Image links may need refreshing if the posts were fetched earlier.")
            if st.button("Extract events from collected posts"):
                process(st.session_state.collected_posts, source="Apify extraction")
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
                process([post], {identity: blobs}, source="Flyer / caption upload")
    else:
        st.subheader("Bring an existing Apify export")
        st.caption("Export an Instagram Post Scraper dataset as JSON. Images must have fresh Instagram CDN URLs for OCR.")
        uploaded = st.file_uploader("Apify dataset JSON", type=["json"])
        import_organization = st.text_input("Organization name if missing from JSON (optional)", placeholder="SWE")
        if st.button("Import & extract events", disabled=uploaded is None):
            try:
                posts = normalize_posts(json.loads(uploaded.getvalue()), import_organization)
                if posts:
                    process(posts, source="JSON import")
                else:
                    st.info("This dataset has no posts.")
            except (ValueError, TypeError, AttributeError) as exc:
                st.error(f"Could not import this dataset: {exc}")
    result_slot = st.container()
    st.divider()
    left, right = st.columns([1, 2])
    if left.button("Try fictional demo posts"):
        posts = normalize_posts(json.loads((ROOT / "examples" / "posts.json").read_text(encoding="utf-8")))
        flyer = ROOT / "examples" / "demo_flyer.png"
        process(posts, {"demo-flyer": [flyer.read_bytes()]} if flyer.exists() else {}, source="Demo")
    right.caption("The demo includes two captions and an image-only flyer. It uses real local OCR when image reading is enabled.")
    with result_slot:
        render_extraction_result()

with review:
    st.subheader("A quick check before your calendar fills up")
    st.caption("Compare the extracted text with the original post. Save each event to approve it for export.")
    batch_notice = st.session_state.pop("batch_approval_notice", None)
    if batch_notice:
        st.success(f"Approved {batch_notice['approved']} selected event(s). {batch_notice['already']} were already approved.")
        if batch_notice["problems"]:
            st.warning(f"{len(batch_notice['problems'])} selected event(s) still need details before approval.")
            for problem in batch_notice["problems"].values():
                st.error(f"{problem['title']}: {' '.join(problem['errors'])}")
    deleted_count = st.session_state.pop("batch_delete_notice", 0)
    if deleted_count:
        st.success(f"Deleted {deleted_count} selected event(s).")
    if any(item.organization == "Imported club" for item in st.session_state.candidates):
        with st.expander("Name imported organizations"):
            st.caption("Use this when an older JSON export did not include the account name. Only events labeled Imported club will change.")
            with st.form("name-imports"):
                import_name = st.text_input("Organization name for unnamed imports", placeholder="SWE")
                apply_name = st.form_submit_button("Apply organization name")
            if apply_name:
                try:
                    st.session_state.candidates = name_unnamed_imports(st.session_state.candidates, import_name)
                    saved_rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if st.button("Add event manually"):
        stamp = datetime.now().isoformat()
        add_candidates([Candidate(candidate_id(stamp), "New event", "My club", timezone=timezone)])
    if not st.session_state.candidates:
        st.info("Add posts or try the demo to get started.")
    else:
        # Selection belongs to the visible library and never changes approval on its own.
        selection_scope = (st.session_state.library_store.kind, st.session_state.library_store.prefix,
                           st.session_state.library_store.workspace)
        if st.session_state.get("review_selection_scope") != selection_scope:
            for key in list(st.session_state):
                if key.startswith("select-event-"):
                    st.session_state.pop(key, None)
            st.session_state.review_selection_scope = selection_scope
        select_all, clear_selection = st.columns(2)
        if select_all.button("Select all"):
            for item in st.session_state.candidates:
                st.session_state[f"select-event-{item.id}"] = True
        if clear_selection.button("Clear selection"):
            for item in st.session_state.candidates:
                st.session_state[f"select-event-{item.id}"] = False
        selected = {item.id for item in st.session_state.candidates if st.session_state.get(f"select-event-{item.id}", False)}
        approve_batch, delete_batch = st.columns(2)
        if approve_batch.button(f"Approve selected ({len(selected)})", type="primary", disabled=not selected):
            updated, approved, already, problems = approve_selected(st.session_state.candidates, selected)
            st.session_state.candidates = updated
            for identity in selected - set(problems):
                st.session_state[f"select-event-{identity}"] = False
            st.session_state.batch_approval_notice = {"approved": len(approved), "already": len(already), "problems": problems}
            saved_rerun()
        if delete_batch.button(f"Delete selected ({len(selected)})", disabled=not selected):
            st.session_state.candidates = [item for item in st.session_state.candidates if item.id not in selected]
            for identity in selected:
                st.session_state.pop(f"select-event-{identity}", None)
            st.session_state.batch_delete_notice = len(selected)
            saved_rerun()
        st.caption(f"{len(selected)} selected · Batch approval uses saved event details. Save any edits first.")
    for item in list(st.session_state.candidates):
        status = "Approved" if item.reviewed and not validate(item) else "Needs review"
        select_event, event_details = st.columns([0.07, 0.93], gap="small", vertical_alignment="top")
        select_event.checkbox(f"Select event: {item.title}", key=f"select-event-{item.id}", label_visibility="collapsed")
        with event_details.expander(f"{status} · {item.title} · {item.start_date or 'Date unknown'}", expanded=False):
            st.caption(f"{item.organization} · {item.method}")
            for warning in item.warnings:
                if warning.startswith("Image OCR:"):
                    st.caption(warning)
                else:
                    st.warning(warning)
            st.caption("Image reading: OCR text included below." if "[Image " in item.evidence else "Image reading: caption only; no image text is included in this result.")
            if item.source_url.startswith(("https://www.instagram.com/", "https://instagram.com/")):
                st.link_button("View original Instagram post", item.source_url)
                if st.button("Retry image extraction", key=f"retry-images-{item.id}", disabled=item.reviewed or not use_ocr,
                             help="Reuses images or reads existing Apify results. Does not start a new scraper run. Save edits first; approved events stay protected."):
                    retry_id = begin_import("Image retry", [item.organization])
                    try:
                        from clubcal.ingest import recover_post_media
                        post = source_post(item, st.session_state.get("collected_posts", []))
                        recovered = False
                        if not post.image_urls and apify_token.strip():
                            with st.spinner("Recovering image links from existing Apify results…"):
                                post = recover_post_media(post, apify_token)
                                recovered = True
                        elif not post.image_urls:
                            raise ValueError("Add your Apify token to recover image links from existing results, or upload the flyer.")
                        if ai and not ai.api_key.strip():
                            raise ValueError("Add a Meta API key or switch off Meta AI before retrying image extraction.")
                        with st.spinner("Reading the post images…"):
                            fresh_items, warnings = process_post(post, item.timezone, ai, use_ocr=True, max_images=max_images)
                        if not recovered and apify_token.strip() and not any("[Image " in candidate.evidence for candidate in fresh_items):
                            with st.spinner("Checking saved Apify results for replacement image links…"):
                                post = recover_post_media(post, apify_token)
                                fresh_items, warnings = process_post(post, item.timezone, ai, use_ocr=True, max_images=max_images)
                        queue_post(post, processing_key(post, item.timezone, ai, True, max_images), fresh_items, warnings)
                        queue_post(post, "collected", [], [], failed=True)
                        st.session_state.collected_posts = [post] + [saved for saved in st.session_state.get("collected_posts", []) if saved.id != post.id and saved.url != post.url]
                        fresh = next((candidate for candidate in fresh_items if candidate.id == item.id), fresh_items[0] if len(fresh_items) == 1 else None)
                        if fresh is None or "[Image " not in fresh.evidence:
                            raise ValueError("No readable image text was returned. Check the image link or upload the flyer manually.")
                        enriched = enrich_from_images(item, fresh)
                        st.session_state.candidates = [enriched if candidate.id == item.id else candidate for candidate in st.session_state.candidates]
                        sync_schedule_widgets(enriched)
                        st.session_state.pop(f"evidence-{item.id}", None)
                        finish_import(retry_id, posts=1, events=1)
                        saved_rerun()
                    except Exception as exc:
                        finish_import(retry_id, error=type(exc).__name__)
                        st.error(str(exc) if isinstance(exc, ValueError) else f"Image retry failed ({type(exc).__name__}). Try fetching fresh posts or uploading the flyer.")
            st.text_area("Caption & recognized flyer text", item.evidence, disabled=True, key=f"evidence-{item.id}")
            for prefix, value, parse in (
                ("sd", item.start_date, date.fromisoformat), ("st", item.start_time, time.fromisoformat),
                ("ed", item.end_date, date.fromisoformat), ("et", item.end_time, time.fromisoformat),
            ):
                st.session_state.setdefault(f"{prefix}-{item.id}", parse(value) if value else None)
            with st.form(f"review-{item.id}"):
                title = st.text_input("Event title", item.title)
                organization = st.text_input("Organization", item.organization)
                col1, col2 = st.columns(2)
                start_day = col1.date_input("Event date", value=None, key=f"sd-{item.id}")
                start_clock = col2.time_input("Start time", value=None, key=f"st-{item.id}")
                col3, col4 = st.columns(2)
                end_day = col3.date_input("End date / last day (optional)", value=None, key=f"ed-{item.id}")
                end_clock = col4.time_input("End time (optional)", value=None, key=f"et-{item.id}")
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
                    saved_rerun()
            if st.button("Remove candidate", key=f"remove-{item.id}"):
                st.session_state.candidates = [event for event in st.session_state.candidates if event.id != item.id]
                saved_rerun()

    if st.session_state.skipped_recaps:
        with st.expander(f"Skipped recap posts ({len(st.session_state.skipped_recaps)})"):
            st.caption("These posts describe past events. They are saved in your library and excluded from calendar exports.")
            for item in list(st.session_state.skipped_recaps):
                st.markdown(f"**{item.title}**")
                st.caption(f"{item.organization} · {RECAP_REASON}")
                st.text_area("Original post text", item.evidence, disabled=True, key=f"skipped-{item.id}")
                if st.button("Review as event", key=f"restore-recap-{item.id}"):
                    restored_item = replace(item, method="manual", reviewed=False, warnings=["Restored from skipped posts. Confirm an upcoming event date."])
                    add_candidates([restored_item])
                    st.session_state.skipped_recaps = [event for event in st.session_state.skipped_recaps if event.id != item.id]
                    saved_rerun()

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
    st.caption("Your library saves to the storage shown in the sidebar. Download a portable backup whenever you need one.")
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
            saved_rerun()
        except (TypeError, ValueError, AttributeError):
            st.error("This is not a valid ClubCal session backup.")

found_metric.metric("Events found", len(st.session_state.candidates))
ready_metric.metric("Ready to export", len(approved_events(st.session_state.candidates)))
review_metric.metric("Awaiting review", len(st.session_state.candidates) - len(approved_events(st.session_state.candidates)))
skipped_metric.metric("Skipped recaps", len(st.session_state.skipped_recaps))

with saved_data:
    render_saved_data()

if not flush_library():
    st.warning("Your latest changes have not been saved to storage. Open Saved data for details, retry saving, or download a session backup.")
