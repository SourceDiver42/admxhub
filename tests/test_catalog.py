import json

import admxhub.catalog as catalog
import admxhub.fetch as fetchmod
from admxhub.discovery import Release
from admxhub.sources import validate

from conftest import make_zip

SRC = validate({"id": "demo", "name": "Demo", "vendor": "V", "description": "d", "homepage": "https://x", "license": "MIT",
                "markers": ["test.admx"], "discovery": {"type": "static", "url": "https://example.invalid/t.zip"}})


def run_build(tmp_path, monkeypatch, version, data, etag):
    monkeypatch.setattr(catalog, "resolve", lambda s: Release(version=version, url="https://example.invalid/t.zip", etag=etag))

    def fake_download(release, dest, progress=None):
        p = dest / "t.zip"
        p.write_bytes(data)
        import hashlib
        return p, hashlib.sha256(data).hexdigest(), len(data)
    monkeypatch.setattr(fetchmod, "download", fake_download)
    return catalog.build({"demo": SRC}, tmp_path, log=lambda m: None)


def test_build_history_and_diff(tmp_path, monkeypatch):
    idx = run_build(tmp_path, monkeypatch, "1.0", make_zip(("Alpha", "Beta")), "e1")
    p = idx["products"][0]
    assert p["latest"]["version"] == "1.0" and p["latest"]["policyCount"] == 2
    assert p["latest"]["languages"] == ["de-DE", "en-US"]
    # unchanged fingerprint → no new version
    idx = run_build(tmp_path, monkeypatch, "1.0", make_zip(("Alpha", "Beta")), "e1")
    assert idx["products"][0]["versionCount"] == 1
    # new release: one policy removed, one added
    idx = run_build(tmp_path, monkeypatch, "2.0", make_zip(("Alpha", "Gamma")), "e2")
    latest = idx["products"][0]["latest"]
    assert latest["version"] == "2.0"
    assert latest["diff"] == {"previous": "1.0", "added": 1, "removed": 1, "changed": 0,
                              "details": {"added": ["Test.Policies:Gamma"], "removed": ["Test.Policies:Beta"], "changed": []}}
    hist = json.loads((tmp_path / "catalog/v1/products/demo.json").read_text())
    assert [v["version"] for v in hist["versions"]] == ["2.0", "1.0"]
    pol = json.loads((tmp_path / "catalog/v1/policies/demo/2.0.json").read_text())
    assert {p["display"] for p in pol} == {"Alpha policy", "Gamma policy"}
    assert "<title>Demo 2.0</title>" in (tmp_path / "catalog/v1/feed.xml").read_text()
    assert not (tmp_path / "mirror").exists()


def test_failure_is_recorded(tmp_path, monkeypatch):
    from admxhub.discovery import DiscoveryError

    def boom(s):
        raise DiscoveryError("vendor changed their site")
    monkeypatch.setattr(catalog, "resolve", boom)
    idx = catalog.build({"demo": SRC}, tmp_path, log=lambda m: None)
    idx = catalog.build({"demo": SRC}, tmp_path, log=lambda m: None)
    st = idx["products"][0]["status"]
    assert st["consecutiveFailures"] == 2 and "changed their site" in st["error"]


def test_backfill_inserts_history_and_recomputes_diffs(tmp_path, monkeypatch):
    import admxhub.discovery as disc
    hist_src = validate({**{k: getattr(SRC, k) for k in ("id", "name", "vendor", "description", "homepage", "license", "markers")},
                         "discovery": SRC.discovery,
                         "history": {"url": "https://example.invalid/releases", "releases": "root",
                                     "fields": {"version": "release.v", "url": "release.u"}}})
    zips = {"1.0": make_zip(("Alpha",)), "2.0": make_zip(("Alpha", "Beta")), "3.0": make_zip(("Alpha", "Beta", "Gamma"))}
    current = {"v": "3.0"}

    def fake_download(release, dest, progress=None):
        import hashlib
        data = zips[release.version]
        p = dest / "t.zip"
        p.write_bytes(data)
        return p, hashlib.sha256(data).hexdigest(), len(data)
    monkeypatch.setattr(fetchmod, "download", fake_download)
    monkeypatch.setattr(catalog, "resolve", lambda s: Release(version=current["v"], url="https://x/3", etag="e3"))
    catalog.build({"demo": hist_src}, tmp_path, log=lambda m: None)  # first seen: 3.0 only
    monkeypatch.setattr(disc, "history", lambda s, limit=20: [Release(version="2.0", url="https://x/2", date="2026-01-02"),
                                                              Release(version="1.0", url="https://x/1", date="2026-01-01")])
    idx = catalog.build({"demo": hist_src}, tmp_path, log=lambda m: None, backfill=5)
    hist = json.loads((tmp_path / "catalog/v1/products/demo.json").read_text())["versions"]
    assert [v["version"] for v in hist] == ["3.0", "2.0", "1.0"]
    assert hist[0]["diff"]["previous"] == "2.0" and hist[0]["diff"]["details"]["added"] == ["Test.Policies:Gamma"]
    assert hist[1]["diff"]["previous"] == "1.0" and hist[1]["backfilled"]
    assert hist[2]["diff"]["previous"] is None
    assert idx["products"][0]["latest"]["version"] == "3.0"
    feed = (tmp_path / "catalog/v1/feed.xml").read_text()
    assert "Demo 3.0" in feed and "Demo 2.0" not in feed
