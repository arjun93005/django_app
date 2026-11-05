"""Real-browser tests (headless Chrome). Opt-in: RUN_SELENIUM_TESTS=1 pytest -m selenium

They boot the DemoShop target on a random local port and exercise the parts
of the crawler that genuinely need a JavaScript engine.
"""

import os
import threading

import pytest
from werkzeug.serving import make_server

from apps.crawler.browser import chrome_driver
from apps.crawler.discovery import NetworkRecorder, summarize
from apps.crawler.sources.demo_shop import (
    SESSION_COOKIE,
    BrowserSessionProvider,
    DemoShopSigner,
    scrape_rendered_catalog,
)
from demo_target import app as demo

pytestmark = [
    pytest.mark.selenium,
    pytest.mark.skipif(not os.environ.get("RUN_SELENIUM_TESTS"), reason="set RUN_SELENIUM_TESTS=1"),
]


@pytest.fixture(scope="module")
def demo_url():
    server = make_server("127.0.0.1", 0, demo.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def test_browser_solves_js_challenge_and_exposes_httponly_cookie(demo_url):
    cookies = BrowserSessionProvider(demo_url).new_session()
    assert demo.session_from_cookie(cookies[SESSION_COOKIE]) is not None


def test_network_capture_and_signer_verification(demo_url):
    with chrome_driver(capture_network=True) as driver:
        recorder = NetworkRecorder(driver)
        driver.get(demo_url)
        recorder.wait_for_idle()
        calls = recorder.api_calls()

    endpoints = {(s.method, s.path): s for s in summarize(calls)}
    catalog = endpoints[("GET", "/api/v2/catalog")]
    assert {"x-ds-s", "x-ds-t"} <= set(catalog.custom_request_headers)
    assert set(catalog.response_shape) == {"data", "next", "total"}

    signed = [c for c in calls if "x-ds-s" in c.request_headers]
    signer = DemoShopSigner()
    assert signed and all(
        signer.signature(c.method, c.path, c.query, c.request_headers["x-ds-t"]) == c.request_headers["x-ds-s"]
        for c in signed
    )


def test_dom_fallback_clicks_through_the_whole_catalog(demo_url, monkeypatch):
    monkeypatch.setattr(demo, "RATE_LIMIT_PER_SEC", 1000.0)
    rows = [row for batch in scrape_rendered_catalog(demo_url) for row in batch]
    assert len(rows) == len(demo.CATALOG)
    assert rows[0]["sku"] == "DS-00001" and isinstance(rows[0]["prc"], int)
