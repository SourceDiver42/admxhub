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


def build_one(src: Source, out: Path, force=False, mirror=False, log=print) -> dict:
    base = out / "catalog" / "v1"
    prod_path = base / "products" / f"{src.id}.json"
    prod = _read(prod_path, {"versions": [], "status": {}})
    status = prod.get("status") or {}
    status["lastChecked"] = now()
    versions = prod.get("versions") or []
    latest = versions[0] if versions else None
    try:
        rel = resolve(src)
        if latest and not force and fingerprint(rel) == latest.get("fingerprint"):
            log(f"  {src.id}: unchanged ({latest['version']})")
        else:
            log(f"  {src.id}: fetching {rel.version} from {rel.url}")
            bundle = fetch(rel, src.archive)
            try:
                fp = fingerprint(rel)
                apply_version_from(src, bundle, rel)
                policies, errors = policy_index(bundle)
                namespaces = sorted({ns for f in bundle.files if f.kind == "admx" and (ns := namespace_of(f.path.read_bytes()))})
                prev_pol = _read(base / "policies" / src.id / f"{safe_version(latest['version'])}.json", []) if latest else []
                d = diff_policies(prev_pol, policies) if latest else {"added": [], "removed": [], "changed": []}
                entry = {
                    "version": rel.version, "date": rel.date, "discovered": now(), "url": rel.url, "sha256": bundle.sha256,
                    "size": bundle.size, "fingerprint": fp, "languages": bundle.languages,
                    "files": [f.to_json() for f in bundle.files], "admxCount": sum(f.kind == "admx" for f in bundle.files),
                    "policyCount": len(policies), "namespaces": namespaces, "parseErrors": errors[:20],
                    "diff": {"previous": latest["version"] if latest else None,
                             **{k: len(v) for k, v in d.items()}, "details": {k: v[:200] for k, v in d.items()}},
                }
                if latest and latest["version"] == rel.version and latest.get("sha256") == bundle.sha256:
                    latest["fingerprint"] = fp  # same content, new headers
                    log(f"  {src.id}: same content as {rel.version}")
                else:
                    _write(base / "policies" / src.id / f"{safe_version(rel.version)}.json", policies)
                    versions = [entry] + [v for v in versions if v["version"] != rel.version]
                    if mirror and src.mirror:
                        dest = out / "mirror" / src.id / safe_version(rel.version)
                        for f in bundle.files:
                            (dest / f.target).parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(f.path, dest / f.target)
                    log(f"  {src.id}: new version {rel.version} ({len(policies)} policies, +{len(d['added'])} -{len(d['removed'])})")
            finally:
                bundle.cleanup()
        status.update(lastSuccess=now(), error=None, consecutiveFailures=0)
    except (DiscoveryError, FetchError, OSError, ValueError) as e:
        status.update(error=str(e)[:500], consecutiveFailures=int(status.get("consecutiveFailures") or 0) + 1)
        log(f"  {src.id}: FAILED: {e}")
    prod = {**src.to_json(), "status": status, "versions": versions[:MAX_VERSIONS]}
    _write(prod_path, prod)
    return prod


def build(sources: dict[str, Source], out: Path, only: list[str] | None = None, force=False, mirror=False, log=print) -> dict:
    out = Path(out)
    base = out / "catalog" / "v1"
    ids = only or sorted(sources)
    for i in ids:
        if i not in sources:
            raise ValueError(f"unknown source {i}")
    log(f"Building catalog for {len(ids)} products into {base}")
    for i in ids:
        build_one(sources[i], out, force=force, mirror=mirror, log=log)
    # index over all products (including ones not rebuilt this run)
    products, ns_owner, warnings = [], {}, []
    for i in sorted(sources):
        p = _read(base / "products" / f"{i}.json", None)
        if not p:
            continue
        latest = p["versions"][0] if p["versions"] else None
        for ns in (latest or {}).get("namespaces", []):
            if ns in ns_owner and ns_owner[ns] != i:
                warnings.append(f"namespace {ns} is defined by both {ns_owner[ns]} and {i}")
            ns_owner[ns] = i
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
