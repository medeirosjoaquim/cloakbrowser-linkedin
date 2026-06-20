"""One-time interactive login. Run this ONCE before starting the app.

Opens a HEADED stealth browser, fills your credentials, then PAUSES so you can
clear any 2FA / CAPTCHA / checkpoint by hand. When you press Enter it persists
the session into ./profile (reused by browser.py) plus a state.json backup.

Credentials are read from a local .env file (or the environment):

    cp .env.example .env        # then edit .env
    venv/bin/python bootstrap_login.py

STOP the app first — it locks ./profile, and two Chromium instances
cannot share one profile directory.

Automated unattended login is the single biggest trigger for LinkedIn account
locks, which is why this step is manual-first and only run once.
"""

import os
import sys
import time

import cloakbrowser as cb
from dotenv import load_dotenv

from browser import PROFILE_DIR, STATE_FILE


def main() -> int:
    load_dotenv()  # read ./.env into os.environ if present
    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")
    if not email or not password:
        print("Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD (copy .env.example to .env).")
        return 1

    ctx = cb.launch_persistent_context(PROFILE_DIR, headless=False, humanize=True)
    page = ctx.new_page()
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

    # Human-like typing rather than instant fill.
    page.type("#username", email, delay=80)
    time.sleep(0.6)
    page.type("#password", password, delay=80)
    time.sleep(0.4)
    page.click("button[type=submit]")

    print("\n" + "=" * 60)
    print("A browser window is open. Finish logging in there:")
    print("  - clear any 2FA / SMS code / CAPTCHA / 'is this you' check")
    print("  - make sure you land on your LinkedIn feed")
    print("=" * 60)
    input("\nPress Enter here once you're fully logged in... ")

    ctx.storage_state(path=STATE_FILE)
    print(f"Session persisted to {PROFILE_DIR} (+ {STATE_FILE}).")
    ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
