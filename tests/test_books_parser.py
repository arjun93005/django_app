from decimal import Decimal

import httpx

from apps.crawler.frontier import ClaimedURL
from apps.crawler.http import CrawlerHttpClient
from apps.crawler.sources.books_toscrape import BooksToScrapeSource, parse_detail, parse_listing

from .conftest import FIXTURES

BASE = "https://books.toscrape.com/catalogue/page-2.html"
DETAIL_URL = "https://books.toscrape.com/catalogue/the-quiet-compiler_42/index.html"


def test_parse_listing_resolves_relative_links_and_pagination():
    links = parse_listing((FIXTURES / "books/listing.html").read_text(encoding="utf-8"), BASE)

    assert [(link.url, link.kind) for link in links] == [
        ("https://books.toscrape.com/catalogue/the-quiet-compiler_42/index.html", "detail"),
        ("https://books.toscrape.com/catalogue/loud-linker_43/index.html", "detail"),  # fragment dropped
        ("https://books.toscrape.com/catalogue/page-3.html", "listing"),
    ]
    assert links[-1].priority > links[0].priority


def test_parse_detail_extracts_a_valid_item():
    product = parse_detail((FIXTURES / "books/detail.html").read_text(encoding="utf-8"), DETAIL_URL)

    assert product.external_id == "f00dfeedbeef0001"
    assert product.title == "The Quiet Compiler"
    assert product.price == Decimal("1051.77")
    assert product.currency == "GBP"
    assert (product.in_stock, product.stock_quantity, product.rating) == (True, 7, 4)
    assert product.category == "Science"
    assert product.image_url == "https://books.toscrape.com/media/cache/ab/cd/abcd1234.jpg"
    assert product.attributes["reviews"] == 3


def _entry(kind: str, url: str = DETAIL_URL, etag: str = "") -> ClaimedURL:
    return ClaimedURL(id=1, url=url, kind=kind, depth=1, attempts=1, etag=etag, last_modified="")


def test_process_handles_304_without_parsing(source):
    client = CrawlerHttpClient(
        user_agent="t", respect_robots=False, transport=httpx.MockTransport(lambda r: httpx.Response(304))
    )
    result = BooksToScrapeSource(source).process(_entry("detail", etag='"x"'), client)
    assert result.status_code == 304
    assert result.items == []


def test_process_counts_invalid_pages_instead_of_crashing(source):
    client = CrawlerHttpClient(
        user_agent="t",
        respect_robots=False,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html><h1>moved</h1></html>")),
    )
    result = BooksToScrapeSource(source).process(_entry("detail"), client)
    assert (result.items, result.invalid_items) == ([], 1)
