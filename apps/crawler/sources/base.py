"""Source plug-in interface.

Two crawl strategies are supported:

* ``FrontierSource`` - classic link-following crawl. Pages are queued in the
  Postgres frontier and processed by N concurrent workers (threads locally,
  Celery tasks in production).
* ``StreamSource`` - the data comes from an API that must be paged
  sequentially (cursor pagination); the source just yields items and a
  checkpoint so an interrupted run can resume.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ..frontier import ClaimedURL, Link
    from ..http import CrawlerHttpClient
    from ..items import ProductItem
    from ..models import CrawlRun, Source

REGISTRY: dict[str, type[BaseSource]] = {}


def register(cls: type[BaseSource]) -> type[BaseSource]:
    REGISTRY[cls.key] = cls
    return cls


def get_source_class(source: Source) -> type[BaseSource]:
    try:
        return REGISTRY[source.implementation]
    except KeyError as exc:
        raise LookupError(f"no crawler registered as {source.implementation!r} (known: {sorted(REGISTRY)})") from exc


@dataclass
class PageResult:
    status_code: int
    items: list[ProductItem] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    invalid_items: int = 0
    etag: str = ""
    last_modified: str = ""


class BaseSource(ABC):
    key: ClassVar[str]
    strategy: ClassVar[str]

    def __init__(self, source: Source):
        self.source = source


class FrontierSource(BaseSource):
    strategy = "frontier"

    @abstractmethod
    def seeds(self) -> list[Link]: ...

    @abstractmethod
    def process(self, entry: ClaimedURL, http: CrawlerHttpClient) -> PageResult:
        """Fetch + parse one frontier URL."""


@dataclass
class StreamBatch:
    items: list[ProductItem]
    invalid_items: int
    checkpoint: dict


class StreamSource(BaseSource):
    strategy = "stream"

    @abstractmethod
    def stream(self, run: CrawlRun, http: CrawlerHttpClient, checkpoint: dict) -> Iterator[StreamBatch]: ...
