#!/usr/bin/env python3
"""
Dartmouth page watcher.

Polls a webpage on an interval, alerts you when:
  1. Any watched keyword appears (e.g. "apply", "transfer term")
  2. The content of the page (or a specific section) changes at all

Notifications go to ntfy.sh (free, no account needed) so you get push
alerts on your phone even when away from your computer.
"""

import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# =================== CONFIG ===================

# The page you want to watch.
URL = "https://registrar.dartmouth.edu/students/enrollment/study-away/transfer-terms"

# OPTIONAL: narrow the watch to one section of the page using a CSS selector.
# Examples: "main", "#content", ".program-content", "article"
# Set to None to watch the whole page body (safer default).
CSS_SELECTOR = None

# The term you are waiting for. Change this if you want to watch a different term.
WATCHED_TERM = "Winter 2027"

# Words/phrases that, if they appear on the page, trigger an URGENT alert.
# These are tuned to fire when the Winter 2027 application goes live.
KEYWORDS = [
    "Winter Transfer Term Application",  # link text that appears when open
    "Winter 2027 Transfer Term Application",
]

# How often to check, in seconds. 60 to 120 is a polite range.
# Do not go below 30 unless you really need to.
CHECK_INTERVAL = 90

# Where to remember state between runs (what the page looked like last time)
STATE_FILE = Path.home() / ".dartmouth_watcher_state.json"

# ntfy.sh topic name. MAKE THIS LONG AND RANDOM, like a password.
# Anyone who knows the topic can send AND read messages on it.
# Then: install the "ntfy" app on your phone, tap +, and subscribe to the
# same topic name to get push notifications.
NTFY_TOPIC = "dartmouth_winter27_kj83nf9wpq"

# Print activity to the terminal
VERBOSE = True

# ==============================================


def fetch_page(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (personal page watcher)"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def extract_content(html: str, selector):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    if selector:
        node = soup.select_one(selector)
        text = node.get_text(" ", strip=True) if node else ""
    else:
        body = soup.body or soup
        text = body.get_text(" ", strip=True)

    # Collapse whitespace so trivial spacing edits don't trigger alerts
    return re.sub(r"\s+", " ", text).strip()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_keywords(text: str, keywords):
    lower = text.lower()
    return [kw for kw in keywords if kw.lower() in lower]


def check_watched_term_status(text: str):
    """
    Look specifically at the line for the watched term (e.g. "Winter 2027")
    and determine whether its application appears to be OPEN.

    Returns: (opened: bool, description: str)
      opened=True means the status looks like it flipped to Open.
    """
    if WATCHED_TERM not in text:
        return False, f"'{WATCHED_TERM}' not found on page (page may have been restructured)"

    idx = text.find(WATCHED_TERM)
    window = text[idx:idx + 250]
    lower = window.lower()

    if "not yet available" in lower:
        return False, "still 'Not yet Available'"
    if "closed" in lower:
        return False, "shows 'Closed' (you may have missed it!)"
    if "open" in lower or "transfer term application" in lower:
        return True, window.strip()
    return False, f"status unclear near '{WATCHED_TERM}': {window[:120]}"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"hash": None, "keywords_seen": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def notify(title: str, message: str, priority: str = "default") -> None:
    """Send a push via ntfy.sh and also beep the terminal."""
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,  # default, high, urgent
                "Tags": "bell",
            },
            timeout=10,
        )
    except Exception as e:
        log(f"ntfy push failed: {e}")

    print("\a", end="", flush=True)  # terminal bell
    print(f"\n{'=' * 60}\n  ALERT: {title}\n  {message}\n{'=' * 60}\n")


def log(msg: str) -> None:
    if VERBOSE:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {msg}")


def check_once(state: dict) -> dict:
    try:
        html = fetch_page(URL)
    except Exception as e:
        log(f"Fetch failed: {e}")
        return state

    text = extract_content(html, CSS_SELECTOR)
    current_hash = hash_text(text)

    # TARGETED CHECK: is the watched term's application open?
    opened, description = check_watched_term_status(text)
    already_alerted = state.get("opened_alert_sent", False)
    if opened and not already_alerted:
        notify(
            title=f"{WATCHED_TERM} transfer term application is OPEN",
            message=f"{WATCHED_TERM} now appears to be open.\n\nContext: {description}\n\n{URL}",
            priority="urgent",
        )
        state["opened_alert_sent"] = True
    elif VERBOSE:
        log(f"{WATCHED_TERM}: {description}")

    # Keyword check (urgent)
    found = find_keywords(text, KEYWORDS)
    seen = state.get("keywords_seen", [])
    new_keywords = [kw for kw in found if kw not in seen]
    if new_keywords:
        notify(
            title="Dartmouth page: keyword detected",
            message=f"New keyword(s): {', '.join(new_keywords)}\n\n{URL}",
            priority="urgent",
        )
        state["keywords_seen"] = list(set(seen + new_keywords))

    # General content change check (catches anything else)
    prev_hash = state.get("hash")
    if prev_hash is None:
        log(f"First run. Baseline stored. Content length: {len(text)} chars")
    elif prev_hash != current_hash:
        notify(
            title="Dartmouth page changed",
            message=f"Content on the watched page changed.\n\n{URL}",
            priority="high",
        )
        log("Change detected.")
    else:
        log("No hash change.")

    state["hash"] = current_hash
    return state


def main():
    if "REPLACE_ME" in URL or "REPLACE_ME" in NTFY_TOPIC:
        print("Edit the CONFIG section at the top of this file first.")
        print("You need to set URL and NTFY_TOPIC.")
        sys.exit(1)

    log(f"Watching {URL}")
    log(f"Interval: {CHECK_INTERVAL}s  |  State: {STATE_FILE}")
    log(f"ntfy topic: {NTFY_TOPIC}  (subscribe in the ntfy app on your phone)")

    state = load_state()
    try:
        while True:
            state = check_once(state)
            save_state(state)
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        log("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
