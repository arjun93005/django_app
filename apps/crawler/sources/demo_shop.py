"""DemoShop: a JavaScript SPA whose data only exists behind a signed private API.

How the pieces were found is documented in docs/reverse-engineering.md. Short
version of the resulting strategy ("hybrid crawling"):

* **Browser only where JavaScript is unavoidable.** The session cookie is
  issued after solving a per-request JS challenge, so a real Chrome (Selenium)
  loads the page once, lets the site's own code solve it, and we harvest the
  HttpOnly ``_dsid`` cookie (invisible to ``document.cookie``, but readable
  through WebDriver).
* **Everything else over plain HTTP.** The request signature is ported to
  Python (``DemoShopSigner``) so catalogue pages are fetched with httpx -
  ~100x cheaper than driving a browser per page.
* **Self-healing:** 401 -> bootstrap a new session; 403 E_CLOCK -> re-sync our
  clock from the server's ``Date`` header; 429 -> backoff (HTTP layer).
* **Graceful degradation:** if the server starts rejecting our signatures
  (the site shipped a new bundle), fall back to scraping the rendered DOM with
  Selenium, so data keeps flowing while the signer is updated.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Iterator
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import quote, urljoin

from django.conf import settings
from pydantic import ValidationError
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ..browser import chrome_driver
from ..http import CrawlerHttpClient
from ..items import ProductItem
from .base import StreamBatch, StreamSource, register

logger = logging.getLogger(__name__)

SESSION_COOKIE = "_dsid"
CATALOG_PATH = "/api/v2/catalog"
PAGE_SIZE = 60

# Copied verbatim from `_0x1a` in /static/app.js; de-obfuscated exactly the way
# the bundle does it in `_0xk()`: byte ^ (0x2a + index % 7).
OBFUSCATED_KEY = (
    0x4E,
    0x58,
    0x16,
    0x14,
    0x4D,
    0x1B,
    0x01,
    0x4F,
    0x1C,
    0x4E,
    0x1F,
    0x14,
    0x47,
    0x5D,
    0x4B,
    0x48,
    0x01,
    0x5B,
    0x1C,
)
URI_COMPONENT_SAFE = "-_.!~*'()"  # characters encodeURIComponent leaves alone


class DemoShopError(Exception):
    pass


class SignatureRejected(DemoShopError):
    """The API refused a correctly-timed signature: our port is out of date."""


def deobfuscate_key(data: tuple[int, ...] = OBFUSCATED_KEY) -> bytes:
    return bytes(b ^ (0x2A + i % 7) for i, b in enumerate(data))


class DemoShopSigner:
    """Python port of ``_sig()`` / ``_q()`` from the site's JS bundle.

    canonical = METHOD \\n PATH \\n sorted(k=encodeURIComponent(v)) joined by & \\n timestamp_ms
    X-DS-S    = hex(HMAC_SHA256(key, canonical)),   X-DS-T = timestamp_ms
    """

    def __init__(self, key: bytes | None = None):
        self.key = key or deobfuscate_key()

    @staticmethod
    def canonical_query(params: dict[str, object]) -> str:
        return "&".join(f"{k}={quote(str(params[k]), safe=URI_COMPONENT_SAFE)}" for k in sorted(params))

    def signature(self, method: str, path: str, params: dict[str, object], timestamp_ms: int | str) -> str:
        message = "\n".join([method.upper(), path, self.canonical_query(params), str(timestamp_ms)])
        return hmac.new(self.key, message.encode(), hashlib.sha256).hexdigest()

    def headers(self, method: str, path: str, params: dict[str, object], timestamp_ms: int) -> dict[str, str]:
        return {"X-DS-T": str(timestamp_ms), "X-DS-S": self.signature(method, path, params, timestamp_ms)}


class SessionProvider(Protocol):
    def new_session(self) -> dict[str, str]: ...


class BrowserSessionProvider:
    """Let the site's own JavaScript solve the challenge in headless Chrome."""

    def __init__(self, base_url: str, *, timeout: float = 30):
        self.base_url = base_url
        self.timeout = timeout

    def new_session(self) -> dict[str, str]:
        with chrome_driver(block_images=True) as driver:
            driver.get(self.base_url)
            WebDriverWait(driver, self.timeout).until(lambda d: d.get_cookie(SESSION_COOKIE))
            cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        logger.info("bootstrapped DemoShop session via headless Chrome")
        return cookies


class DemoShopApi:
    def __init__(
        self,
        base_url: str,
        http: CrawlerHttpClient,
        sessions: SessionProvider,
        signer: DemoShopSigner | None = None,
        clock=time.time,
    ):
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.sessions = sessions
        self.signer = signer or DemoShopSigner()
        self.clock = clock
        self.clock_offset = 0.0
        self.session_bootstraps = 0
        self._cookies: dict[str, str] | None = None

    def _now_ms(self) -> int:
        return int((self.clock() + self.clock_offset) * 1000)

    def _sync_clock(self, response) -> None:
        server_date = response.headers.get("Date")
        if server_date:
            self.clock_offset = parsedate_to_datetime(server_date).timestamp() - self.clock()
            logger.warning("clock skew detected, offset now %.1fs", self.clock_offset)

    def catalog_page(self, cursor: str | None, limit: int = PAGE_SIZE) -> dict:
        params: dict[str, object] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor

        for _ in range(3):
            if self._cookies is None:
                self._cookies = self.sessions.new_session()
                self.session_bootstraps += 1
            headers = {
                **self.signer.headers("GET", CATALOG_PATH, params, self._now_ms()),
                "Accept": "application/json",
                "Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items()),
            }
            url = f"{self.base_url}{CATALOG_PATH}?{self.signer.canonical_query(params)}"
            response = self.http.get(url, headers=headers)

            if response.status_code == 401:  # session expired / revoked
                self._cookies = None
                continue
            if response.status_code == 403:
                error = response.json().get("err")
                if error == "E_CLOCK":
                    self._sync_clock(response)
                    continue
                raise SignatureRejected(error or "403")
            response.raise_for_status()
            return response.json()
        raise DemoShopError("API still refusing requests after re-bootstrapping the session")


def to_item(raw: dict, base_url: str) -> ProductItem:
    stock = raw.get("stk")
    return ProductItem(
        external_id=raw["sku"],
        url=f"{base_url.rstrip('/')}/p/{raw['sku']}",
        title=raw["ttl"],
        brand=raw.get("brd") or "",
        category=raw.get("cat"),
        price=Decimal(raw["prc"]) / 100,
        currency=raw.get("cur") or "USD",
        in_stock=bool(stock and stock > 0),
        stock_quantity=stock,
        rating=raw.get("rtg") or None,
        image_url=urljoin(base_url, raw.get("img") or ""),
        description=raw.get("dsc") or "",
        attributes=raw.get("att") or {},
    )


def _convert(rows: list[dict], base_url: str) -> tuple[list[ProductItem], int]:
    items, invalid = [], 0
    for raw in rows:
        try:
            items.append(to_item(raw, base_url))
        except (ValidationError, KeyError, TypeError, ValueError):
            invalid += 1
    return items, invalid


# Runs inside the page: one round-trip returns every new card as plain data,
# instead of hundreds of find_element() calls over the WebDriver wire protocol.
EXTRACT_CARDS_JS = """
return Array.from(document.querySelectorAll('article.card')).slice(arguments[0]).map(function (el) {
  function text(sel) { var n = el.querySelector(sel); return n ? n.textContent.trim() : ''; }
  var price = el.querySelector('.card__price'), stock = el.querySelector('.card__stock'),
      rating = el.querySelector('.card__rating'), img = el.querySelector('img');
  return {
    sku: el.getAttribute('data-sku'), ttl: text('.card__title'), brd: text('.card__brand'), cat: text('.card__cat'),
    prc: parseInt(price.getAttribute('data-cents'), 10), cur: price.getAttribute('data-currency'),
    stk: parseInt(stock.getAttribute('data-qty'), 10), rtg: parseInt(rating.getAttribute('data-rating'), 10) || null,
    img: img ? img.getAttribute('src') : ''
  };
});
"""


def scrape_rendered_catalog(base_url: str, *, timeout: float = 20) -> Iterator[list[dict]]:
    """Fallback: drive the real UI ("Load more" until exhausted) and read the DOM."""
    card = (By.CSS_SELECTOR, "article.card")
    with chrome_driver(block_images=True) as driver:
        driver.get(base_url)
        wait = WebDriverWait(driver, timeout, ignored_exceptions=(StaleElementReferenceException,))
        wait.until(EC.presence_of_element_located(card))
        seen = 0
        while True:
            rows = driver.execute_script(EXTRACT_CARDS_JS, seen)
            seen += len(rows)
            if rows:
                yield rows
            button = driver.find_elements(By.ID, "load-more")
            if not button or not button[0].is_displayed():
                return
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button[0])
            wait.until(EC.element_to_be_clickable((By.ID, "load-more"))).click()
            wait.until(
                lambda d, seen=seen: (
                    len(d.find_elements(*card)) > seen or not d.find_element(By.ID, "load-more").is_displayed()
                )
            )


@register
class DemoShopSource(StreamSource):
    key = "demo_shop"
    # Swappable collaborators (tests inject fakes; nothing else needs a browser).
    session_provider_factory = BrowserSessionProvider
    signer_factory = DemoShopSigner
    dom_scraper = staticmethod(scrape_rendered_catalog)

    @property
    def base_url(self) -> str:
        return self.source.config.get("base_url") or settings.CRAWLER["DEMO_SHOP_URL"]

    def stream(self, run, http: CrawlerHttpClient, checkpoint: dict) -> Iterator[StreamBatch]:
        api = DemoShopApi(self.base_url, http, self.session_provider_factory(self.base_url), self.signer_factory())
        cursor = checkpoint.get("cursor")
        try:
            while True:
                payload = api.catalog_page(cursor)
                items, invalid = _convert(payload["data"], self.base_url)
                cursor = payload.get("next")
                yield StreamBatch(items, invalid, {"cursor": cursor, "mode": "api", "sessions": api.session_bootstraps})
                if not cursor:
                    return
        except SignatureRejected as exc:
            logger.error(
                "DemoShop rejected our request signature (%s) - the JS bundle probably changed. "
                "Degrading to browser rendering; update DemoShopSigner.",
                exc,
            )

        for rows in self.dom_scraper(self.base_url):
            items, invalid = _convert(rows, self.base_url)
            yield StreamBatch(items, invalid, {"cursor": None, "mode": "browser"})
