#!/usr/bin/env python3
"""
Econ Department waitlist watcher.

Watches https://economics.dartmouth.edu/undergraduate/course-information
for the moment the Course Wait List opens for a target term (default: summer '26).

Currently the page shows "Course Wait List for spring '26 (CLOSED)".
When summer '26 opens, the line will change to mention summer.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# =================== CONFIG ===================

# Friendly name for this watcher (used in notification titles)
NAME = "Econ summer '26 waitlist"

# Page to watch
URL = "https://economics.dartmouth.edu/undergraduate/course-information"

# Optional CSS selector to narrow the watched section. None = whole body.
CSS_SELECTOR = None

# Strings that, if any appear on the page, mean the summer '26 waitlist is open.
# Case-insensitive substring match.
KEYWORDS = [
    "Course Wait List for summer",
    "Course Wait List for Summer",
    "summer '26",
    "Summer '26",
    "summer 2026",
]

# Poll interval in seconds (local mode only). 60-120 is polite.
CHECK_INTERVAL = 90

# State file. Overridable via env var (used by GitHub Actions).
_state_env = os.environ.get("WATCHER_STATE_FILE")
STATE_FILE = Path(_state_env) if _state_env else Path.home() / ".econ_watcher_state.json"

# ntfy topic. Same secret/topic as your other watcher = all alerts go to same phone.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "REPLACE_ME_with_something_random_like_dart_xyz_8472"

VERBOSE = True

# Heartbeat ping every N days. 0 to disable.
HEARTBEAT_DAYS = 7

# ==============================================


def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    log(f"Fetching {url} ...")
    r = requests.get(url, headers=headers, timeout=(10, 20))
    log(f"Got HTTP {r.status_code} ({len(r.content)} bytes)")
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

    return re.sub(r"\s+", " ", text).strip()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_summer_waitlist_open(text: str):
    """
    Detect whether the summer '26 waitlist is open.

    Returns: (opened: bool, description: str)
    """
    text_lower = text.lower()

    # Most reliable signal: explicit "Course Wait List for summer" phrase
    if "course wait list for summer" in text_lower:
        idx = text_lower.find("course wait list for summer")
        return True, text[idx:idx + 200].strip()

    # Fallback: "summer '26" / "summer 26" / "summer 2026" near a Wait List mention
    for phrase in ["summer '26", "summer 26", "summer 2026"]:
        if phrase in text_lower:
            idx = text_lower.find(phrase)
            window = text[max(0, idx - 150):idx + 200]
            wl = window.lower()
            if "wait list" in wl or "waitlist" in wl:
                return True, window.strip()

    return False, "no summer '26 waitlist mention yet"


def find_keywords(text: str, keywords):
    lower = text.lower()
    return [kw for kw in keywords if kw.lower() in lower]


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
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": "books",
            },
            timeout=10,
        )
    except Exception as e:
        log(f"ntfy push failed: {e}")

    print("\a", end="", flush=True)
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

    # TARGETED: is the summer '26 waitlist open?
    opened, description = check_summer_waitlist_open(text)
    already_alerted = state.get("opened_alert_sent", False)
    if opened and not already_alerted:
        notify(
            title=f"{NAME} is OPEN",
            message=(
                f"The summer '26 waitlist appears to be open.\n\n"
                f"Context: {description}\n\n{URL}"
            ),
            priority="urgent",
        )
        state["opened_alert_sent"] = True
    elif VERBOSE:
        log(f"{NAME}: {description}")

    # Keyword check (urgent, also captures new keyword appearances)
    found = find_keywords(text, KEYWORDS)
    seen = state.get("keywords_seen", [])
    new_keywords = [kw for kw in found if kw not in seen]
    if new_keywords:
        notify(
            title=f"{NAME}: keyword detected",
            message=f"New keyword(s): {', '.join(new_keywords)}\n\n{URL}",
            priority="urgent",
        )
        state["keywords_seen"] = list(set(seen + new_keywords))

    # General page-change fallback
    prev_hash = state.get("hash")
    if prev_hash is None:
        log(f"First run. Baseline stored. Content length: {len(text)} chars")
    elif prev_hash != current_hash:
        notify(
            title=f"{NAME}: page changed",
            message=f"Content on the watched page changed.\n\n{URL}",
            priority="high",
        )
        log("Change detected.")
    else:
        log("No hash change.")

    state["hash"] = current_hash

    # Heartbeat
    if HEARTBEAT_DAYS > 0:
        last_hb_str = state.get("last_heartbeat")
        now = datetime.now()
        send_hb = False
        if last_hb_str is None:
            send_hb = True
        else:
            try:
                last_hb = datetime.fromisoformat(last_hb_str)
                if (now - last_hb).total_seconds() >= HEARTBEAT_DAYS * 86400:
                    send_hb = True
            except Exception:
                send_hb = True

        if send_hb:
            notify(
                title=f"{NAME}: still alive",
                message=(
                    f"Heartbeat ping. Watcher is running normally.\n"
                    f"Status: {description}\n\n"
                    f"You'll get this every {HEARTBEAT_DAYS} days."
                ),
                priority="low",
            )
            state["last_heartbeat"] = now.isoformat()

    return state


def main():
    run_once = "--once" in sys.argv

    if "REPLACE_ME" in URL or "REPLACE_ME" in NTFY_TOPIC:
        print("Config missing. Either edit the top of this file, or set the")
        print("NTFY_TOPIC environment variable (for GitHub Actions / CI).")
        sys.exit(1)

    log(f"Watching {URL}")
    log(f"State: {STATE_FILE}")
    log(f"ntfy topic: {NTFY_TOPIC}")

    state = load_state()

    if run_once:
        state = check_once(state)
        save_state(state)
        log("Single check complete.")
        return

    log(f"Interval: {CHECK_INTERVAL}s  (Ctrl+C to stop)")
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
