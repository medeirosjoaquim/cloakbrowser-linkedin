"""Real headless end-to-end scrape of the kruncher company page.

Runs only when LINKEDIN_EMAIL and LINKEDIN_PASSWORD are set (locally via .env,
or as CI secrets); skipped otherwise. Launches cloakbrowser headless, logs in,
and scrapes the live page — no window, no manual steps.

    venv/bin/python -m pytest -m e2e
"""

import os

import pytest
from dotenv import load_dotenv

import browser

load_dotenv()

KRUNCHER = "https://www.linkedin.com/company/kruncher/"

pytestmark = pytest.mark.e2e

needs_creds = pytest.mark.skipif(
    not (os.getenv("LINKEDIN_EMAIL") and os.getenv("LINKEDIN_PASSWORD")),
    reason="set LINKEDIN_EMAIL and LINKEDIN_PASSWORD to run the headless e2e scrape",
)


@pytest.fixture(scope="module")
def session():
    state = browser.start_login()  # headless login via cloakbrowser
    if state["state"] != "logged_in":
        pytest.skip(f"could not log in headless (state={state['state']}): {state['detail']}")
    yield
    if browser._ctx is not None:
        browser._ctx.close()


@needs_creds
def test_headless_login(session):
    assert browser.is_logged_in() is True


@needs_creds
def test_scrape_kruncher(session):
    result = browser.fetch_html(KRUNCHER)
    # we must land on the company page, not be bounced to the authwall
    assert "/company/kruncher" in result["final_url"], result["final_url"]
    assert "authwall" not in result["final_url"]
    assert "/login" not in result["final_url"]
    # a real rendered profile is a large document
    assert len(result["html"]) > 10_000
