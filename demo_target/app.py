"""DemoShop - a deliberately crawler-unfriendly single-page storefront.

A local practice target that reproduces patterns found on real e-commerce
SPAs, so the crawler can be built and tested without hammering anyone's site:

* No server-rendered data: the page is an empty shell, products arrive via an
  undocumented JSON API (``/api/v2/catalog``) with minified field names and
  opaque cursor pagination.
* A session cookie is only issued after the browser solves a JavaScript
  challenge (a freshly generated expression per nonce - Cloudflare-IUAM style).
* Every API call must carry an HMAC-SHA256 request signature computed by the
  bundled JS with a key that is obfuscated inside the bundle.
* Per-session rate limiting (429 + Retry-After), clock-skew checks, session
  expiry, and optional random 5xx "chaos".
* Prices drift deterministically over time, so repeated crawls see changes.

None of this is real security - it exists to be reverse engineered
(see docs/reverse-engineering.md).
"""

import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_from_directory

HERE = Path(__file__).resolve().parent

SIGNING_KEY = b"ds:9c41e7b2:hmac-v2"  # also embedded (XOR-obfuscated) in static/app.js
SERVER_SECRET = os.environ.get("DEMO_SERVER_SECRET", "demo-server-secret").encode()
SESSION_COOKIE = "_dsid"
SESSION_TTL = int(os.environ.get("DEMO_SESSION_TTL", "600"))
PRICE_PERIOD = int(os.environ.get("DEMO_PRICE_PERIOD_SECONDS", "86400"))
CHAOS_RATE = float(os.environ.get("DEMO_CHAOS_RATE", "0"))
RATE_LIMIT_PER_SEC = float(os.environ.get("DEMO_RATE_LIMIT", "10"))
CATALOG_SIZE = int(os.environ.get("DEMO_CATALOG_SIZE", "600"))
MAX_CLOCK_SKEW_MS = 60_000
MAX_PAGE_SIZE = 60

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")

# ---------------------------------------------------------------------------
# Deterministic catalogue
# ---------------------------------------------------------------------------
CATEGORIES = {
    "Headphones": (
        ["Wireless Headphones", "Noise-Cancelling Earbuds", "Studio Headphones", "Sport Earbuds"],
        4900,
        39900,
    ),
    "Laptops": (["Ultrabook 14", "Gaming Laptop 16", "Convertible 2-in-1", "Creator Laptop 15"], 69900, 329900),
    "Smartwatches": (["Fitness Watch", "GPS Running Watch", "Hybrid Smartwatch", "Kids Watch"], 7900, 79900),
    "Cameras": (["Mirrorless Camera", "Action Camera", "Instant Camera", "Vlogging Kit"], 12900, 249900),
    "Keyboards": (
        ["Mechanical Keyboard", "Low-Profile Keyboard", "Ergonomic Split Keyboard", "Wireless Keypad"],
        2900,
        24900,
    ),
    "Monitors": (["4K Monitor 27", "Ultrawide Monitor 34", "Portable Monitor 15", "Gaming Monitor 24"], 12900, 149900),
}
BRANDS = ["Auralis", "Northwind", "Kestrel", "Lumio", "Vantage", "Orbitron", "Halcyon", "Pinecrest", "Zephyr", "Quanta"]
SUFFIXES = ["Pro", "Max", "Air", "Lite", "Plus", "X", "Ultra", "Neo", "S", "Edge"]
FEATURES = [
    "all-day battery life",
    "a machined aluminium chassis",
    "USB-C fast charging",
    "a two-year warranty",
    "low-latency Bluetooth 5.3",
    "a factory-calibrated display",
    "recycled packaging",
    "IP54 splash resistance",
    "multi-device pairing",
    "a companion app with firmware updates",
]
COLORS = ["black", "silver", "white", "midnight blue", "graphite", "sand"]


def _build_catalog(size: int) -> list[dict]:
    rng = random.Random(1337)
    names = list(CATEGORIES)
    products = []
    for pid in range(1, size + 1):
        category = names[pid % len(names)]
        models, low, high = CATEGORIES[category]
        brand = rng.choice(BRANDS)
        title = f"{brand} {rng.choice(models)} {rng.choice(SUFFIXES)} {rng.randint(2, 9)}"
        features = rng.sample(FEATURES, 3)
        products.append(
            {
                "id": pid,
                "sku": f"DS-{pid:05d}",
                "title": title,
                "brand": brand,
                "category": category,
                "base_cents": rng.randrange(low, high, 100) - 1,
                "phase": rng.randrange(0, PRICE_PERIOD),
                "rating": rng.choice([3, 4, 4, 4, 5, 5]),
                "description": f"The {title} pairs {features[0]} with {features[1]} and {features[2]}.",
                "attributes": {
                    "color": rng.choice(COLORS),
                    "weight_g": rng.randrange(40, 2600, 5),
                    "warranty_years": rng.choice([1, 2, 3]),
                },
            }
        )
    return products


CATALOG = _build_catalog(CATALOG_SIZE)


def _h32(*parts) -> int:
    return int.from_bytes(hashlib.sha256(":".join(map(str, parts)).encode()).digest()[:4], "big")


def live_price_cents(product: dict, now: float) -> int:
    epoch = int((now + product["phase"]) // PRICE_PERIOD)
    h = _h32(product["sku"], epoch)
    factor = 1.0 if h % 3 == 0 else 1 + ((h >> 8) % 31 - 18) / 100  # -18% .. +12%
    return max(99, round(product["base_cents"] * factor / 100) * 100 - 1)


def live_stock(product: dict, now: float) -> int:
    h = _h32(product["sku"], "stock", int((now + product["phase"]) // PRICE_PERIOD))
    return 0 if h % 9 == 0 else (h >> 4) % 60 + 1


def serialize(product: dict, now: float) -> dict:
    # Minified field names, as shipped by many production APIs.
    return {
        "id": product["id"],
        "sku": product["sku"],
        "ttl": product["title"],
        "brd": product["brand"],
        "cat": product["category"],
        "prc": live_price_cents(product, now),
        "cur": "USD",
        "stk": live_stock(product, now),
        "rtg": product["rating"],
        "img": f"/img/{product['sku']}.svg",
        "dsc": product["description"],
        "att": product["attributes"],
        "upd": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# JS challenge + session cookie
# ---------------------------------------------------------------------------
def make_challenge(nonce: str) -> tuple[str, int]:
    """Return (javascript expression, expected answer) for a nonce.

    Derived from HMAC(server secret, nonce), so it is stateless to verify but
    unpredictable to a client that doesn't execute the JavaScript.
    """
    rng = random.Random(hmac.new(SERVER_SECRET, nonce.encode(), "sha256").digest())
    a = rng.randint(1000, 9999)
    b = [rng.randint(1, 255) for _ in range(rng.randint(4, 8))]
    mult = rng.choice([31, 33, 37, 41])
    mod = rng.choice([1000003, 999983, 1000033])
    c = 0
    for x in b:
        c = ((c * mult + x) ^ a) % mod
    answer = c + ((a >> 3) & 0xFF)
    js = (
        f"(function(){{var _a=0x{a:x},_b=[{','.join(hex(x) for x in b)}],_c=0;"
        f"for(var _i=0;_i<_b.length;_i++){{_c=((_c*{mult}+_b[_i])^_a)%{mod}}}"
        f"return _c+((_a>>3)&0xff)}})()"
    )
    return js, answer


def _sign_session(sid: str, exp: int) -> str:
    return hmac.new(SERVER_SECRET, f"{sid}.{exp}".encode(), "sha256").hexdigest()[:32]


def issue_session(ttl: int | None = None) -> str:
    """Mint a session cookie value (also used by the test-suite)."""
    sid = secrets.token_hex(12)
    exp = int(time.time()) + (SESSION_TTL if ttl is None else ttl)
    return f"{sid}.{exp}.{_sign_session(sid, exp)}"


def session_from_cookie(value: str | None) -> str | None:
    try:
        sid, exp, sig = (value or "").split(".")
        if hmac.compare_digest(sig, _sign_session(sid, int(exp))) and int(exp) > time.time():
            return sid
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# Request signing (mirrors _sig() in static/app.js)
# ---------------------------------------------------------------------------
URI_COMPONENT_SAFE = "-_.!~*'()"  # what JavaScript's encodeURIComponent leaves unescaped


def canonical_query(params: dict) -> str:
    return "&".join(f"{k}={quote(str(params[k]), safe=URI_COMPONENT_SAFE)}" for k in sorted(params))


def expected_signature(method: str, path: str, params: dict, timestamp: str) -> str:
    message = "\n".join([method.upper(), path, canonical_query(params), timestamp])
    return hmac.new(SIGNING_KEY, message.encode(), "sha256").hexdigest()


# ---------------------------------------------------------------------------
# Rate limiting (per session token bucket)
# ---------------------------------------------------------------------------
_buckets: dict[str, tuple[float, float]] = {}
_buckets_lock = threading.Lock()


def allow_request(sid: str) -> bool:
    burst = RATE_LIMIT_PER_SEC
    now = time.monotonic()
    with _buckets_lock:
        tokens, updated = _buckets.get(sid, (burst, now))
        tokens = min(burst, tokens + (now - updated) * RATE_LIMIT_PER_SEC)
        allowed = tokens >= 1
        _buckets[sid] = (tokens - 1 if allowed else tokens, now)
    return allowed


def _error(code: str, status: int, **headers) -> Response:
    response = jsonify({"err": code})
    response.status_code = status
    for key, value in headers.items():
        response.headers[key.replace("_", "-")] = value
    return response


def _encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"a": last_id, "v": 2}).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    return int(json.loads(base64.urlsafe_b64decode(padded))["a"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.after_request
def _no_store_api(response: Response) -> Response:
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
@app.get("/p/<sku>")
def index(sku: str | None = None):
    return send_from_directory(app.static_folder, "index.html")


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin/\n", mimetype="text/plain")


@app.get("/img/<sku>.svg")
def image(sku: str):
    hue = _h32(sku) % 360
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">'
        f'<rect width="100%" height="100%" fill="hsl({hue},45%,72%)"/>'
        f'<text x="50%" y="50%" text-anchor="middle" font-family="sans-serif" font-size="22">{sku}</text></svg>'
    )
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/v2/bootstrap")
def bootstrap_challenge():
    nonce = f"{int(time.time()) + 120}.{secrets.token_hex(8)}"
    js, _ = make_challenge(nonce)
    return jsonify({"n": nonce, "c": js})


@app.post("/api/v2/bootstrap")
def bootstrap_verify():
    body = request.get_json(silent=True) or {}
    nonce, answer = str(body.get("n", "")), body.get("a")
    try:
        expires = int(nonce.split(".", 1)[0])
    except ValueError:
        return _error("E_CHALLENGE", 400)
    if expires < time.time():
        return _error("E_CHALLENGE_EXPIRED", 403)
    if answer != make_challenge(nonce)[1]:
        return _error("E_CHALLENGE", 403)

    response = jsonify({"ok": 1, "ttl": SESSION_TTL})
    response.set_cookie(SESSION_COOKIE, issue_session(), max_age=SESSION_TTL, httponly=True, samesite="Lax")
    return response


@app.get("/api/v2/catalog")
def catalog():
    if CHAOS_RATE and random.random() < CHAOS_RATE:
        return _error("E_UPSTREAM", 503)

    sid = session_from_cookie(request.cookies.get(SESSION_COOKIE))
    if sid is None:
        return _error("E_SESSION", 401)

    timestamp = request.headers.get("X-DS-T", "")
    signature = request.headers.get("X-DS-S", "")
    if not timestamp.isdigit() or abs(time.time() * 1000 - int(timestamp)) > MAX_CLOCK_SKEW_MS:
        return _error("E_CLOCK", 403)
    params = request.args.to_dict()
    if not hmac.compare_digest(signature, expected_signature("GET", request.path, params, timestamp)):
        return _error("E_SIG", 403)

    if not allow_request(sid):
        return _error("E_RATE", 429, Retry_After="1")

    try:
        limit = max(1, min(int(params.get("limit", 24)), MAX_PAGE_SIZE))
        after = _decode_cursor(params["cursor"]) if params.get("cursor") else 0
    except (ValueError, KeyError, json.JSONDecodeError):
        return _error("E_PARAM", 400)

    now = time.time()
    page = [p for p in CATALOG if p["id"] > after][:limit]  # keyset pagination
    has_more = bool(page) and page[-1]["id"] < CATALOG[-1]["id"]
    return jsonify(
        {
            "data": [serialize(p, now) for p in page],
            "next": _encode_cursor(page[-1]["id"]) if has_more else None,
            "total": len(CATALOG),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5050")), threaded=True)
