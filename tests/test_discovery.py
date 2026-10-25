import httpx2
import pytest

import admxhub.discovery as disc
from admxhub.sources import validate

BASE = {"name": "Demo", "vendor": "V", "description": "d", "homepage": "https://x", "license": "MIT", "markers": ["d.admx"]}


def src(discovery, sid="demo"):
    return validate({**BASE, "id": sid, "discovery": discovery})


@pytest.fixture
def serve(monkeypatch):
    seen = {}

    def install(handler):
        def client(d):
            seen["headers"] = disc._headers(d)
            return httpx2.Client(transport=httpx2.MockTransport(handler), headers=seen["headers"], follow_redirects=True)
        monkeypatch.setattr(disc, "_client", client)
        return seen
    return install


def test_json_github_style_release(serve):
    serve(lambda req: httpx2.Response(200, json={
        "tag_name": "v2.1", "published_at": "2026-10-01T10:00:00Z",
        "assets": [{"name": "other.zip", "browser_download_url": "https://dl/other.zip"},
                   {"name": "templates_v2.1.zip", "browser_download_url": "https://dl/t.zip", "size": 10,
                    "digest": "sha256:" + "a" * 64}]}))
    r = disc.resolve(src({"type": "json", "url": "https://api.example/latest", "items": "root.assets",
                          "filter": {"field": "name", "regex": r"^templates_v[\d.]+\.zip$"},
                          "fields": {"version": "root.tag_name", "url": "item.browser_download_url", "date": "root.published_at",
                                     "size": "item.size", "sha256": "item.digest"}}))
    assert (r.version, r.url, r.date, r.size, r.sha256) == ("2.1", "https://dl/t.zip", "2026-10-01", 10, "a" * 64)


def test_json_order_by_version(serve):
    serve(lambda req: httpx2.Response(200, json=[{"Product": "Other"}, {"Product": "Policy", "Releases": [
        {"ProductVersion": "9.0.1", "Artifacts": [{"Location": "https://dl/9"}]},
        {"ProductVersion": "10.0.0", "Artifacts": [{"Location": "https://dl/10"}]}]}]))
    r = disc.resolve(src({"type": "json", "url": "https://api.example/", "items": "root[?Product=='Policy'] | [0].Releases",
                          "order_by": "ProductVersion", "fields": {"version": "item.ProductVersion", "url": "item.Artifacts[0].Location"}}))
    assert (r.version, r.url) == ("10.0.0", "https://dl/10")


def test_html_links_with_spaces(serve):
    page = '<a href="x">x</a> {"url":"https://dl.example/files/Templates for Product 7 May 2026.msi"}'
    serve(lambda req: httpx2.Response(200, text=page))
    r = disc.resolve(src({"type": "html", "url": "https://vendor.example/page", "links": r"https://dl\.example/[^\"'<>]+",
                          "match": r"Templates for Product \d+ .*\.msi$", "version": r"Product \d+ (.+?)\.msi$"}))
    assert r.version == "May 2026" and r.url.endswith("Templates%20for%20Product%207%20May%202026.msi")


def test_optional_headers(monkeypatch, serve):
    d = {"type": "json", "url": "https://api.example/", "headers": {"Authorization": "Bearer ${DEMO_TOKEN}"},
         "fields": {"version": "root.v", "url": "root.u"}}
    seen = serve(lambda req: httpx2.Response(200, json={"v": "1", "u": "https://dl/1"}))
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    disc.resolve(src(d))
    assert "Authorization" not in seen["headers"]
    monkeypatch.setenv("DEMO_TOKEN", "abc")
    disc.resolve(src(d))
    assert seen["headers"]["Authorization"] == "Bearer abc"


def test_missing_fields_is_a_clear_error(serve):
    serve(lambda req: httpx2.Response(200, json={}))
    with pytest.raises(disc.DiscoveryError, match="version or url"):
        disc.resolve(src({"type": "json", "url": "https://api.example/", "fields": {"version": "root.v", "url": "root.u"}}))
