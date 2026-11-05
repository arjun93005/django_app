"""Validated, source-independent representation of a scraped product.

Scraped data is untrusted input: every source parser produces ``ProductItem``
objects and anything that fails validation is counted and dropped instead of
poisoning the catalog (or crashing the run on a DB constraint).
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "₹": "INR"}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_money(text: str) -> tuple[Decimal, str | None]:
    """'£1,051.77' -> (Decimal('1051.77'), 'GBP')."""
    match = _NUMBER.search(text or "")
    if not match:
        raise ValueError(f"no amount in {text!r}")
    try:
        amount = Decimal(match.group().replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"bad amount in {text!r}") from exc
    currency = next((code for symbol, code in CURRENCY_SYMBOLS.items() if symbol in text), None)
    return amount, currency


class ProductItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, frozen=True)

    external_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=600)
    title: str = Field(min_length=1)
    price: Decimal = Field(ge=0, lt=Decimal("1e10"))
    currency: str = Field(min_length=3, max_length=3)
    in_stock: bool
    stock_quantity: int | None = Field(default=None, ge=0)
    brand: str = Field(default="", max_length=120)
    description: str = ""
    category: str | None = Field(default=None, max_length=120)
    image_url: str = Field(default="", max_length=600)
    rating: int | None = Field(default=None, ge=1, le=5)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _truncate_title(cls, value: str) -> str:
        return value[:300]

    @field_validator("price")
    @classmethod
    def _two_decimals(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()
