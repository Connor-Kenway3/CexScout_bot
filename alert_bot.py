"""
CEXScout — CEX Listing Alert Bot
Each exchange runs in its own thread. Fetching, comparing, and alerting
happen independently and concurrently. Adding a new exchange = one new
ExchangeWorker instance at the bottom.

Setup:
    pip install requests beautifulsoup4 python-dotenv cloudscraper deep-translator flask

Run:
    python cexscout.py
"""

import os
import re
import time
import json
import random
import threading
import requests
import cloudscraper
from flask import Flask
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID")
POLL_INTERVAL = 40
WINDOW        = 5
STATE_FILE    = "state.json"
STATE_LOCK    = threading.Lock()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

upbit_session    = requests.Session()
upbit_session.headers.update(HEADERS)

telegram_session = requests.Session()
translator       = GoogleTranslator(source="auto", target="en")

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            content = f.read().strip()
            if content:
                return json.loads(content)
    return {}

def save_state(state: dict):
    with STATE_LOCK:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = telegram_session.post(url, json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        r.raise_for_status()
        print("Alert sent.")
    except Exception as e:
        print(f"Telegram send failed: {e}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def translate(text: str) -> str:
    try:
        return translator.translate(text)
    except Exception as e:
        print(f"Translation failed: {e}")
        return text

def extract_ticker(title: str) -> str:
    match = re.search(r'\(([A-Z0-9]{2,10})\)', title)
    return match.group(1) if match else ""

def format_alert(notice: dict) -> str:
    ticker = extract_ticker(notice["title"])
    translated = translate(notice["title"])
    ticker_line = f"Related Ticker: ${ticker}" if ticker else ""
    return (
        f"{notice['exchange'].upper()} ALERT\n"
        f"<a href=\"{notice['url']}\">{translated}</a>\n"
        f"{ticker_line}"
    ).strip()

# ── Base Exchange Worker ──────────────────────────────────────────────────────

class ExchangeWorker(threading.Thread):

    def __init__(self, name: str, state: dict):
        super().__init__(name=name, daemon=True)
        self.key   = name.lower()
        self.state = state

        if self.key not in self.state:
            self.state[self.key] = []

    def fetch(self) -> list[dict]:
        raise NotImplementedError

    def run(self):
        print(f"{self.name} worker started.")
        while True:
            try:
                fetched = self.fetch()
                self._process(fetched)
            except Exception as e:
                print(f"{self.name} error: {e}")

            sleep_for = POLL_INTERVAL + random.randint(-10, 10)
            time.sleep(sleep_for)

    def _process(self, fetched: list[dict]):
        if not fetched:
            print(f"{self.name}: fetch returned empty.")
            return

        fresh_top  = fetched[:WINDOW]
        fresh_ids  = [n["id"] for n in fresh_top]
        stored_ids = set(self.state[self.key])

        if not stored_ids:
            print(f"{self.name}: seeding window.")
            self.state[self.key] = fresh_ids
            save_state(self.state)
            return

        new_notices = [n for n in fresh_top if n["id"] not in stored_ids]

        if not new_notices:
            print(f"{self.name}: no new listings.")
            return

        print(f"{self.name}: {len(new_notices)} new listing(s).")

        for notice in new_notices:
            send_telegram(format_alert(notice))

        self.state[self.key] = fresh_ids
        save_state(self.state)

# ── Upbit Worker ──────────────────────────────────────────────────────────────

class UpbitWorker(ExchangeWorker):

    URL = (
        "https://api-manager.upbit.com/api/v1/announcements"
        "?os=web&page=1&per_page=20&category=trade"
    )

    def __init__(self, state: dict):
        super().__init__("Upbit", state)

    def fetch(self) -> list[dict]:
        r = upbit_session.get(
            self.URL,
            headers={"accept": "application/json", "referer": "https://upbit.com/"},
            timeout=10,
        )
        r.raise_for_status()
        notices = r.json().get("data", {}).get("notices", [])
        return [
            {
                "id":       f"upbit-{n['id']}",
                "title":    n.get("title", ""),
                "url":      f"https://upbit.com/service_center/notice?id={n['id']}",
                "exchange": "Upbit",
            }
            for n in notices
        ]

# ── Bithumb Worker ────────────────────────────────────────────────────────────

class BithumbWorker(ExchangeWorker):

    URL = "https://feed.bithumb.com/notice?category=9&page=1"

    def __init__(self, state: dict):
        super().__init__("Bithumb", state)
        self._scraper = cloudscraper.create_scraper()

    def fetch(self) -> list[dict]:
        r = self._scraper.get(self.URL, timeout=15)
        r.raise_for_status()

        soup   = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            print("Bithumb: __NEXT_DATA__ not found.")
            return []

        data    = json.loads(script.string)
        notices = data.get("props", {}).get("pageProps", {}).get("noticeList", [])
        return [
            {
                "id":       f"bithumb-{n.get('id', '')}",
                "title":    n.get("title", ""),
                "url":      f"https://feed.bithumb.com/notice/{n.get('id', '')}",
                "exchange": "Bithumb",
            }
            for n in notices
        ]

# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def health():
    return "ok", 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    state = load_state()

    workers = [
        UpbitWorker(state),
        BithumbWorker(state),
    ]

    send_telegram("CEXScout is live.")

    threading.Thread(target=run_flask, daemon=True).start()

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

if __name__ == "__main__":
    main()
