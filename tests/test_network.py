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


@pytest.mark.network
def test_msi_extraction_without_external_tools(tmp_path):
    """The Windows templates MSI must unpack through the built-in MSI reader."""
    from admxhub.fetch import fetch
    bundle = fetch(resolve(SOURCES["windows11"]), SOURCES["windows11"].archive, workdir=tmp_path)
    names = {f.name.lower() for f in bundle.files if f.kind == "admx"}
    assert "windows.admx" in names and len(names) > 150
    assert "en-US" in bundle.languages
