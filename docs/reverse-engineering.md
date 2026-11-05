# Reverse engineering DemoShop's private API

DemoShop ([demo_target/](../demo_target)) renders nothing on the server: the HTML is an
empty shell and products arrive through XHR calls. The goal is to collect the full
catalogue *without* driving a browser for every page. This is the workflow, in the
order it was done.

## 1. Observe the traffic

Manually: DevTools → Network → Fetch/XHR, reload, click "Load more". Automated with
the `discover_api` command, which records Chrome DevTools Protocol events
(`Network.requestWillBeSent`, `…ExtraInfo`, `responseReceived`, `loadingFinished`) from
ChromeDriver's performance log and pulls response bodies with `Network.getResponseBody`:

```text
$ python manage.py discover_api http://127.0.0.1:5050/ --click "#load-more" --clicks 2 --verify-signer

5 XHR/fetch calls captured
GET /api/v2/bootstrap
  calls: 1   statuses: [200]
  response shape: {"c": "str", "n": "str"}

POST /api/v2/bootstrap
  calls: 1   statuses: [200]
  response shape: {"ok": "int", "ttl": "int"}

GET /api/v2/catalog
  calls: 3   statuses: [200]
  query params: cursor, limit
  non-standard request headers: cookie, x-ds-s, x-ds-t
  response shape:
    {"data": [{"id": "int", "sku": "str", "ttl": "str", "brd": "str", "cat": "str",
               "prc": "int", "cur": "str", "stk": "int", "rtg": "int", "img": "str",
               "dsc": "str", "att": {...}, "upd": "str"}],
     "next": "str", "total": "int"}
```

A sanitized capture (cookies redacted) is in [api-captures/demo_shop.json](api-captures/demo_shop.json).

Observations:

* The data endpoint is `GET /api/v2/catalog` with `limit` and an opaque `cursor`.
* Two custom headers: `X-DS-T` (looks like a millisecond timestamp) and `X-DS-S`
  (64 hex chars → probably SHA-256 / HMAC-SHA256).
* A `_dsid` cookie is required; it is set by `POST /api/v2/bootstrap` and is `HttpOnly`.
* Field names are minified: `ttl`=title, `brd`=brand, `prc`=price **in cents**,
  `stk`=stock count, `rtg`=rating, `dsc`=description, `att`=attributes.

## 2. Probe the contract by replaying requests

| Replay | Response | Conclusion |
|---|---|---|
| without the cookie | `401 {"err":"E_SESSION"}` | session required |
| with cookie, original headers, a minute later | `403 {"err":"E_CLOCK"}` | `X-DS-T` is checked against server time (±60 s) |
| with cookie, fresh `X-DS-T`, old `X-DS-S` | `403 {"err":"E_SIG"}` | the signature covers the timestamp |
| signature re-used with a different `cursor` | `403 {"err":"E_SIG"}` | the signature covers the query string |
| many requests quickly | `429 {"err":"E_RATE"}`, `Retry-After: 1` | per-session rate limit (~10 rps) |

The cursor is base64url: `eyJhIjogMjQsICJ2IjogMn0` → `{"a": 24, "v": 2}`. It is
keyset pagination ("after id 24"), so pages can't be fetched in parallel, but a
crawl can resume from any saved cursor.

## 3. Read the bundle

`/static/app.js` is minified. Searching it for the header name leads to the signer:

```js
function _q(o){return Object.keys(o).sort().map(function(k){return k+"="+encodeURIComponent(o[k])}).join("&")}
function _sig(m,p,o){var t=String(Date.now());return{t:t,s:_x(_m(_0xk(),_u([m,p,_q(o),t].join("\n"))))}}
```

* `_q` - canonical query: keys sorted, values `encodeURIComponent`-escaped, joined by `&`.
* The signed message is `METHOD \n PATH \n canonical_query \n timestamp`.
* `_m(key, msg)` is an HMAC: it XORs the key with `0x36` / `0x5c` (the HMAC ipad/opad)
  around `_h`, and `_h` initialises its state with `0x6a09e667, 0xbb67ae85…` - the
  SHA-256 initial hash values. So: **HMAC-SHA256**, hex encoded by `_x`.
* The key comes from `_0xk()`:

```js
var _0x1a=[0x4e,0x58,0x16,0x14,0x4d,0x1b,0x01,0x4f,0x1c,0x4e,0x1f,0x14,0x47,0x5d,0x4b,0x48,0x01,0x5b,0x1c];
function _0xk(){for(var o=[],i=0;i<_0x1a.length;i++)o.push(_0x1a[i]^(0x2a+i%7));return o}
```

i.e. `key[i] = obfuscated[i] XOR (0x2a + i % 7)`.

## 4. Port it and prove it

The Python port is `DemoShopSigner` in
[apps/crawler/sources/demo_shop.py](../apps/crawler/sources/demo_shop.py). Two details
matter for byte-exact signatures:

* `encodeURIComponent` leaves `A-Z a-z 0-9 - _ . ! ~ * ' ( )` unescaped, so the port
  uses `urllib.parse.quote(value, safe="-_.!~*'()")`.
* JavaScript sorts keys by UTF-16 code units; Python by code points. Identical for the
  ASCII keys used here.

Proof, against signatures produced by the real browser:

```text
$ python manage.py discover_api http://127.0.0.1:5050/ --click "#load-more" --clicks 2 --verify-signer
  match  GET /api/v2/catalog?limit=24
  match  GET /api/v2/catalog?cursor=eyJhIjogMjQsICJ2IjogMn0&limit=24
  match  GET /api/v2/catalog?cursor=eyJhIjogNDgsICJ2IjogMn0&limit=24
signer reproduced 3/3 captured signatures
```

The same check runs in the test-suite (`tests/test_selenium_e2e.py`), and
`tests/test_demo_shop.py` runs the client against the server's own verification code.

## 5. The session: run the site's JavaScript instead of porting it

`GET /api/v2/bootstrap` returns a nonce and a JavaScript *expression* that changes on
every call:

```json
{"n": "1762358520.3f9c…", "c": "(function(){var _a=0x1f2b,_b=[0x11,0x9a,…],_c=0;for(…){_c=((_c*37+_b[_i])^_a)%999983}return _c+((_a>>3)&0xff)})()"}
```

The bundle evaluates it and POSTs the answer, and the server sets the `HttpOnly`
`_dsid` cookie (10 minutes). This *could* be ported (or evaluated with an embedded JS
engine), but it is the part of a real site most likely to change and to grow
fingerprinting. The pragmatic split:

* **Chrome (Selenium) bootstraps the session.** Load the page, wait until the cookie
  exists, read it with `driver.get_cookies()` - WebDriver exposes `HttpOnly` cookies
  that `document.cookie` cannot see. About 3 s, once per session.
* **httpx fetches the data.** All catalogue pages are signed in Python and fetched over
  pooled keep-alive connections; 600 products in ~1.5 s after bootstrap.

## 6. Make it robust

| Signal | Reaction (in `DemoShopApi` / `DemoShopSource`) |
|---|---|
| `401 E_SESSION` | drop the cookie, bootstrap a new session in Chrome, retry |
| `403 E_CLOCK` | compute our clock offset from the response's `Date` header, retry |
| `429` + `Retry-After` | HTTP layer waits as told and halves the host rate |
| `5xx` | retried with jittered exponential backoff |
| `403 E_SIG` | the bundle changed: log loudly and **degrade to DOM scraping** - Selenium clicks "Load more" until it disappears and extracts all cards with one `execute_script` call per page (not one WebDriver round-trip per field) |
| crash mid-run | the cursor is checkpointed after every page; `crawl demo_shop --resume` |

A real bug found by the browser tests: the DOM fallback hung at the end of the list
because the page's CSS (`#load-more { display: block }`) overrode the `hidden`
attribute, so the button never disappeared. The site's CSS was fixed, and the scraper
now treats "clicked, but no new cards appeared" as the end of the list - many real
sites leave a dead button there.

## Ethics

The crawler identifies itself (`PriceLensBot/1.0 (+repo URL)`), obeys robots.txt and
`Crawl-delay`, rate-limits per host and backs off on 429/5xx. DemoShop exists so the
reverse-engineering techniques can be demonstrated on a target we own; the public
target (books.toscrape.com) is a sandbox published for scraping practice.
