"""
BARC Dog Watch
--------------
Checks BARC's public 24petconnect adoption listing for DOGS, figures out
which ones are new since the last run, and emails a summary.

How it decides "new":
  Every animal on 24petconnect has an ID (e.g. A2094187). We keep a running
  list of every dog ID we've ever seen in `seen_ids.json` (committed back to
  the repo after each run). Anything on today's listing that ISN'T in that
  file yet is "new" and goes in the email.

Designed to run headless (GitHub Actions), but you can also run it locally:
    python scraper.py
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---- Config -----------------------------------------------------------

SHELTER_URL = "https://24petconnect.com/BARCadopt"
SHELTER_NAME = "BARC Animal Shelter & Adoptions"
SEEN_IDS_FILE = Path(__file__).parent / "seen_ids.json"
HOUSTON_TZ = ZoneInfo("America/Chicago")
MAX_PAGES = 8  # safety cap so a scraping hiccup can't loop forever

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Matches one animal "card" worth of plain text, in the order 24petconnect
# renders it. We work off the visible label:value text rather than CSS
# classes, since that's the more stable part of the page across redesigns.
ANIMAL_BLOCK_RE = re.compile(
    r"Name\s*:\s*(?P<name>.+?)\s*\((?P<id>A\d+)\)\s*"
    r"Gender\s*:\s*(?P<gender>.+?)\s*"
    r"Breed\s*:\s*(?P<breed>.+?)\s*"
    r"Animal type\s*:\s*(?P<animal_type>.+?)\s*"
    r"Age\s*:\s*(?P<age>.+?)\s*"
    r"Brought to the shelter\s*:\s*(?P<intake_date>[\d.]+)\s*"
    r"Located at\s*:\s*(?P<location>.+?)\s*"
    r"ViewType\s*:\s*\w+",
    re.DOTALL,
)

# Each photo's alt text is literally "Image_<animalID>" (e.g. "Image_A2094187"),
# which is a much more reliable hook than any CSS class for tying a photo to
# the right animal.
IMAGE_ALT_RE = re.compile(r"Image_(?P<id>A\d+)")


def fetch_rendered_html(page, url: str) -> str:
    """Load the page in a real (headless) browser and let its JavaScript
    populate the animal listing before reading the HTML back out."""
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2000)  # small buffer for any lazy AJAX calls
    return page.content()


def parse_images(html: str) -> dict[str, str]:
    """Map animal ID -> absolute photo URL, using each <img>'s alt text."""
    soup = BeautifulSoup(html, "html.parser")
    images = {}
    for img in soup.find_all("img", alt=True, src=True):
        m = IMAGE_ALT_RE.search(img["alt"])
        if not m:
            continue
        src = img["src"]
        if src.startswith("/"):
            src = "https://24petconnect.com" + src
        images[m.group("id")] = src
    return images


def parse_animals(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text)
    images = parse_images(html)
    animals = []
    for m in ANIMAL_BLOCK_RE.finditer(text):
        animal_id = m.group("id")
        animals.append(
            {
                "id": animal_id,
                "name": m.group("name").strip(),
                "gender": m.group("gender").strip(),
                "breed": m.group("breed").strip(),
                "animal_type": m.group("animal_type").strip(),
                "age": m.group("age").strip(),
                "intake_date": m.group("intake_date").strip(),
                "location": m.group("location").strip(),
                "photo_url": images.get(animal_id),
            }
        )
    return animals


def scrape_all_dogs() -> list[dict]:
    """Load the listing in a headless browser, then click through numbered
    pages until we run out of pages, hit MAX_PAGES, or a page turns up
    empty."""
    all_animals: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        html = fetch_rendered_html(page, SHELTER_URL)

        page_num = 1
        while page_num <= MAX_PAGES:
            animals = parse_animals(html)
            dog_count = sum(1 for a in animals if a["animal_type"].lower() == "dog")
            print(f"Page {page_num}: {len(animals)} animal(s) found, {dog_count} dog(s).")

            if not animals:
                if page_num == 1:
                    # First page came back empty — dump a snippet so we can
                    # see what actually loaded, next time this needs fixing.
                    text_snippet = BeautifulSoup(html, "html.parser").get_text(" ")
                    text_snippet = re.sub(r"\s+", " ", text_snippet)[:800]
                    print("DEBUG — first 800 chars of rendered page text:")
                    print(text_snippet)
                break

            for a in animals:
                all_animals[a["id"]] = a

            # Try to click through to the next numbered page.
            next_link = page.get_by_role("link", name=str(page_num + 1), exact=True)
            if next_link.count() == 0:
                break
            try:
                next_link.first.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(1500)
                html = page.content()
            except Exception as e:
                print(f"Couldn't advance past page {page_num}: {e}")
                break
            page_num += 1

        browser.close()

    dogs = [a for a in all_animals.values() if a["animal_type"].lower() == "dog"]
    return dogs


def load_seen_ids() -> set[str]:
    if SEEN_IDS_FILE.exists():
        return set(json.loads(SEEN_IDS_FILE.read_text()))
    return set()


def save_seen_ids(ids: set[str]) -> None:
    # Keep the file from growing forever - 5000 IDs is far more than BARC
    # will ever have in flight at once.
    trimmed = list(ids)[-5000:]
    SEEN_IDS_FILE.write_text(json.dumps(sorted(trimmed), indent=2))


def format_email_text(new_dogs: list[dict], total_dogs_seen: int) -> str:
    """Plain-text fallback for mail clients that don't render HTML. Photos
    show up as plain links here instead of embedded images."""
    today_str = datetime.now(HOUSTON_TZ).strftime("%A, %B %-d, %Y")
    lines = [f"BARC Dog Watch — {today_str}", ""]

    if not new_dogs:
        lines.append("No new dogs since the last check.")
    else:
        lines.append(f"{len(new_dogs)} new dog(s) since the last check:")
        lines.append("")
        for d in new_dogs:
            lines.append(f"• {d['name']} ({d['id']})")
            lines.append(f"  {d['breed']} — {d['gender']}, {d['age']}")
            lines.append(f"  Brought in: {d['intake_date']}")
            if d.get("photo_url"):
                lines.append(f"  Photo: {d['photo_url']}")
            lines.append("")
        lines.append(f"View them all: {SHELTER_URL}")

    lines.append("")
    lines.append(f"(Currently tracking {total_dogs_seen} dog IDs total at {SHELTER_NAME}.)")
    return "\n".join(lines)


def format_email_html(new_dogs: list[dict], total_dogs_seen: int) -> str:
    """HTML version with photos embedded inline."""
    today_str = datetime.now(HOUSTON_TZ).strftime("%A, %B %-d, %Y")
    parts = [f"<h2>BARC Dog Watch — {today_str}</h2>"]

    if not new_dogs:
        parts.append("<p>No new dogs since the last check.</p>")
    else:
        parts.append(f"<p>{len(new_dogs)} new dog(s) since the last check:</p>")
        for d in new_dogs:
            photo_html = (
                f'<img src="{d["photo_url"]}" alt="{d["name"]}" '
                f'style="max-width:220px; border-radius:8px; display:block; '
                f'margin-bottom:8px;">'
                if d.get("photo_url")
                else ""
            )
            parts.append(
                "<div style='margin-bottom:20px; font-family:sans-serif;'>"
                f"{photo_html}"
                f"<strong>{d['name']} ({d['id']})</strong><br>"
                f"{d['breed']} — {d['gender']}, {d['age']}<br>"
                f"Brought in: {d['intake_date']}"
                "</div>"
            )
        parts.append(f'<p><a href="{SHELTER_URL}">View them all on 24petconnect →</a></p>')

    parts.append(
        f"<p style='color:#888; font-size:12px;'>"
        f"(Currently tracking {total_dogs_seen} dog IDs total at {SHELTER_NAME}.)</p>"
    )
    return "<html><body>" + "".join(parts) + "</body></html>"


def send_email(subject: str, text_body: str, html_body: str | None = None) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", gmail_user)

    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    else:
        msg = MIMEText(text_body)

    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)


def main() -> int:
    print(f"Fetching dog listing from {SHELTER_URL} ...")
    dogs = scrape_all_dogs()
    print(f"Found {len(dogs)} dogs currently listed.")

    if not dogs:
        print("No dogs parsed at all — the site's layout may have changed.")
        # Still email so Caroline knows something's off, rather than silence.
        send_email(
            subject="BARC Dog Watch — heads up, nothing parsed today",
            text_body=(
                "The scraper ran but didn't find any dog listings. "
                "This usually means the shelter site's page layout changed "
                "and the script needs an update. Nothing was marked as seen."
            ),
        )
        return 1

    seen_ids = load_seen_ids()
    new_dogs = [d for d in dogs if d["id"] not in seen_ids]
    new_dogs.sort(key=lambda d: d["intake_date"], reverse=True)

    all_ids_today = seen_ids.union(d["id"] for d in dogs)
    save_seen_ids(all_ids_today)

    subject = (
        f"BARC Dog Watch — {len(new_dogs)} new dog(s)"
        if new_dogs
        else "BARC Dog Watch — no new dogs today"
    )
    text_body = format_email_text(new_dogs, len(all_ids_today))
    html_body = format_email_html(new_dogs, len(all_ids_today))
    send_email(subject, text_body, html_body)
    print("Email sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
