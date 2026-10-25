from admxhub.fetch import collect, extract
from admxhub.store import Store, apply, plan

from conftest import make_zip


def bundle_from(tmp_path, data, name="b"):
    z = tmp_path / f"{name}.zip"
    z.write_bytes(data)
    extract(z, tmp_path / name)

    class B:
        files = collect(tmp_path / name)
    return B()


def test_plan_apply_and_identical(tmp_path, zip_bytes):
    store = Store(tmp_path / "store")
    b = bundle_from(tmp_path, zip_bytes)
    items = plan(b, store, None)  # empty store → en-US only
    assert sorted(i.target for i in items) == ["en-US/test.adml", "test.admx"]
    assert {i.status for i in items} == {"new"}
    assert apply(items, store) == 2
    again = plan(b, store, None)
    assert {i.status for i in again} == {"identical"}
    assert apply(again, store) == 0


def test_existing_language_folders_are_reused(tmp_path, zip_bytes):
    (tmp_path / "store" / "de-de").mkdir(parents=True)
    items = plan(bundle_from(tmp_path, zip_bytes), Store(tmp_path / "store"), None)
    assert sorted(i.target for i in items) == ["de-de/test.adml", "test.admx"]


def test_namespace_conflict_warning(tmp_path):
    store = Store(tmp_path / "store")
    apply(plan(bundle_from(tmp_path, make_zip(ns="Same.NS"), "one"), store, None), store)
    (store.path / "test.admx").rename(store.path / "old-name.admx")
    items = plan(bundle_from(tmp_path, make_zip(ns="Same.NS"), "two"), store, None)
    assert any(i.warning and "old-name.admx" in i.warning for i in items)
