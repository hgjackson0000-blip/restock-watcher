import base64
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

STATE_FILE = "state.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

CYCLES_PER_BROWSER = 300     # restart the browser now and then to keep memory low
FAILS_BEFORE_ALERT = 12      # consecutive failed checks before a "trouble" alert


def get_cfg():
    try:
        interval = max(3, int(os.environ.get("INTERVAL", "10")))
    except ValueError:
        interval = 10
    return {
        "webhook_url": os.environ.get("WEBHOOK_URL", "").strip(),
        "ping": os.environ.get("PING", "").strip(),
        "interval": interval,
        "url": os.environ.get("SITE_URL", "https://spoofdonut.com/").strip(),
        "bot_name": os.environ.get("BOT_NAME", "Restock Watcher").strip() or "Restock Watcher",
        "avatar_url": os.environ.get("AVATAR_URL", "").strip(),
    }


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def norm(s):
    return " ".join(s.split())


def parse_status(body):
    """Pull just the stock/status text out of the page text."""
    lines = [norm(l) for l in body.splitlines() if norm(l)]
    low = [l.lower() for l in lines]

    def find(text, start=0):
        return next((i for i in range(start, len(low)) if text in low[i]), None)

    # Layout 1: logged-in dashboard ("Your dashboard" ... "Sell an account")
    a = find("your dashboard")
    if a is not None:
        b = find("sell an account", a + 1)
        if b is not None:
            chunk = [l for l in lines[a + 1:b] if "ltc per" not in l.lower()]
            return " | ".join(chunk)

    # Layout 2: public landing page ("Instant Litecoin payout" ... "accounts bought")
    a = find("instant litecoin payout")
    b = find("accounts bought")
    if a is not None and b is not None and b > a:
        chunk = lines[a + 1:b]
        if chunk and re.fullmatch(r"[\d,]+", chunk[-1]):
            chunk = chunk[:-1]
        return " | ".join(chunk)

    # Fallback
    return " | ".join(l for l in lines if "payout funds" in l.lower())


def is_out(text):
    t = (text or "").lower()
    return "out of payout funds" in t or "out of funds" in t


def mention_for(cfg):
    p = cfg.get("ping", "").strip()
    if p.lower() == "everyone":
        return "@everyone "
    if p.isdigit():
        return f"<@{p}> "
    return ""


def clean(s):
    return (s or "").replace("`", "'")


def wrap(cfg, content, embed):
    payload = {
        "username": cfg["bot_name"],
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"parse": ["users", "everyone"]},
    }
    if cfg.get("avatar_url"):
        payload["avatar_url"] = cfg["avatar_url"]
    return payload


def build_alert(cfg, old, new):
    """The pretty restock / out-of-funds embed."""
    now = int(time.time())
    if is_out(new):
        title, color = "\U0001F534  OUT OF PAYOUT FUNDS", 0xE74C3C
        desc = "The shop ran out of payout funds again."
    else:
        title, color = "\U0001F7E2  RESTOCK DETECTED", 0x2ECC71
        desc = "The status just changed. Go check the site now!"

    embed = {
        "title": title,
        "url": cfg["url"],
        "description": f"{desc}\n\n[**Open the site**]({cfg['url']})",
        "color": color,
        "fields": [
            {"name": "Was", "value": f"```{clean(old)[:900] or '(nothing)'}```", "inline": True},
            {"name": "Now", "value": f"```{clean(new)[:900] or '(badge is gone)'}```", "inline": True},
            {"name": "Detected", "value": f"<t:{now}:T>  (<t:{now}:R>)", "inline": False},
        ],
        "footer": {"text": f"Restock Watcher  •  checking every {cfg['interval']}s"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return wrap(cfg, f"{mention_for(cfg)}{title.strip()}", embed)


def build_notice(cfg, title, desc, color, ping=False):
    """Small info embed (started / trouble / login expired)."""
    embed = {
        "title": title,
        "description": desc[:1800],
        "color": color,
        "footer": {"text": "Restock Watcher  •  server"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return wrap(cfg, mention_for(cfg).strip() if ping else "", embed)


def send_webhook(cfg, payload):
    r = requests.post(cfg["webhook_url"], json=payload, timeout=15)
    r.raise_for_status()


def safe_send(cfg, payload):
    try:
        send_webhook(cfg, payload)
    except Exception as e:
        log(f"Could not send Discord message: {e}")


def read_status(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function(
        """() => {
            const t = (document.body ? document.body.innerText : '').toLowerCase();
            return t.includes('ltc per') || t.includes('accounts bought') || t.includes('payout funds');
        }""",
        timeout=20000,
    )
    body = page.inner_text("body")
    return parse_status(body), "your dashboard" in body.lower()


def process(cfg, state, status, logged_in, have_login):
    """Handle one successful page read. Returns nothing; sends Discord messages."""
    # Recovered from an outage?
    if state["fail"] >= FAILS_BEFORE_ALERT:
        safe_send(cfg, build_notice(cfg, "\u2705  Watcher recovered",
                                    "The checks are working again.", 0x2ECC71))
    state["fail"] = 0

    # Logged out? Don't treat the public page as a status change.
    if have_login and not logged_in:
        if not state["session_alerted"]:
            state["session_alerted"] = True
            log("Login looks expired.")
            safe_send(cfg, build_notice(
                cfg, "\u26A0\uFE0F  Login expired",
                "The saved login no longer works, so I can't see the restock status. "
                "Log in again on your PC, re-save the session, and update the STATE_B64 variable.",
                0xF1C40F, ping=True))
        else:
            log("Still logged out - skipping.")
        return
    state["session_alerted"] = False

    key = status.lower()
    if state["last"] is None:
        log(f"Starting status: {status or '(no badge)'}")
        if not state["announced"]:
            state["announced"] = True
            safe_send(cfg, build_notice(
                cfg, "\U0001F7E2  Watcher is online",
                f"Watching every {cfg['interval']}s.\n**Current status:** {status or '(no badge)'}",
                0x5865F2))
    elif key != state["last"]:
        log(f"CHANGED: {state['last']!r} -> {key!r}")
        send_webhook(cfg, build_alert(cfg, state.get("last_raw", ""), status))   # raises on failure -> retried next cycle
        log("Discord alert sent.")
    else:
        log(f"No change: {status or '(no badge)'}")
    state["last"] = key
    state["last_raw"] = status


def run_session(cfg, state, have_login):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            kwargs = {"user_agent": UA}
            if have_login:
                kwargs["storage_state"] = STATE_FILE
            context = browser.new_context(**kwargs)
            page = context.new_page()

            consecutive = 0
            for _ in range(CYCLES_PER_BROWSER):
                try:
                    status, logged_in = read_status(page, cfg["url"])
                    process(cfg, state, status, logged_in, have_login)
                    consecutive = 0
                except Exception as e:
                    consecutive += 1
                    state["fail"] += 1
                    log(f"Error (will retry): {str(e)[:300]}")
                    if state["fail"] == FAILS_BEFORE_ALERT:
                        safe_send(cfg, build_notice(
                            cfg, "\u26A0\uFE0F  Watcher having trouble",
                            f"The last {FAILS_BEFORE_ALERT} checks failed.\n`{str(e)[:300]}`",
                            0xF1C40F, ping=True))
                    if consecutive >= 5:
                        raise RuntimeError("too many errors in a row, restarting the browser")
                time.sleep(cfg["interval"])
        finally:
            try:
                browser.close()
            except Exception:
                pass


def main():
    cfg = get_cfg()
    if not cfg["webhook_url"].startswith("https://"):
        log("ERROR: set the WEBHOOK_URL variable (your Discord webhook URL).")
        sys.exit(1)

    have_login = False
    b64 = os.environ.get("STATE_B64", "").strip()
    if b64:
        try:
            with open(STATE_FILE, "wb") as f:
                f.write(base64.b64decode(b64))
            have_login = True
            log("Loaded saved login.")
        except Exception as e:
            log(f"WARNING: could not read STATE_B64: {e}")
    else:
        log("WARNING: no STATE_B64 set, so I can only see the public page (not logged in).")

    log(f"Watching {cfg['url']} every {cfg['interval']}s")
    state = {"last": None, "announced": False, "session_alerted": False, "fail": 0}

    while True:
        try:
            run_session(cfg, state, have_login)
        except Exception as e:
            log(f"Restarting browser: {str(e)[:300]}")
            time.sleep(10)


if __name__ == "__main__":
    main()
