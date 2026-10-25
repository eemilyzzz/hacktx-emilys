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
| Snowflake | Cloud event library, separate review records, post cache, and import history | Optional; local SQLite is the default |
| Instagram Graph API | An alternative collection source with account and permission requirements | Not integrated in this version |

Instagram data access and Meta AI inference are separate services. AI credits do not grant access to arbitrary Instagram profiles. Meta's Instagram API has restrictions around professional accounts and permissions; this project uses Apify for account collection. See [Meta's Instagram API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-1ff01566-3509-48bd-a0f4-8571a91ccfdf).

### Apify setup

Create an Apify account and get an API token from its Console. Paste it into the sidebar or configure `APIFY_TOKEN` in `.env`. The app uses the Apify-maintained [`apify/instagram-post-scraper`](https://apify.com/apify/instagram-post-scraper), with its [documented username array and results limit](https://apify.com/apify/instagram-post-scraper/input-schema).

Use **Test Apify connection** to validate the token without starting a scraper. HTTP 401 means the token was rejected; HTTP 403 points to permissions or scraper access. Network failures are reported separately. Windows error 10013 means the process cannot open an outbound connection; start the app from a normal terminal with internet access, or approve a network-enabled launch in Codex. A valid token cannot overcome a process network restriction. If a run was created before an error, check Apify Console before starting another billable run.

1. Add up to 10 public club usernames, one per line.
2. Choose the recent-post limit and lookback period.
3. Click **Fetch public posts**. This starts a billable Actor run using your account.
4. Click **Extract events from collected posts**. Retries of this step reuse the collected posts and do not start a new Actor run.

The app reads image posts, carousel slides, and video/reel cover images; it does not transcribe video or fetch Stories. Private accounts are unsupported. Instagram CDN links expire: fetch fresh posts or upload the flyer when an image cannot be downloaded. JSON imports accept the [Actor's output fields](https://apify.com/apify/instagram-post-scraper/output-schema), including `caption`, `displayUrl`, `images`, and `childPosts`, as well as alternate thumbnail and nested carousel fields. Account names are also read from nested `owner`, `user`, `account`, and `profile` objects; profile exports can contain `latestPosts`. If the export omits account metadata, enter an organization name with the import, or use **Name imported organizations** in Review events to fix older unnamed imports together.

Each review shows whether image text was included. **Retry image extraction** reprocesses an unapproved event's saved images and fills missing fields while preserving existing corrections. If image links are missing or fail, an Apify token lets it look for replacement links in the latest five scraper runs' saved datasets. This uses read-only requests and never starts a new Actor run. Missing or unreadable images are reported instead of silently treated as successful OCR, and those results are not reused from the extraction cache.

### Optional Meta AI setup

You can use the app before identifying which Meta product your credits cover. Start in basic mode, then configure AI when you have an inference API key:

```powershell
Copy-Item .env.example .env
```

Edit `.env` locally; it is ignored by Git. Set `META_API_KEY`, `META_BASE_URL`, and `META_MODEL` to the values in your dashboard. The sidebar also accepts these settings.

- Current Meta Model API: `https://api.meta.ai/v1`; the configured model must be available to your account. The example uses `muse-spark-1.3`, as shown in [Meta's quickstart](https://dev.meta.ai/docs/cookbook/quickstart-chat-completions).
- Legacy Llama API: `https://api.llama.com/v1`; enter your available Llama model ID. Its response shape is supported separately; see [Meta's Llama API Python client](https://github.com/meta-llama/llama-api-python).

Only caption and recognized OCR text are sent to Meta. The model returns structured event candidates; the app validates date and time formats and requires human approval. API keys are never included in exports. Live API connectivity and your account's model entitlement must be verified with your credentials.

## Saved data and Snowflake

The **Saved data** tab shows your stored events, organization filters, and the last 100 collection/extraction attempts. Events, corrections, skipped posts, fetched posts awaiting extraction, and import history now survive browser reloads. The sidebar identifies the active storage backend.

Without Snowflake, the app saves to `.clubcal/clubcal.sqlite3` in this project. That directory is ignored by Git. Set `CLUBCAL_DATA_DIR` to choose another data directory. Existing in-memory events are imported into this local library when the updated app first runs.

To connect your own Snowflake account:

1. Install the optional connector (already installed in this computer's `.venv`):

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-snowflake.txt
   ```

2. In a Snowflake worksheet, run `scripts/snowflake_setup.sql` with a role allowed to create the warehouse, database, schema, and app role. Replace the commented `YOUR_USERNAME` grant and run it for your Snowflake user. The script uses an X-Small warehouse with 60-second auto-suspend. It is supplied for you to run; the app does not create billable warehouses or databases automatically.

3. Open **Saved data → Connect Snowflake**, enter your client account identifier (`ORG-ACCOUNT`) and username, and keep the matching warehouse/database/schema/role settings. Snowflake account URLs and SQL/data-sharing identifiers such as `ORG.ACCOUNT` are normalized for the connector; legacy regional identifiers retain their dots. Choose **Browser sign-in** and click **Connect & load Snowflake library**. This uses Snowflake's [built-in OAuth flow for local applications](https://docs.snowflake.com/en/user-guide/oauth-local-applications), without requiring a third-party SAML provider or an application client secret. Complete Snowflake login and MFA in the browser window it opens. **Organization SSO (SAML)** remains available for accounts configured with an identity provider. The app then creates its four tables in your configured schema. Browser authentication tokens are not persisted by ClubCal's connector settings. Connection errors distinguish account/DNS, network, certificates, browser-user mismatches, SAML configuration, unavailable roles, and missing database objects without displaying raw authentication responses.

4. Optional: save connection defaults by copying `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and filling in your own settings locally. The real secrets file is ignored by Git. Browser sign-in requires no password in the app. For key-pair authentication, configure `private_key_file` and, if needed, `private_key_file_pwd` in that local file. Private keys and passphrases are never written into event tables, cache entries, import history, or portable backups. See [Snowflake's Python connection guide](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect) and [key-pair authentication](https://docs.snowflake.com/en/user-guide/key-pair-auth).

5. After connecting, use **Copy local library into Snowflake** to copy your local events, review choices, recent history, and saved posts. Existing cloud records and their reviews take precedence. Your local database remains available through **Use local library**.

You can also supply the settings using `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WORKSPACE`, `SNOWFLAKE_AUTHENTICATOR`, `SNOWFLAKE_PRIVATE_KEY_FILE`, and `SNOWFLAKE_PRIVATE_KEY_FILE_PWD` environment variables.

Cloud data remains in Snowflake when the app closes. A fresh app session starts with the local library; reconnect to the same Snowflake account and workspace to open the cloud library. The connection indicator switches to **Snowflake** only after connection, schema preparation, and loading succeed. **Reconnect & retry saving** reauthenticates without discarding edits if a cloud connection expires. Failed saves remain in the session, show a warning, and can be retried; do not close the browser with unsaved changes without downloading a backup.

If browser sign-in succeeds but table setup fails, ClubCal keeps the authenticated connection in the current session. **Check Snowflake setup** reads the visibility of your configured database, schema, and warehouse. If the database or schema is missing, the app shows two `CREATE ... IF NOT EXISTS` statements and a confirmation checkbox before **Create database and schema**. This uses the already signed-in role and creates neither a warehouse nor role grants. You can instead correct the setup/permission issue in Snowflake and choose **Retry Snowflake table setup** to prepare and load the library without signing in again. **Cancel Snowflake setup** closes that pending connection and keeps the local library active.

The tables are `CC_EVENTS` (original extraction records), `CC_REVIEWS` (edits, approvals, and removal records), `CC_POSTS` (captions, media URLs, content hashes, and reusable extraction results), and `CC_IMPORTS` (collection/extraction history). SQL values are parameter-bound. A content hash plus processing settings controls cache reuse; edited captions, different local images, timezone/model changes, and parser upgrades invalidate it. Failed OCR is not reused. Apify collection still incurs its normal charges; the cache saves repeat OCR/AI work, not already-started scraper costs.

This implementation is a private local app with one workspace per connection, not a multi-user hosted service. Workspace names organize records; they are not sign-in or an authorization boundary. Multiple browser sessions can access the same library, with the last saved edit winning for the same event. Snowflake compute charges apply after you connect. Cortex AI extraction, scheduled collection, and direct Google Calendar syncing are separate features and are not enabled by this storage integration.

## Review and export

Open **Review events**, expand a candidate, compare its recognized text with the original, and correct the title, date, time, location, or timezone. Click **Save & approve**. Only valid, approved candidates appear in exports.

To approve several events together, use the checkbox beside each event or **Select all**, then click **Approve selected**. **Clear selection** unchecks the list. Batch approval uses saved details, so submit any edits first. Valid selections are approved and saved; events with missing or invalid details remain selected and unapproved, with an explanation of what to fix. Previously approved events remain approved.

**Delete selected** removes the checked events from your saved library and calendar exports. Combine it with **Select all** to clear the active event list. Skipped recap posts are kept separately. Removed event IDs stay excluded on later imports and reloads.

- Events without a specific start time default to all-day. You can turn off **All-day event** and enter a time if one becomes available. Saving a blank start time also makes the event all-day.
- An overall event time range takes priority over a separate speaker or check-in time. Registration deadlines stay in the description and do not conflict with the event date.
- End times are optional; an unknown end time does not produce an invented duration.
- For overnight events, enter an end date. For all-day events, the editor's last day is inclusive.
- Times use the event's IANA timezone, defaulting to `America/Chicago`. Timed calendar entries export in UTC with the correct daylight-saving offset. Nonexistent or repeated wall times during DST transitions require correction; use UTC to disambiguate a repeated hour.
- Repeated imports preserve saved edits. Same-organization reposts with matching title, date, and time are collapsed. Different titles, cross-club announcements, and changed extraction order can still produce duplicates; review the list.
- Clear recap and thank-you posts are excluded from event candidates and saved under **Skipped recap posts**. You can restore one as a manual draft if needed. Mixed posts that also announce a next event keep the upcoming announcement; old recap dates and times are left out of basic parsing. Previously approved events are preserved.
- Extraction reports detected events separately from new events, existing duplicates, and previously deleted events. Deleted events stay excluded on re-import until you choose **Restore deleted events** in the extraction result. Restoration keeps saved edits, returns only those detected events as unapproved drafts, and persists after reload. It does not start another Apify run.
- Data saves to the backend identified in the sidebar. **Download session backup** creates an additional portable copy of active event candidates; restore it through the Export tab. Storage also retains skipped posts, import history, and post cache entries that are not part of that portable file.
- **Saved data → Delete saved data** permanently clears the current storage workspace after you check the confirmation box. This removes events, review decisions, skipped recaps, collected posts, extraction caches, import history, and deletion records, and resets the current session. Other workspaces, the local library when clearing Snowflake, API keys, connection settings, and downloaded exports/backups are kept.

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

Storage tests use real SQLite files for reopen/reload persistence, separate original/review records, workspace filtering, rollback, caching, collected-post recovery, failed saves, and local/cloud UI switching. Snowflake SQL binding and connector authentication are tested with mocked connections. The optional Snowflake package is needed for its connector test; install `requirements-snowflake.txt` to run the complete suite. A live Snowflake read/write round trip still requires your account configuration and is not claimed by these tests. Every test uses isolated data/secrets paths and never touches your actual event library.

`requirements-lock.txt` captures the dependency versions used for verification on Python 3.12 / Windows. For a reproducible installation on that platform, install it instead of `requirements-dev.txt`.

```text
main.py                 Streamlit app, review state, and downloads
clubcal/ingest.py        Apify collection, JSON normalization, image downloads
clubcal/ocr.py           Local OCR and image preprocessing
clubcal/extract.py       Basic parsing and optional Meta interpretation
clubcal/classify.py      Recap detection and mixed-post announcement selection
clubcal/models.py        Candidates, validation, and duplicate handling
clubcal/pipeline.py      Per-post OCR and extraction workflow
clubcal/storage.py       SQLite and Snowflake storage adapters, review separation, post cache
clubcal/persistence.py   Autosave, saved-data UI, cloud authentication, and import history
clubcal/refresh.py       Repair older drafts while preserving saved edits
clubcal/export.py        ICS and Notion CSV generation
examples/               Fictional captions and OCR demo flyer
tests/                  Behavioral tests and actual OCR integration check
```
