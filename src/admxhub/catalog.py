"""Build the public catalog (catalog/v1/…) from the source definitions.

Layout of the output directory (published on GitHub Pages, history kept on the `data` branch):

    catalog/v1/index.json                    all products, latest version, health
    catalog/v1/products/<id>.json            full version history of one product
    catalog/v1/policies/<id>/<version>.json  policy index of one version (names, registry keys, categories)
    catalog/v1/feed.xml                      Atom feed of newly discovered versions
    mirror/<id>/<version>/…                  template files, only for sources with mirror: true
                                             (disabled for now: no ADMX files are hosted)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__
import httpx2

from .admx import AdmxSet
from .discovery import DiscoveryError, Release, resolve
from .fetch import Bundle, FetchError, fetch
from .sources import Source

MAX_VERSIONS = 50
NS_RE = re.compile(rb'<target\s+[^>]*namespace="([^"]+)"', re.I)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def safe_version(v: str) -> str:
    return re.sub(r"[^\w.+-]", "_", v)[:80]


def namespace_of(data: bytes) -> str | None:
    m = NS_RE.search(data[:8192])
    if m:
        return m.group(1).decode("utf-8", "replace")
    try:
        m = re.search(r'<target\s+[^>]*namespace="([^"]+)"', data[:16384].decode("utf-16"))
        return m.group(1) if m else None
    except UnicodeDecodeError:
        return None


def apply_version_from(src: Source, bundle: Bundle, release: Release) -> None:
    vf = src.version_from
    if not vf or not bundle.workdir:
        return
    for p in bundle.workdir.rglob(vf["file"]):
        m = re.search(vf["pattern"], p.read_text(encoding="utf-8", errors="replace"))
        if m:
            release.version = vf.get("join", ".").join(m.groups()) if m.groups() else m.group(0)
            return


def policy_index(bundle: Bundle, lang: str = "en-US") -> tuple[list[dict], list[str]]:
    admx = {f.name: f.path.read_bytes() for f in bundle.files if f.kind == "admx"}
    by_lang: dict[str, dict[str, bytes]] = {}
    for f in bundle.files:
        if f.kind == "adml":
            by_lang.setdefault(f.lang.lower(), {})[f.name] = f.path.read_bytes()
    adml = by_lang.get(lang.lower()) or next(iter(by_lang.values()), {})
    s = AdmxSet.load(admx, adml)
    out = []
    for p in sorted(s.policies.values(), key=lambda p: p.id):
        out.append({
            "id": p.id, "name": p.name, "display": p.display, "class": p.cls, "file": p.file,
            "category": s.category_path(p.category), "key": p.key, "valueName": p.value_name,
            "supported": p.supported, "elements": [e.type for e in p.elements],
        })
    return out, s.errors


def diff_policies(old: list[dict], new: list[dict]) -> dict:
    o = {p["id"]: p for p in old}
    n = {p["id"]: p for p in new}
    changed = [i for i in n if i in o and (o[i]["key"], o[i]["valueName"], o[i]["elements"]) != (n[i]["key"], n[i]["valueName"], n[i]["elements"])]
    return {"added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)), "changed": sorted(changed)}


def fingerprint(r: Release) -> str:
    return r.sha256 or r.etag or f"{r.url}|{r.date}|{r.size or ''}"


def index_release(src: Source, rel: Release, out: Path, mirror=False, discovered: str | None = None) -> dict:
    """Download one release, write its policy index and return its version entry (diff filled in later)."""
    base = out / "catalog" / "v1"
    fp = fingerprint(rel)
    bundle = fetch(rel, src.archive)
    try:
        apply_version_from(src, bundle, rel)
        policies, errors = policy_index(bundle)
        ns_files = {ns: f.name for f in bundle.files if f.kind == "admx" and (ns := namespace_of(f.path.read_bytes()))}
        _write(base / "policies" / src.id / f"{safe_version(rel.version)}.json", policies)
        if mirror and src.mirror:
            dest = out / "mirror" / src.id / safe_version(rel.version)
            for f in bundle.files:
                (dest / f.target).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f.path, dest / f.target)
        return {
            "version": rel.version, "date": rel.date, "discovered": discovered or now(), "url": rel.url,
            "sha256": bundle.sha256, "size": bundle.size, "fingerprint": fp, "languages": bundle.languages,
            "files": [f.to_json() for f in bundle.files], "admxCount": sum(f.kind == "admx" for f in bundle.files),
            "policyCount": len(policies), "namespaces": sorted(ns_files), "namespaceFiles": ns_files, "parseErrors": errors[:20],
        }
    finally:
        bundle.cleanup()


NUMERIC_VERSION = re.compile(r"^\d+(\.\d+)+$")


def _vkey(version: str) -> list:
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[.\-_ ]", version)]


def order_versions(versions: list[dict]) -> list[dict]:
    """Newest first: by version number when all versions are dotted numbers (release dates can
    lie, e.g. a patch for an older branch published later), otherwise by release/discovery date."""
    if versions and all(NUMERIC_VERSION.match(v["version"]) for v in versions):
        return sorted(versions, key=lambda v: _vkey(v["version"]), reverse=True)
    return sorted(versions, key=lambda v: ((v.get("date") or v.get("discovered") or "")[:10], _vkey(v["version"])), reverse=True)


def recompute_diffs(base: Path, sid: str, versions: list[dict]) -> list[dict]:
    """Sort newest first and diff every version against the next older one."""
    versions = order_versions(versions)
    cache: dict[str, list] = {}

    def pols(v):
        if v["version"] not in cache:
            cache[v["version"]] = _read(base / "policies" / sid / f"{safe_version(v['version'])}.json", [])
        return cache[v["version"]]
    for i, v in enumerate(versions):
        older = versions[i + 1] if i + 1 < len(versions) else None
        if older is None:
            v["diff"] = {"previous": None, "added": 0, "removed": 0, "changed": 0, "details": {"added": [], "removed": [], "changed": []}}
            continue
        d = diff_policies(pols(older), pols(v))
        v["diff"] = {"previous": older["version"], **{k: len(x) for k, x in d.items()}, "details": {k: x[:200] for k, x in d.items()}}
    return versions


def _save_product(src: Source, out: Path, status: dict, versions: list[dict]) -> dict:
    base = out / "catalog" / "v1"
    versions = recompute_diffs(base, src.id, versions)[:MAX_VERSIONS]
    prod = {**src.to_json(), "status": status, "versions": versions}
    _write(base / "products" / f"{src.id}.json", prod)
    return prod


def build_one(src: Source, out: Path, force=False, mirror=False, log=print) -> dict:
    base = out / "catalog" / "v1"
    prod = _read(base / "products" / f"{src.id}.json", {"versions": [], "status": {}})
    status = prod.get("status") or {}
    status["lastChecked"] = now()
    versions = prod.get("versions") or []
    latest = versions[0] if versions else None
    try:
        rel = resolve(src)
        known = {v.get("fingerprint") for v in versions}
        if not force and fingerprint(rel) in known:
            log(f"  {src.id}: unchanged ({latest['version'] if latest else rel.version})")
        else:
            log(f"  {src.id}: fetching {rel.version} from {rel.url}")
            entry = index_release(src, rel, out, mirror)
            same = next((v for v in versions if v["version"] == entry["version"] and v.get("sha256") == entry["sha256"]), None)
            if same:
                same["fingerprint"] = entry["fingerprint"]  # same content, new headers
                log(f"  {src.id}: same content as {entry['version']}")
            else:
                versions = [entry] + [v for v in versions if v["version"] != entry["version"]]
                log(f"  {src.id}: new version {entry['version']} ({entry['policyCount']} policies)")
        status.update(lastSuccess=now(), error=None, consecutiveFailures=0)
    except (DiscoveryError, FetchError, OSError, ValueError) as e:
        status.update(error=str(e)[:500], consecutiveFailures=int(status.get("consecutiveFailures") or 0) + 1)
        log(f"  {src.id}: FAILED: {e}")
    return _save_product(src, out, status, versions)


def backfill_one(src: Source, out: Path, limit: int = 20, mirror=False, log=print) -> dict:
    """Add past releases (from the source's `history` block) to the product's version history."""
    from .discovery import history
    base = out / "catalog" / "v1"
    prod = _read(base / "products" / f"{src.id}.json", {"versions": [], "status": {}})
    versions = prod.get("versions") or []
    if not src.history:
        log(f"  {src.id}: no history source defined; history builds up from now on")
        return _save_product(src, out, prod.get("status") or {}, versions)
    have = {v["version"] for v in versions}
    releases = history(src, limit)
    todo = [r for r in releases if r.version not in have]
    log(f"  {src.id}: {len(releases)} past releases listed, {len(todo)} new")
    status = prod.get("status") or {}
    saved = None
    for rel in todo:
        try:
            entry = index_release(src, rel, out, mirror, discovered=(rel.date + "T00:00:00Z") if rel.date else None)
        except (FetchError, OSError, ValueError, httpx2.HTTPError) as e:
            log(f"    {rel.version}: skipped ({e})")  # retried on the next run
            continue
        entry["backfilled"] = True
        versions = [v for v in versions if v["version"] != entry["version"]] + [entry]
        saved = _save_product(src, out, status, versions)  # keep progress if the run is interrupted
        log(f"    {rel.version}: {entry['policyCount']} policies")
    return saved or _save_product(src, out, status, versions)


def build(sources: dict[str, Source], out: Path, only: list[str] | None = None, force=False, mirror=False, log=print,
          backfill: int = 0) -> dict:
    out = Path(out)
    base = out / "catalog" / "v1"
    ids = only or sorted(sources)
    for i in ids:
        if i not in sources:
            raise ValueError(f"unknown source {i}")
    log(f"Building catalog for {len(ids)} products into {base}")
    for i in ids:
        build_one(sources[i], out, force=force, mirror=mirror, log=log)
        if backfill:
            backfill_one(sources[i], out, limit=backfill, mirror=mirror, log=log)
    # index over all products (including ones not rebuilt this run)
    products, ns_owner, warnings = [], {}, []
    for i in sorted(sources):
        p = _read(base / "products" / f"{i}.json", None)
        if not p:
            continue
        latest = p["versions"][0] if p["versions"] else None
        for ns, fname in ((latest or {}).get("namespaceFiles") or {}).items():
            # the same file shipped by two products (e.g. a shared parent category) is fine;
            # the same namespace in differently named files breaks the Group Policy editor
            if ns in ns_owner and ns_owner[ns][1].lower() != fname.lower():
                warnings.append(f"namespace {ns} is defined by {ns_owner[ns][0]} ({ns_owner[ns][1]}) and {i} ({fname})")
            ns_owner.setdefault(ns, (i, fname))
        products.append({**{k: v for k, v in p.items() if k != "versions"},
                         "latest": {k: v for k, v in latest.items() if k not in ("fingerprint",)} if latest else None,
                         "versionCount": len(p["versions"])})
    index = {"schema": "admxhub/catalog/v1", "generated": now(), "generator": f"admxhub {__version__}",
             "products": products, "warnings": warnings}
    _write(base / "index.json", index)
    write_feed(base, products, sources)
    return index


def write_feed(base: Path, products: list[dict], sources: dict[str, Source]) -> None:
    entries = []
    for p in products:
        hist = _read(base / "products" / f"{p['id']}.json", {"versions": []})["versions"]
        for v in hist[:10]:
            if not v.get("backfilled"):  # historic versions are not news
                entries.append((v.get("discovered") or now(), p, v))
    entries.sort(key=lambda e: e[0], reverse=True)
    items = []
    for when, p, v in entries[:100]:
        d = v.get("diff") or {}
        summary = f"{p['name']} {v['version']}: {v.get('policyCount', 0)} policies"
        if d.get("previous"):
            summary += f" (+{d.get('added', 0)} / -{d.get('removed', 0)} / ~{d.get('changed', 0)} since {d['previous']})"
        items.append(f"""  <entry>
    <id>urn:admxhub:{escape(p['id'])}:{escape(v['version'])}</id>
    <title>{escape(p['name'])} {escape(v['version'])}</title>
    <updated>{escape(when)}</updated>
    <link href="{escape(v['url'])}"/>
    <summary>{escape(summary)}</summary>
  </entry>""")
    feed = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:admxhub:feed</id>
  <title>ADMX Hub: new Group Policy templates</title>
  <updated>{now()}</updated>
  <generator>admxhub {__version__}</generator>
{chr(10).join(items)}
</feed>
"""
    (base / "feed.xml").write_text(feed, encoding="utf-8")
