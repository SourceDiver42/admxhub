"""Find the latest version and download URL of a product's templates.

The tool is vendor-neutral: it only knows four generic discovery methods. Every URL,
pattern and field mapping lives in the source definitions (admxhub/sources/*.yaml).

  static    fixed download URL; version from HTTP headers (or later from the archive)
  redirect  follow a link; version from the final file name
  json      query a JSON API with JMESPath (e.g. GitHub releases, vendor update APIs)
  html      scan a download page for a link matching a pattern
"""
from __future__ import annotations

import email.utils
import os
import re
import string
import urllib.parse
from dataclasses import dataclass

import httpx2
import jmespath

from . import __version__
from .sources import Source

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
UA = f"admxhub/{__version__}"


class DiscoveryError(RuntimeError):
    pass


@dataclass
class Release:
    version: str
    url: str
    date: str = ""
    size: int | None = None
    sha256: str | None = None  # when the vendor publishes it
    etag: str = ""
    fingerprint: str = ""      # set after download: identity of the downloaded file

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


def _headers(d: dict) -> dict:
    """User-Agent plus optional headers from the source; ${VAR} is taken from the environment and
    a header whose variable is unset is left out (e.g. an optional API token)."""
    h = {"User-Agent": BROWSER_UA if d.get("browser") else UA}
    for k, v in (d.get("headers") or {}).items():
        names = re.findall(r"\$\{(\w+)\}", str(v))
        if all(os.environ.get(n) for n in names):
            h[k] = string.Template(str(v)).substitute(os.environ)
    return h


def _client(d: dict) -> httpx2.Client:
    return httpx2.Client(headers=_headers(d), follow_redirects=True, timeout=60)


def _http_date(value: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _size(headers) -> int | None:
    v = headers.get("content-length", "")
    return int(v) if v.isdigit() else None


def _version_from_name(d: dict, name: str) -> str:
    m = re.search(d.get("version", r"(\d+(?:[._]\d+)+)"), name)
    if not m:
        return name
    return (m.group(1) if m.groups() else m.group(0)).replace("_", ".")


def resolve(src: Source) -> Release:
    d = src.discovery
    fn = {"static": _static, "redirect": _redirect, "json": _json, "html": _html}.get(d["type"])
    if not fn:
        raise DiscoveryError(f"{src.id}: unknown discovery type {d['type']}")
    try:
        return fn(d)
    except httpx2.HTTPError as e:
        raise DiscoveryError(f"{src.id}: {e}") from e
    except (jmespath.exceptions.JMESPathError, KeyError, IndexError, TypeError, ValueError) as e:
        raise DiscoveryError(f"{src.id}: unexpected response ({e})") from e


def _static(d: dict) -> Release:
    with _client(d) as c:
        r = c.head(d["url"])
        r.raise_for_status()
    date = _http_date(r.headers.get("last-modified", ""))
    # The real version may be read from the archive after download (version_from); the date is the fallback.
    return Release(version=date or "latest", url=str(r.url), date=date, size=_size(r.headers),
                   etag=r.headers.get("etag", "").strip('"'))


def _redirect(d: dict) -> Release:
    with _client(d) as c:
        r = c.head(d["url"])
        r.raise_for_status()
    final = str(r.url)
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(final).path))
    return Release(version=_version_from_name(d, name), url=final, date=_http_date(r.headers.get("last-modified", "")),
                   size=_size(r.headers))


def _vkey(v) -> list:
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[.\-_]", str(v))]


def _json(d: dict) -> Release:
    """JMESPath over {"root": <response>, "item": <selected element>}.

    items:     expression giving a list to pick from (optional)
    filter:    {field: <expr on item>, regex: <pattern>} keeps matching elements
    order_by:  expression on item; the highest version-sorted element wins (default: first)
    fields:    version / url (required), date, size, sha256 as expressions
    """
    with _client(d) as c:
        r = c.get(d["url"])
        r.raise_for_status()
        root = r.json()
    item = None
    if d.get("items"):
        items = jmespath.search(d["items"], {"root": root}) or []
        flt = d.get("filter")
        if flt:
            items = [i for i in items if re.search(flt["regex"], str(jmespath.search(flt["field"], i) or ""))]
        if not items:
            raise DiscoveryError("no element matched in the API response")
        item = max(items, key=lambda i: _vkey(jmespath.search(d["order_by"], i))) if d.get("order_by") else items[0]
    ctx = {"root": root, "item": item}
    f = d["fields"]

    def get(k):
        return jmespath.search(f[k], ctx) if f.get(k) else None

    version, url = get("version"), get("url")
    if not version or not url:
        raise DiscoveryError("version or url not found in the API response")
    sha = get("sha256")
    if sha:
        sha = str(sha).split(":", 1)[-1].lower()  # accept "sha256:<hex>"
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            sha = None
    size = get("size")
    version = _version_from_name(d, str(version)) if d.get("version") else str(version).lstrip("v")
    return Release(version=version, url=str(url), date=str(get("date") or "")[:10], size=int(size) if size else None, sha256=sha)


def _html(d: dict) -> Release:
    """links: pattern for candidate URLs in the page (default: absolute http(s) URLs);
    match: pattern the (URL-decoded) link must match; version: pattern applied to the file name."""
    with _client(d) as c:
        r = c.get(d["url"])
        r.raise_for_status()
        page = r.text
    links = sorted(set(re.findall(d.get("links", r"https?://[^\"'<>\s]+"), page)))
    hit = next((u for u in links if re.search(d["match"], urllib.parse.unquote(u))), None)
    if not hit:
        raise DiscoveryError(f"no link matching {d['match']!r} on {d['url']}")
    hit = urllib.parse.urljoin(d["url"], hit)
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(hit).path))
    return Release(version=_version_from_name(d, name), url=urllib.parse.quote(urllib.parse.unquote(hit), safe=":/?=&%"))
