#!/usr/bin/env python3
"""
flame_telegram.py - send-only Telegram notifier for the flame monitor.

Uses the Purplevector Empire bot. Credentials come from (first hit wins):
    1. env vars TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    2. the file named by FLAME_TELEGRAM_ENV
    3. ../../uload/.env.local   (the Empire bot's home, C:\\Code\\uload)

SEND-ONLY ON PURPOSE: uload long-polls getUpdates on this same bot, and
Telegram allows only one poller per bot. If this module ever polled it would
steal uload's button taps and replies. So: sendMessage only, never getUpdates.

CLI:
    python flame_telegram.py --check          show whether creds were found (sends nothing)
    python flame_telegram.py --test "hello"   send one test message
"""

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UL_ENV = os.path.normpath(os.path.join(HERE, "..", "..", "uload", ".env.local"))
PREFIX = "\U0001F525 "  # fire emoji


def _parse_env_file(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[7:].strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except OSError:
        pass
    return out


def load_credentials():
    """Return (token, chat_id, source) or (None, None, None)."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        return tok, chat, "environment"
    for path in (os.environ.get("FLAME_TELEGRAM_ENV", ""), UL_ENV):
        if not path:
            continue
        env = _parse_env_file(path)
        tok = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = env.get("TELEGRAM_CHAT_ID", "").strip()
        if tok and chat:
            return tok, chat, path
    return None, None, None


class Telegram:
    """Ordered, non-blocking sender. send() queues; a worker thread delivers
    with retries (and honours Telegram's 429 retry_after)."""

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self._q = queue.Queue()
        self._worker = None
        self._lock = threading.Lock()
        self.last_ok = None

    @classmethod
    def from_env(cls):
        tok, chat, src = load_credentials()
        if not tok:
            return None, None
        return cls(tok, chat), src

    def send(self, text):
        self._ensure_worker()
        self._q.put(PREFIX + text)

    def flush(self, timeout=8.0):
        """Wait (bounded) until queued messages are delivered. Used on shutdown."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.1)
        return False

    def _ensure_worker(self):
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="telegram", daemon=True)
                self._worker.start()

    def _run(self):
        while True:
            text = self._q.get()
            try:
                self.last_ok = self._deliver(text)
            finally:
                self._q.task_done()

    def _deliver(self, text):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=body, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
                return True
            except urllib.error.HTTPError as e:
                wait = 3 * (attempt + 1)
                if e.code == 429:
                    try:
                        wait = int(json.loads(e.read()).get("parameters", {}).get("retry_after", wait))
                    except Exception:
                        pass
                elif 400 <= e.code < 500:
                    print(f"[telegram] rejected ({e.code}); not retrying", flush=True)
                    return False
                time.sleep(min(wait, 30))
            except Exception as e:  # network down, DNS, timeout
                print(f"[telegram] send failed ({e}); retry {attempt + 1}/4", flush=True)
                time.sleep(3 * (attempt + 1))
        return False


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test", metavar="TEXT")
    a = ap.parse_args()
    tg, src = Telegram.from_env()
    if tg is None:
        print("No Telegram credentials found (env or uload/.env.local).")
        raise SystemExit(1)
    print(f"Telegram credentials found in: {src}")
    if a.test:
        tg.send(a.test)
        print("sent" if (tg.flush(15) and tg.last_ok) else "FAILED (see messages above)")
