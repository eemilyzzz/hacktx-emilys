# ClubCal

Turn Instagram club posts into reviewed, exportable calendar events. Add public organization accounts, import an Apify JSON dataset, or upload event flyers and captions. Real local OCR reads images; an optional Meta AI connection interprets the text. Approve events, then download an **`.ics`** file for Google Calendar or a CSV for a Notion database.

## Run locally

Python **3.12** is the tested version. This repository includes a working local `.venv` on the current computer. Start it from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m streamlit run main.py --server.address 127.0.0.1
```

For a fresh checkout, install Python 3.12 and create the environment first:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run main.py --server.address 127.0.0.1
```

On macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run main.py --server.address 127.0.0.1
```

Open [the local app](http://localhost:8501). Click **Try fictional demo posts** to test two captions and an image-only event flyer. OCR runs on CPU using RapidOCR and ONNX Runtime; you do not need a Tesseract installation or an AI key. The installed RapidOCR wheel includes its default OCR models.

## Choose your services

| Service | Purpose | Required? |
| --- | --- | --- |
| Apify | Retrieve public posts, captions, image URLs, and carousel slides | Only for fetching accounts |
| RapidOCR | Extract text from post images on your computer | Included for image reading |
| Meta Model API / legacy Llama API | Interpret OCR and captions into event fields, including multiple events | Optional |
| Instagram Graph API | An alternative collection source with account and permission requirements | Not integrated in this version |

Instagram data access and Meta AI inference are separate services. AI credits do not grant access to arbitrary Instagram profiles. Meta's Instagram API has restrictions around professional accounts and permissions; this project uses Apify for account collection. See [Meta's Instagram API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-1ff01566-3509-48bd-a0f4-8571a91ccfdf).

### Apify setup

Create an Apify account and get an API token from its Console. Paste it into the sidebar or configure `APIFY_TOKEN` in `.env`. The app uses the Apify-maintained [`apify/instagram-post-scraper`](https://apify.com/apify/instagram-post-scraper), with its [documented username array and results limit](https://apify.com/apify/instagram-post-scraper/input-schema).

Use **Test Apify connection** to validate the token without starting a scraper. HTTP 401 means the token was rejected; HTTP 403 points to permissions or scraper access. Network failures are reported separately. Windows error 10013 means the process cannot open an outbound connection; start the app from a normal terminal with internet access, or approve a network-enabled launch in Codex. A valid token cannot overcome a process network restriction. If a run was created before an error, check Apify Console before starting another billable run.

1. Add up to 10 public club usernames, one per line.
2. Choose the recent-post limit and lookback period.
3. Click **Fetch public posts**. This starts a billable Actor run using your account.
4. Click **Extract events from collected posts**. Retries of this step reuse the collected posts and do not start a new Actor run.

The app reads image posts and carousel slides; it does not transcribe video or fetch Stories. Private accounts are unsupported. Instagram CDN links expire: fetch fresh posts or upload the flyer when an image cannot be downloaded. JSON imports accept the [Actor's output fields](https://apify.com/apify/instagram-post-scraper/output-schema), including `caption`, `displayUrl`, `images`, and `childPosts`. Account names are also read from nested `owner`, `user`, `account`, and `profile` objects; profile exports can contain `latestPosts`. If the export omits account metadata, enter an organization name with the import, or use **Name imported organizations** in Review events to fix older unnamed imports together.

### Optional Meta AI setup

You can use the app before identifying which Meta product your credits cover. Start in basic mode, then configure AI when you have an inference API key:

```powershell
Copy-Item .env.example .env
```

Edit `.env` locally; it is ignored by Git. Set `META_API_KEY`, `META_BASE_URL`, and `META_MODEL` to the values in your dashboard. The sidebar also accepts these settings.

- Current Meta Model API: `https://api.meta.ai/v1`; the configured model must be available to your account. The example uses `muse-spark-1.3`, as shown in [Meta's quickstart](https://dev.meta.ai/docs/cookbook/quickstart-chat-completions).
- Legacy Llama API: `https://api.llama.com/v1`; enter your available Llama model ID. Its response shape is supported separately; see [Meta's Llama API Python client](https://github.com/meta-llama/llama-api-python).

Only caption and recognized OCR text are sent to Meta. The model returns structured event candidates; the app validates date and time formats and requires human approval. API keys are never included in exports. Live API connectivity and your account's model entitlement must be verified with your credentials.

## Review and export

Open **Review events**, expand a candidate, compare its recognized text with the original, and correct the title, date, time, location, or timezone. Click **Save & approve**. Only valid, approved candidates appear in exports.

- Events without a specific start time default to all-day. You can turn off **All-day event** and enter a time if one becomes available. Saving a blank start time also makes the event all-day.
- An overall event time range takes priority over a separate speaker or check-in time. Registration deadlines stay in the description and do not conflict with the event date.
- End times are optional; an unknown end time does not produce an invented duration.
- For overnight events, enter an end date. For all-day events, the editor's last day is inclusive.
- Times use the event's IANA timezone, defaulting to `America/Chicago`. Timed calendar entries export in UTC with the correct daylight-saving offset. Nonexistent or repeated wall times during DST transitions require correction; use UTC to disambiguate a repeated hour.
- Repeated imports preserve saved edits. Same-organization reposts with matching title, date, and time are collapsed. Different titles, cross-club announcements, and changed extraction order can still produce duplicates; review the list.
- Clear recap and thank-you posts are excluded from event candidates and shown under **Skipped recap posts** for the current session. You can restore one as a manual draft if needed. Mixed posts that also announce a next event keep the upcoming announcement; old recap dates and times are left out of basic parsing. Previously approved events are preserved.
- Data is held in the app session. **Download session backup** before restarting; restore it through the Export tab.

**Google Calendar:** On desktop, go to Settings → Import & export → Import. Choose the `.ics` file and target calendar. See [Google's import instructions](https://support.google.com/calendar/answer/37118?hl=en).

**Notion Calendar:** Import into Google Calendar, then connect that Google account in Notion Calendar and enable the calendar. See [Notion Calendar connections](https://www.notion.com/help/notion-calendar-connections). Direct `.ics` import into a Notion database is not implemented.

**Notion database:** Download the CSV, import it into Notion, set Start to a Date property, and create a calendar view. End is a separate column that may need mapping; CSV does not create a Notion date range automatically.

Exports are snapshots. This version does not schedule polling, serve a subscription feed, propagate cancellations, or automatically sync destinations. Stable UIDs are included in ICS files, but destination importers may still duplicate repeated file imports.

## Extraction limits

Basic mode recognizes English month/day and day/month dates, ordinal dates, ISO dates, US month/day numeric dates, `today`/`tomorrow`, `this`/`next` weekdays, AM/PM and 24-hour times, simple ranges, and labeled locations. Dotted AM/PM and Unicode spacing are supported. Registration cutoffs are excluded when selecting an event date. Clear recaps are skipped using retrospective wording and future-invitation cues. This is a heuristic: subtle or unusual wording can still need manual correction. Conflicting event dates are left unresolved. Missing years and inferred AM/PM are flagged. Basic mode does not reliably separate multiple future events or infer titles and locations from every flyer layout. All candidates require review.

Meta AI is more suitable for varied caption wording, multiple events, and distinguishing registration deadlines from event dates. OCR can still misread stylized, low-resolution, handwritten, or multilingual flyers. No mode guarantees event accuracy; the evidence and warnings remain visible for review. The initial OCR model works best with clear printed English text.

## Development and verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover actual OCR on an image-only flyer, caption parsing, publication-relative dates, ambiguous dates, carousel normalization, API request shapes with mocked responses, duplicate imports, approval gating, Streamlit review/export interactions, calendar round trips, UTC offsets, all-day boundaries, DST gaps, overnight events, and CSV formula escaping. Live Apify and Meta calls need credentials and are not run by the test suite.

`requirements-lock.txt` captures the dependency versions used for verification on Python 3.12 / Windows. For a reproducible installation on that platform, install it instead of `requirements-dev.txt`.

```text
main.py                 Streamlit app, review state, and downloads
clubcal/ingest.py        Apify collection, JSON normalization, image downloads
clubcal/ocr.py           Local OCR and image preprocessing
clubcal/extract.py       Basic parsing and optional Meta interpretation
clubcal/classify.py      Recap detection and mixed-post announcement selection
clubcal/models.py        Candidates, validation, and duplicate handling
clubcal/pipeline.py      Per-post OCR and extraction workflow
clubcal/refresh.py       Repair older drafts while preserving saved edits
clubcal/export.py        ICS and Notion CSV generation
examples/               Fictional captions and OCR demo flyer
tests/                  Behavioral tests and actual OCR integration check
```
