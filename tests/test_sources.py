import pytest

from admxhub.sources import SourceError, load_all, validate

GOOD = {"id": "demo", "name": "Demo", "vendor": "V", "description": "d", "homepage": "https://x", "license": "MIT",
        "markers": ["demo.admx"], "discovery": {"type": "json", "url": "https://api.example/releases/latest",
                                         "items": "root.assets", "filter": {"field": "name", "regex": "^x\\.zip$"},
                                         "fields": {"version": "root.tag_name", "url": "item.browser_download_url"}}}


def test_bundled_sources_are_valid():
    srcs = load_all()
    assert len(srcs) >= 12
    assert {"edge", "chrome", "windows11", "firefox"} <= set(srcs)
    assert not any(s.mirror for s in srcs.values()), "no templates are hosted yet"


@pytest.mark.parametrize("change,msg", [
    ({"id": "Bad_ID"}, "lowercase"),
    ({"discovery": {"type": "ftp"}}, "unknown discovery"),
    ({"discovery": {"type": "html", "url": "https://x"}}, "needs match"),
    ({"discovery": {"type": "json", "url": "https://x", "fields": {"version": "a"}}}, "version and url"),
    ({"discovery": {"type": "json", "url": "https://x", "fields": {"version": "a[", "url": "b"}}}, "invalid JMESPath"),
    ({"discovery": {"type": "static", "url": "http://insecure"}}, "https"),
    ({"markers": []}, "markers"),
    ({"mirror": True, "license": "proprietary"}, "mirroring"),
    ({"discovery": {"type": "static", "url": "https://x", "match": "("}}, "invalid regex"),
])
def test_invalid_sources(change, msg):
    with pytest.raises(SourceError, match=msg):
        validate({**GOOD, **change})


def test_file_name_must_match_id():
    with pytest.raises(SourceError, match="file name"):
        validate(GOOD, "other.yaml")
