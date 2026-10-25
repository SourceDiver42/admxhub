import zipfile

from admxhub.fetch import collect, extract, normalize_lang


def test_zip_extract_and_collect(tmp_path, zip_bytes):
    z = tmp_path / "t.zip"
    z.write_bytes(zip_bytes)
    extract(z, tmp_path / "x")
    files = collect(tmp_path / "x")
    assert [f.target for f in files] == ["test.admx", "de-DE/test.adml", "en-US/test.adml"]
    assert all(len(f.sha256) == 64 for f in files)


def test_zip_slip_is_ignored(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../escaped.txt", "x")
        zf.writestr("ok/a.admx", "<x/>")
    extract(z, tmp_path / "x")
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert (tmp_path / "x" / "ok" / "a.admx").exists()


def test_include_filter_and_lang_case(tmp_path):
    (tmp_path / "a" / "en-us").mkdir(parents=True)
    (tmp_path / "a" / "x.admx").write_text("<x/>")
    (tmp_path / "a" / "en-us" / "x.adml").write_text("<x/>")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.admx").write_text("<x/>")
    assert [f.target for f in collect(tmp_path, [r"^a/"])] == ["x.admx", "en-US/x.adml"]
    assert normalize_lang("sr-latn-rs") == "sr-Latn-RS"
