"""The reverse-engineered DemoShop client, tested against the real target app.

The Flask app is mounted in-process through ``httpx.WSGITransport``: the
crawler's HTTP stack, signer and session handling run unmodified against the
server's actual verification code, with no sockets and no browser.
"""

import time
from email.utils import formatdate

import httpx
import pytest

from apps.catalog.models import Product
from apps.crawler.http import CrawlerHttpClient
from apps.crawler.models import CrawlRun
from apps.crawler.runner import run_stream
from apps.crawler.sources.demo_shop import (
    DemoShopApi,
    DemoShopSigner,
    DemoShopSource,
    SignatureRejected,
    deobfuscate_key,
)
from demo_target import app as demo

from .conftest import make_source

BASE = "http://demo.test"


class DatedWSGITransport(httpx.WSGITransport):
    """Real HTTP servers add a Date header; a bare WSGI app does not."""

    def handle_request(self, request):
        response = super().handle_request(request)
        response.headers["Date"] = formatdate(usegmt=True)
        return response


class MintedSessions:
    """Stands in for the Selenium bootstrap: mints cookies the way the server would."""

    def __init__(self, *ttls: int):
        self.ttls = list(ttls) or [600]
        self.calls = 0

    def __call__(self, base_url):  # used as session_provider_factory
        return self

    def new_session(self) -> dict[str, str]:
        ttl = self.ttls[min(self.calls, len(self.ttls) - 1)]
        self.calls += 1
        return {demo.SESSION_COOKIE: demo.issue_session(ttl=ttl)}


def http_client() -> CrawlerHttpClient:
    return CrawlerHttpClient(
        user_agent="t",
        respect_robots=False,
        rate_per_host=10_000,
        transport=DatedWSGITransport(app=demo.app),
        sleep=lambda s: None,
    )


@pytest.fixture(autouse=True)
def _no_rate_limit_no_chaos(monkeypatch):
    monkeypatch.setattr(demo, "RATE_LIMIT_PER_SEC", 1_000_000.0)
    monkeypatch.setattr(demo, "CHAOS_RATE", 0.0)


def test_deobfuscated_key_matches_server_key():
    assert deobfuscate_key() == demo.SIGNING_KEY


@pytest.mark.parametrize(
    "params",
    [{}, {"limit": 24}, {"limit": "60", "cursor": "eyJhIjogMjQsICJ2IjogMn0"}, {"q": "noise cancelling & 'pro'!"}],
)
def test_signer_agrees_with_server_verification(params):
    ts = str(int(time.time() * 1000))
    ours = DemoShopSigner().signature("GET", "/api/v2/catalog", params, ts)
    assert ours == demo.expected_signature("GET", "/api/v2/catalog", {k: str(v) for k, v in params.items()}, ts)


def test_paginates_the_whole_catalog_with_signed_requests():
    api = DemoShopApi(BASE, http_client(), MintedSessions())
    cursor, rows = None, []
    while True:
        page = api.catalog_page(cursor)
        rows += page["data"]
        cursor = page["next"]
        if not cursor:
            break
    assert len(rows) == len(demo.CATALOG)
    assert len({r["sku"] for r in rows}) == len(rows)


def test_expired_session_is_rebootstrapped():
    sessions = MintedSessions(-1, 600)  # first cookie is already expired
    api = DemoShopApi(BASE, http_client(), sessions)
    assert api.catalog_page(None)["data"]
    assert sessions.calls == 2


def test_clock_skew_is_corrected_from_server_date_header():
    skewed = lambda: time.time() + 3600  # noqa: E731 - our clock is an hour fast
    api = DemoShopApi(BASE, http_client(), MintedSessions(), clock=skewed)
    assert api.catalog_page(None)["data"]
    assert api.clock_offset == pytest.approx(-3600, abs=5)


def test_wrong_key_is_reported_as_signature_rejected():
    api = DemoShopApi(BASE, http_client(), MintedSessions(), signer=DemoShopSigner(key=b"stale-key"))
    with pytest.raises(SignatureRejected):
        api.catalog_page(None)


@pytest.mark.django_db
def test_stream_run_ingests_catalog_and_checkpoints(monkeypatch):
    source = make_source("demo_shop", implementation="demo_shop", config={"base_url": BASE})
    monkeypatch.setattr(DemoShopSource, "session_provider_factory", MintedSessions())
    run = CrawlRun.objects.create(source=source)

    run_stream(run, http=http_client())

    run.refresh_from_db()
    assert Product.objects.filter(source=source).count() == len(demo.CATALOG)
    assert run.items_scraped == len(demo.CATALOG)
    assert run.products_created == len(demo.CATALOG)
    assert run.checkpoint == {"cursor": None, "mode": "api", "sessions": 1}
    assert run.http_status == {"200": run.pages_fetched}


@pytest.mark.django_db
def test_resume_from_checkpoint_skips_finished_pages(monkeypatch):
    source = make_source("demo_shop", implementation="demo_shop", config={"base_url": BASE})
    monkeypatch.setattr(DemoShopSource, "session_provider_factory", MintedSessions())
    cursor_after_540 = demo._encode_cursor(540)
    run = CrawlRun.objects.create(source=source)

    run_stream(run, http=http_client(), checkpoint={"cursor": cursor_after_540})

    assert Product.objects.filter(source=source).count() == len(demo.CATALOG) - 540


@pytest.mark.django_db
def test_falls_back_to_dom_scraping_when_signature_is_rejected(monkeypatch):
    source = make_source("demo_shop", implementation="demo_shop", config={"base_url": BASE})
    monkeypatch.setattr(DemoShopSource, "session_provider_factory", MintedSessions())
    monkeypatch.setattr(DemoShopSource, "signer_factory", staticmethod(lambda: DemoShopSigner(key=b"bundle-changed")))
    rendered = [{"sku": "DS-00001", "ttl": "From the DOM", "prc": 1999, "cur": "USD", "stk": 3, "rtg": 4}]
    monkeypatch.setattr(DemoShopSource, "dom_scraper", staticmethod(lambda base_url: iter([rendered])))
    run = CrawlRun.objects.create(source=source)

    run_stream(run, http=http_client())

    run.refresh_from_db()
    assert Product.objects.get(source=source).title == "From the DOM"
    assert run.checkpoint["mode"] == "browser"
