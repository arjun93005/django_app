"""Crawler implementations. Importing this package registers them in REGISTRY."""

from . import books_toscrape  # noqa: F401
from .base import REGISTRY, get_source_class  # noqa: F401
