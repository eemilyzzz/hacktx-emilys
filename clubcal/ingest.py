from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .models import Post

APIFY_API = "https://api.apify.com/v2"


def _apify_request(method: str, path: str, token: str, *, action: str, run_id: str = "", **kwargs):
    """Keep credentials out of error messages and distinguish auth from networking."""
    if not token.strip():
        raise ValueError("Add an Apify token in the sidebar, or import a JSON export.")
    recovery = f" Apify run {run_id} already exists; check it in Apify Console instead of starting another run." if run_id else ""
    if method == "post":
        recovery += " If Apify received the request, a run may already exist. Check Apify Console before fetching again."
    try:
        # Deliberately no POST retries: starting a run can incur a charge.
        response = getattr(requests, method)(
            APIFY_API + path, headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=(10, 40), **kwargs,
        )
    except requests.exceptions.SSLError:
        raise ValueError("Could not verify Apify's HTTPS certificate. Check your computer's certificate or proxy configuration." + recovery) from None
    except requests.exceptions.Timeout:
        raise ValueError(f"The connection to Apify timed out while {action}. Your token was not rejected." + recovery) from None
    except requests.exceptions.ConnectionError as exc:
        if "10013" in str(exc):
            raise ValueError("This app process is blocked from connecting to Apify (Windows error 10013). Start ClubCal in a terminal with internet access. Your token has not been checked." + recovery) from None
        raise ValueError(f"Could not connect to Apify while {action}. Check the app's network access, firewall, DNS, or proxy. This is not an authentication response." + recovery) from None
    except requests.exceptions.RequestException:
        raise ValueError(f"The Apify request failed while {action}." + recovery) from None
    if not response.ok:
        status = response.status_code
        descriptions = {
            401: "Apify rejected the token. Check that it is valid, unexpired, and copied in full.",
            402: "Apify requires additional balance or a paid plan for this request.",
            403: "Apify denied access. Check the token's permissions and your account's access to the Instagram scraper.",
            404: "Apify could not find the requested scraper or run.",
            429: "Apify's request limit was reached. Wait before trying again.",
        }
        detail = descriptions.get(status, f"Apify returned an error while {action}.")
        # Error codes help diagnose Actor input/billing issues without echoing bodies.
        try:
            code = response.json().get("error", {}).get("type", "")
        except (ValueError, AttributeError):
            code = ""
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", code):
            detail += f" Error code: {code}."
        raise ValueError(f"{detail} (HTTP {status})." + recovery)
    return response


def check_apify_connection(token: str) -> None:
    """Validate connectivity and token without creating a billable Actor run."""
    _apify_request("get", "/users/me", token, action="checking the token")


def username_from_input(value: str) -> str:
    value = value.strip().lstrip("@")
    if "://" in value:
        parsed = urlparse(value)
        if parsed.hostname not in ("instagram.com", "www.instagram.com"):
            raise ValueError("Use an Instagram username or profile URL.")
        value = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value) or value.lower() in {"p", "reel", "stories", "explore"}:
        raise ValueError("Enter a profile username, such as @yourclub, rather than a post URL.")
    return value


def organization_from_row(row: dict, fallback: str = "") -> str:
    """Support display names, handles, nested owners, and profile scrape exports."""
    for key in ("organization", "organization_name", "organizationName", "accountName", "account_name", "profileName", "profile_name", "ownerFullName", "fullName", "full_name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("owner", "user", "author", "account", "profile", "organization"):
        nested = row.get(key)
        if isinstance(nested, dict):
            for field in ("full_name", "fullName", "display_name", "displayName", "name", "username", "userName", "handle"):
                value = nested.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip().lstrip("@")
        elif isinstance(nested, str) and nested.strip():
            return nested.strip().lstrip("@")
    for key in ("ownerUsername", "owner_username", "username", "userName", "authorUsername", "accountUsername", "profileUsername", "handle"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("@")
    for key in ("inputUrl", "profileUrl", "profile_url", "accountUrl"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                return username_from_input(value)
            except ValueError:
                pass
    return fallback.strip() or "Imported club"


def _post_timestamp(row: dict) -> str:
    for key in ("timestamp", "takenAt", "taken_at", "taken_at_timestamp", "postedAt", "posted_at", "publishedAt", "published_at", "date"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError):
                continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_posts(rows: list[dict] | dict, organization: str = "") -> list[Post]:
    if isinstance(rows, dict):
        organization = organization_from_row(rows, organization)
        rows = rows.get("posts", rows.get("items", rows.get("data")))
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("Import an Apify JSON array containing at most 500 posts.")
    posts = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Every JSON entry must be a post object.")
        profile_posts = row.get("latestPosts", row.get("posts"))
        if isinstance(profile_posts, list):
            posts.extend(normalize_posts(profile_posts, organization_from_row(row, organization)))
            if len(posts) > 500:
                raise ValueError("Import at most 500 posts at a time.")
            continue
        images = []
        children = row.get("childPosts") or row.get("children") or []
        if isinstance(children, dict):
            children = children.get("data", [])
        for child in children:
            if not isinstance(child, dict):
                continue
            if child.get("type", child.get("media_type")) not in ("Video", "VIDEO"):
                images.append(child.get("displayUrl") or child.get("media_url"))
        images.extend(row.get("images") or [])
        if not images and row.get("type", row.get("media_type")) not in ("Video", "VIDEO"):
            images.append(row.get("displayUrl") or row.get("media_url"))
        url = str(row.get("url") or row.get("permalink") or row.get("postUrl") or row.get("post_url") or "")
        shortcode = row.get("shortCode") or row.get("shortcode")
        if not url and shortcode:
            url = f"https://www.instagram.com/p/{shortcode}/"
        identity = str(row.get("id") or row.get("post_id") or url or "")
        if not identity:
            raise ValueError("Each post needs an id or Instagram URL for duplicate detection.")
        posts.append(Post(
            id=identity,
            username=organization_from_row(row, organization),
            caption=str(row.get("caption") or ""),
            url=url,
            timestamp=_post_timestamp(row),
            image_urls=list(dict.fromkeys(url for url in images if isinstance(url, str) and url)),
        ))
    return posts


def fetch_apify(usernames: list[str], token: str, limit: int = 10, newer_than: str = "30 days", progress=None) -> list[Post]:
    if not token.strip():
        raise ValueError("Add an Apify token in the sidebar, or import a JSON export.")
    if not 1 <= len(usernames) <= 10 or not 1 <= limit <= 30:
        raise ValueError("Fetch 1–10 accounts and 1–30 posts per account.")
    names = list(dict.fromkeys(username_from_input(value) for value in usernames))
    # Never retry POST: that could create another billable run.
    response = _apify_request(
        "post", "/acts/apify~instagram-post-scraper/runs", token, action="starting the scraper",
        params={"timeout": 240, "maxItems": len(names) * limit},
        json={"username": names, "resultsLimit": limit, "onlyPostsNewerThan": newer_than, "dataDetailLevel": "basicData"},
    )
    run = response.json()["data"]
    run_id = run["id"]
    deadline = time.monotonic() + 280
    while run["status"] in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}:
        if time.monotonic() >= deadline:
            raise ValueError(f"Apify is still processing run {run_id}. Export its JSON from Apify Console and import it here; do not start another run.")
        if progress:
            progress(f"Collecting public posts · Apify run {run_id}")
        time.sleep(2)
        response = _apify_request("get", f"/actor-runs/{run_id}", token, action="checking the scraper", run_id=run_id)
        run = response.json()["data"]
    if run["status"] != "SUCCEEDED":
        raise ValueError(f"Apify run {run_id} ended with {run['status']}. Check Apify Console for details.")
    response = _apify_request(
        "get", f"/datasets/{run['defaultDatasetId']}/items", token,
        action="downloading posts", run_id=run_id,
        params={"format": "json", "clean": "true", "limit": len(names) * limit},
    )
    return normalize_posts(response.json())


def download_instagram_image(url: str) -> bytes:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or not any(
        host.endswith("." + domain) or host == domain for domain in ("cdninstagram.com", "fbcdn.net")
    ):
        raise ValueError("Image URL is not an Instagram CDN URL. Upload the image manually.")
    with requests.get(url, stream=True, timeout=(10, 30), allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("Image download failed. Instagram image links expire; fetch fresh posts or upload the flyer.")
        if not response.headers.get("Content-Type", "").startswith("image/"):
            raise ValueError("The media URL did not return an image.")
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > 20 * 1024 * 1024:
                raise ValueError("Image exceeds the 20 MB limit.")
        return bytes(content)
