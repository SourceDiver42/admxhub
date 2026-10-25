"""Live checks against the vendors; run with: pytest -m network"""
import pytest

from admxhub.discovery import resolve
from admxhub.sources import load_all

SOURCES = load_all()


@pytest.mark.network
@pytest.mark.parametrize("sid", sorted(SOURCES))
def test_source_resolves(sid):
    rel = resolve(SOURCES[sid])
    assert rel.version and rel.url.startswith("https://")
