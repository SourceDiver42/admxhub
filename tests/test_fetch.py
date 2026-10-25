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


def test_extractor_that_produces_nothing_falls_back(tmp_path, monkeypatch):
    import admxhub.fetch as f
    calls = []

    def fake_run(cmd, timeout=600):
        calls.append(cmd[0])
        if cmd[0] == "second":  # the second tool really extracts something
            (tmp_path / "out" / "a.admx").parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / "out" / "a.admx").write_text("<x/>")
        return "ok"
    monkeypatch.setattr(f, "_run", fake_run)
    monkeypatch.setattr(f.shutil, "which", lambda c: c)
    f._first_that_works(tmp_path / "x.cab", tmp_path / "out", [["first"], ["second"]])
    assert calls == ["first", "second"]


def test_all_extractors_fail_with_details(tmp_path, monkeypatch):
    import pytest
    import admxhub.fetch as f
    monkeypatch.setattr(f, "_run", lambda cmd, timeout=600: "Can't open input file")
    monkeypatch.setattr(f.shutil, "which", lambda c: c if c != "missing" else None)
    with pytest.raises(f.FetchError, match="expand.exe: produced no files.*missing: not available"):
        f._first_that_works(tmp_path / "x.cab", tmp_path / "out", [["expand.exe"], ["missing"]])


def test_copy_of_the_archive_does_not_count(tmp_path, monkeypatch):
    import pytest
    import admxhub.fetch as f

    def copy_run(cmd, timeout=600):  # behaves like expand.exe on some cabinets
        (tmp_path / "out").mkdir(exist_ok=True)
        (tmp_path / "out" / "X.CAB").write_text("same thing")
        return "Expanding x.cab to x.cab"
    monkeypatch.setattr(f, "_run", copy_run)
    monkeypatch.setattr(f.shutil, "which", lambda c: c)
    with pytest.raises(f.FetchError, match="produced no files"):
        f._first_that_works(tmp_path / "x.cab", tmp_path / "out", [["expand.exe"]])
    assert not (tmp_path / "out" / "X.CAB").exists()
