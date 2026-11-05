"""books.toscrape.com - a server-rendered catalogue (a sandbox built for scraping practice).

Crawl shape: listing pages (``catalogue/page-N.html``) link to product pages
and to the next listing page. Listing pages get a higher priority so link
discovery stays ahead of the detail-page workers.

The site serves ETag/Last-Modified, so re-crawls send conditional requests and
unchanged pages come back as bodyless 304s.
"""

from __future__ import annotations

from parsel import Selector
from pydantic import ValidationError

from ..frontier import ClaimedURL, Link
from ..http import CrawlerHttpClient
from ..items import ProductItem, parse_money
from ..urls_util import canonicalize_url
from .base import FrontierSource, PageResult, register

RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

LISTING = "listing"
DETAIL = "detail"


def parse_listing(html: str, url: str) -> list[Link]:
    sel = Selector(text=html)
    links = [
        Link(canonicalize_url(href, base=url), kind=DETAIL, priority=5)
        for href in sel.css("article.product_pod h3 a::attr(href)").getall()
    ]
    next_href = sel.css("li.next a::attr(href)").get()
    if next_href:
        links.append(Link(canonicalize_url(next_href, base=url), kind=LISTING, priority=10))
    return links


def parse_detail(html: str, url: str) -> ProductItem:
    sel = Selector(text=html)
    table = {
        row.css("th::text").get("").strip(): row.css("td::text").get("").strip()
        for row in sel.css("table.table-striped tr")
    }
    price, currency = parse_money(sel.css("div.product_main p.price_color::text").get(""))

    availability = table.get("Availability", "")
    quantity = None
    if "(" in availability:
        digits = "".join(ch for ch in availability.split("(", 1)[1] if ch.isdigit())
        quantity = int(digits) if digits else None

    rating_class = sel.css("div.product_main p.star-rating::attr(class)").get("")
    rating = next((value for word, value in RATINGS.items() if word in rating_class.split()), None)

    image = sel.css("div.item.active img::attr(src), #product_gallery img::attr(src)").get("")

    return ProductItem(
        external_id=table.get("UPC", ""),
        url=url,
        title=sel.css("div.product_main h1::text").get(""),
        price=price,
        currency=currency or "GBP",
        in_stock=availability.lower().startswith("in stock"),
        stock_quantity=quantity,
        description=sel.css("#product_description + p::text").get(""),
        category=sel.css("ul.breadcrumb li:nth-child(3) a::text").get(),
        image_url=canonicalize_url(image, base=url) if image else "",
        rating=rating,
        attributes={
            "product_type": table.get("Product Type"),
            "price_excl_tax": table.get("Price (excl. tax)"),
            "tax": table.get("Tax"),
            "reviews": int(table["Number of reviews"]) if table.get("Number of reviews", "").isdigit() else None,
        },
    )


@register
class BooksToScrapeSource(FrontierSource):
    key = "books_toscrape"

    def seeds(self) -> list[Link]:
        base = self.source.base_url
        return [Link(canonicalize_url("catalogue/page-1.html", base=base), kind=LISTING, priority=10)]

    def process(self, entry: ClaimedURL, http: CrawlerHttpClient) -> PageResult:
        response = http.get(entry.url, etag=entry.etag, last_modified=entry.last_modified)
        result = PageResult(
            status_code=response.status_code,
            etag=response.headers.get("ETag", ""),
            last_modified=response.headers.get("Last-Modified", ""),
        )
        if response.status_code == 304:
            return result
        response.raise_for_status()

        # The site omits the charset in Content-Type; it is UTF-8.
        html = response.content.decode("utf-8", errors="replace")
        if entry.kind == DETAIL:
            try:
                result.items.append(parse_detail(html, entry.url))
            except (ValidationError, ValueError):
                result.invalid_items += 1
        else:
            result.links = parse_listing(html, entry.url)
        return result
