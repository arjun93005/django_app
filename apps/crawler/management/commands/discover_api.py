"""Record and summarise the private API a web page talks to.

    python manage.py discover_api http://localhost:5050/ --click "#load-more" --clicks 2 \
        --out docs/api-captures/demo_shop.json --verify-signer

``--verify-signer`` replays every captured request that carries an
``X-DS-S`` header through our Python port of the signing algorithm and checks
it reproduces the browser's signature byte-for-byte - proof that the reverse
engineering is correct before any crawler depends on it.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from apps.crawler.browser import chrome_driver
from apps.crawler.discovery import NetworkRecorder, summarize
from apps.crawler.sources.demo_shop import DemoShopSigner


class Command(BaseCommand):
    help = "Capture XHR/fetch traffic of a page via Chrome DevTools and summarise the API."

    def add_arguments(self, parser):
        parser.add_argument("url")
        parser.add_argument("--click", help="CSS selector to click to trigger more API calls")
        parser.add_argument("--clicks", type=int, default=1)
        parser.add_argument("--out", help="Write the (cookie-redacted) capture to this JSON file")
        parser.add_argument(
            "--verify-signer", action="store_true", help="Check DemoShopSigner against captured signatures"
        )
        parser.add_argument("--headed", action="store_true", help="Show the browser window")

    def handle(self, *args, **opts):
        with chrome_driver(capture_network=True, headless=not opts["headed"]) as driver:
            recorder = NetworkRecorder(driver)
            driver.get(opts["url"])
            recorder.wait_for_idle()
            for _ in range(opts["clicks"] if opts["click"] else 0):
                buttons = driver.find_elements(By.CSS_SELECTOR, opts["click"])
                if not buttons or not buttons[0].is_displayed():
                    break
                WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, opts["click"]))).click()
                recorder.wait_for_idle()
            calls = recorder.api_calls()

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n{len(calls)} XHR/fetch calls captured\n"))
        for summary in summarize(calls):
            self.stdout.write(self.style.SUCCESS(f"{summary.method} {summary.path}"))
            self.stdout.write(f"  calls: {summary.calls}   statuses: {summary.statuses}")
            if summary.query_keys:
                self.stdout.write(f"  query params: {', '.join(summary.query_keys)}")
            if summary.custom_request_headers:
                self.stdout.write(f"  non-standard request headers: {', '.join(summary.custom_request_headers)}")
            if summary.response_shape is not None:
                shape = json.dumps(summary.response_shape, indent=2)
                self.stdout.write("  response shape:\n    " + shape.replace("\n", "\n    "))
            self.stdout.write("")

        if opts["out"]:
            path = Path(opts["out"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([c.redacted() for c in calls], indent=2), encoding="utf-8")
            self.stdout.write(f"capture written to {path}")

        if opts["verify_signer"]:
            self._verify_signer(calls)

    def _verify_signer(self, calls) -> None:
        signer = DemoShopSigner()
        signed = [c for c in calls if "x-ds-s" in c.request_headers]
        matches = 0
        for call in signed:
            expected = call.request_headers["x-ds-s"]
            ours = signer.signature(call.method, call.path, call.query, call.request_headers["x-ds-t"])
            ok = ours == expected
            matches += ok
            mark = self.style.SUCCESS("match") if ok else self.style.ERROR("MISMATCH")
            self.stdout.write(f"  {mark}  {call.method} {call.path}?{call.url.split('?', 1)[-1][:60]}")
        style = self.style.SUCCESS if signed and matches == len(signed) else self.style.ERROR
        self.stdout.write(style(f"signer reproduced {matches}/{len(signed)} captured signatures"))
