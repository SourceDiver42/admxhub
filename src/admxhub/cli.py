"""admxhub command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx2

from . import __version__
from .catalog import build, fingerprint, policy_index
from .discovery import DiscoveryError, Release, resolve
from .fetch import FetchError, fetch
from .sources import SourceError, load_all
from .store import Store, apply, default_store, plan, save_state

DEFAULT_CATALOG = os.environ.get("ADMXHUB_CATALOG", "")  # set once the GitHub Pages catalog is live


class CliError(Exception):
    pass


# -- output helpers ---------------------------------------------------------------

def table(rows: list[list], headers: list[str]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows])


def emit(args, data, text: str) -> None:
    if args.json:
        print(json.dumps(data, indent=1, ensure_ascii=False))
    else:
        print(text)


def info(args, msg: str) -> None:
    if not args.json and not args.quiet:
        print(msg, file=sys.stderr)


# -- catalog / live resolution ----------------------------------------------------------

def load_catalog(args) -> dict | None:
    url = args.catalog or DEFAULT_CATALOG
    if not url:
        return None
    if os.path.exists(url):
        return json.loads(Path(url).read_text(encoding="utf-8"))
    r = httpx2.get(url, follow_redirects=True, timeout=30)
    r.raise_for_status()
    return r.json()


def latest_release(args, src, catalog) -> Release:
    """Latest version: from the published catalog if configured (includes checksums), else live from the vendor."""
    if catalog:
        p = next((x for x in catalog["products"] if x["id"] == src.id), None)
        if p and p.get("latest"):
            l = p["latest"]
            return Release(version=l["version"], url=l["url"], date=l.get("date", ""), size=l.get("size"), sha256=l.get("sha256"))
    return resolve(src)


def get_sources(args, ids: list[str] | None = None):
    srcs = load_all(args.sources)
    if ids:
        unknown = [i for i in ids if i not in srcs]
        if unknown:
            raise CliError(f"unknown product(s): {', '.join(unknown)}. Run 'admxhub sources' for the list.")
        return {i: srcs[i] for i in ids}
    return srcs


def get_store(args) -> Store:
    path = Path(args.store) if args.store else default_store()
    if not path:
        raise CliError("no store given: use --store PATH (e.g. \\\\domain\\SYSVOL\\domain\\Policies\\PolicyDefinitions)")
    return Store(path)


def _progress(args, label):
    if args.json or args.quiet or not sys.stderr.isatty():
        return None
    def cb(n):
        print(f"\r  {label}: {n / 1048576:.1f} MB", end="", file=sys.stderr, flush=True)
    return cb


# -- commands -------------------------------------------------------------------------

def cmd_sources(args):
    srcs = get_sources(args)
    rows = [[s.id, s.name, s.vendor, s.discovery["type"], s.license] for s in srcs.values()]
    emit(args, [s.to_json() for s in srcs.values()], table(rows, ["ID", "Product", "Vendor", "Discovery", "License"]))


def cmd_check(args):
    srcs = get_sources(args, args.ids)
    catalog = None if args.live else load_catalog(args)
    out, rows = [], []
    for s in srcs.values():
        try:
            r = latest_release(args, s, catalog)
            out.append({"id": s.id, **r.to_json()})
            rows.append([s.id, r.version, r.date, r.url])
        except (DiscoveryError, httpx2.HTTPError) as e:
            out.append({"id": s.id, "error": str(e)})
            rows.append([s.id, "ERROR", "", str(e)[:80]])
    emit(args, out, table(rows, ["ID", "Latest", "Date", "Download"]))
    if any("error" in o for o in out):
        return 2


def cmd_list(args):
    catalog = load_catalog(args)
    if not catalog:
        info(args, "No catalog configured (--catalog or ADMXHUB_CATALOG); checking vendors directly.")
        args.ids, args.live = None, True
        return cmd_check(args)
    rows = [[p["id"], p["name"], (p.get("latest") or {}).get("version"), (p.get("latest") or {}).get("policyCount"),
             (p.get("status") or {}).get("error") and "check failing" or "ok"] for p in catalog["products"]]
    emit(args, catalog, table(rows, ["ID", "Product", "Latest", "Policies", "Health"]))


def is_current(installed: dict, rel: Release) -> bool:
    """Installed version matches the latest (by version, or by download fingerprint for
    products whose real version is only known after downloading, e.g. Chrome)."""
    return installed.get("version") == rel.version or bool(installed.get("fingerprint")) and installed["fingerprint"] == fingerprint(rel)


def _fetch(args, src, catalog):
    rel = latest_release(args, src, catalog)
    info(args, f"{src.name} {rel.version}: downloading {rel.url}")
    fp = fingerprint(rel)
    bundle = fetch(rel, src.archive, progress=_progress(args, src.id))
    rel.fingerprint = fp
    if not args.json and not args.quiet and sys.stderr.isatty():
        print(file=sys.stderr)
    from .catalog import apply_version_from
    apply_version_from(src, bundle, rel)
    return rel, bundle


def cmd_download(args):
    srcs = get_sources(args, args.ids)
    catalog = load_catalog(args)
    results = []
    for src in srcs.values():
        rel, bundle = _fetch(args, src, catalog)
        try:
            dest = Path(args.out) / src.id / rel.version / "PolicyDefinitions"
            langs = {l.lower() for l in args.lang} if args.lang else None
            n = 0
            for f in bundle.files:
                if f.kind == "adml" and langs and f.lang.lower() not in langs:
                    continue
                (dest / f.target).parent.mkdir(parents=True, exist_ok=True)
                (dest / f.target).write_bytes(f.path.read_bytes())
                n += 1
            results.append({"id": src.id, "version": rel.version, "sha256": bundle.sha256, "path": str(dest), "files": n,
                            "languages": bundle.languages})
            info(args, f"  {n} files -> {dest}")
        finally:
            bundle.cleanup()
    emit(args, results, table([[r["id"], r["version"], r["files"], r["path"]] for r in results], ["ID", "Version", "Files", "Path"]))


def _install(args, srcs, store, catalog) -> list[dict]:
    results = []
    for src in srcs.values():
        rel, bundle = _fetch(args, src, catalog)
        try:
            langs = ["*"] if args.all_languages else (args.lang or None)
            items = plan(bundle, store, langs)
            for it in items:
                if it.warning:
                    info(args, f"  warning: {it.target}: {it.warning}")
            changes = [it for it in items if it.status != "identical"]
            info(args, f"  {len(changes)} of {len(items)} files to write ({sum(i.status == 'new' for i in items)} new, "
                       f"{sum(i.status == 'update' for i in items)} updated)")
            written = 0
            if not args.dry_run and changes:
                if not args.yes and sys.stdin.isatty():
                    if input(f"  Write to {store.path}? [y/N] ").strip().lower() not in ("y", "yes"):
                        info(args, "  skipped")
                        results.append({"id": src.id, "version": rel.version, "skipped": True})
                        continue
                written = apply(items, store)
            if not args.dry_run:
                save_state(store.path, src.id, rel.version, [i.target for i in items if i.target.lower().endswith(".admx")],
                           rel.fingerprint)
            results.append({"id": src.id, "version": rel.version, "written": written, "dryRun": args.dry_run,
                            "plan": [i.to_json() for i in items]})
        finally:
            bundle.cleanup()
    return results


def cmd_install(args):
    store = get_store(args)
    srcs = get_sources(args, args.ids)
    res = _install(args, srcs, store, load_catalog(args))
    emit(args, res, table([[r["id"], r["version"], "skipped" if r.get("skipped") else ("dry run" if r.get("dryRun") else r["written"])]
                           for r in res], ["ID", "Version", "Files written"]))


def cmd_status(args):
    store = get_store(args)
    srcs = get_sources(args)
    catalog = None if args.live else load_catalog(args)
    inst = store.installed(srcs)
    rows, out = [], []
    for sid, src in srcs.items():
        if sid not in inst and not args.all:
            continue
        cur = (inst.get(sid) or {}).get("version")
        rel = None
        try:
            rel = latest_release(args, src, catalog)
            current = sid in inst and is_current(inst[sid], rel)
            latest = cur if current else rel.version
        except (DiscoveryError, httpx2.HTTPError) as e:
            latest, current = f"error: {e}"[:60], False
        state = "not installed" if sid not in inst else ("unknown version" if not cur else "up to date" if current
                                                         else "update available" if rel else "check failed")
        rows.append([sid, cur or ("-" if sid not in inst else "?"), latest, state])
        out.append({"id": sid, "installed": sid in inst, "version": cur, "latest": latest, "state": state})
    info(args, f"Store: {store.path}" + ("" if store.exists() else " (does not exist yet)"))
    emit(args, out, table(rows, ["ID", "Installed", "Latest", "State"]) if rows else "No known products installed in this store.")


def cmd_update(args):
    store = get_store(args)
    srcs = get_sources(args)
    catalog = load_catalog(args)
    inst = store.installed(srcs)
    todo = {}
    for sid in inst:
        if not is_current(inst[sid], latest_release(args, srcs[sid], catalog)):
            todo[sid] = srcs[sid]
    if not todo:
        emit(args, [], "Everything is up to date.")
        return
    res = _install(args, todo, store, catalog)
    emit(args, res, table([[r["id"], r["version"], "dry run" if r.get("dryRun") else r.get("written", "skipped")] for r in res],
                          ["ID", "Version", "Files written"]))


def cmd_policies(args):
    src = get_sources(args, [args.id])[args.id]
    rel, bundle = _fetch(args, src, load_catalog(args))
    try:
        pols, errors = policy_index(bundle, args.lang or "en-US")
    finally:
        bundle.cleanup()
    if args.search:
        q = args.search.lower()
        pols = [p for p in pols if q in p["display"].lower() or q in p["name"].lower() or q in (p["key"] or "").lower()
                or q in (p["valueName"] or "").lower()]
    emit(args, pols, table([[p["display"][:70], p["class"], f"{p['key']}\\{p['valueName'] or ''}"[:80]] for p in pols],
                           ["Policy", "Scope", "Registry"]) + f"\n\n{len(pols)} policies in {src.name} {rel.version}")


def cmd_build(args):
    srcs = get_sources(args)
    index = build(srcs, Path(args.out), only=args.ids or None, force=args.force, mirror=args.mirror,
                  log=(lambda m: info(args, m)), backfill=args.backfill)
    failing = [p["id"] for p in index["products"] if (p.get("status") or {}).get("error")]
    emit(args, index, f"Catalog written to {args.out}: {len(index['products'])} products"
                      + (f", {len(failing)} failing: {', '.join(failing)}" if failing else ""))
    return 2 if failing and args.strict else 0


def cmd_validate(args):
    srcs = load_all(args.sources)
    emit(args, {"valid": True, "count": len(srcs)}, f"{len(srcs)} source definitions are valid.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="admxhub", description="Find, download and install Group Policy ADMX templates.")
    ap.add_argument("--version", action="version", version=f"admxhub {__version__}")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("-q", "--quiet", action="store_true", help="no progress messages")
    ap.add_argument("--catalog", help="catalog index.json URL or path (default: $ADMXHUB_CATALOG)")
    ap.add_argument("--sources", help="directory with source YAML files (default: bundled)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="no progress messages")
    common.add_argument("--catalog", default=argparse.SUPPRESS, help="catalog index.json URL or path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    command("sources", "list the known products").set_defaults(fn=cmd_sources)
    p = command("check", "show the latest version of products")
    p.add_argument("ids", nargs="*")
    p.add_argument("--live", action="store_true", help="ask the vendors directly instead of the catalog")
    p.set_defaults(fn=cmd_check)
    command("list", "list products in the published catalog").set_defaults(fn=cmd_list)

    p = command("download", "download and extract templates into a folder")
    p.add_argument("ids", nargs="+")
    p.add_argument("--out", default=".", help="target folder (default: current)")
    p.add_argument("--lang", action="append", help="only these ADML languages (repeatable), e.g. en-US")
    p.set_defaults(fn=cmd_download)

    def store_args(p):
        p.add_argument("--store", help="PolicyDefinitions folder (default: domain central store, else local)")

    p = command("install", "install templates into a PolicyDefinitions folder")
    p.add_argument("ids", nargs="+")
    store_args(p)
    p.add_argument("--lang", action="append", help="ADML languages to install (default: those already in the store)")
    p.add_argument("--all-languages", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(fn=cmd_install)

    p = command("status", "show installed products and available updates")
    store_args(p)
    p.add_argument("--all", action="store_true", help="include products that are not installed")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = command("update", "update every installed product that has a newer version")
    store_args(p)
    p.add_argument("--lang", action="append")
    p.add_argument("--all-languages", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(fn=cmd_update)

    p = command("policies", "list (or search) the policies of a product")
    p.add_argument("id")
    p.add_argument("--search")
    p.add_argument("--lang")
    p.set_defaults(fn=cmd_policies)

    p = command("build", "build the catalog (used by the GitHub Action)")
    p.add_argument("ids", nargs="*")
    p.add_argument("--out", default="public")
    p.add_argument("--force", action="store_true", help="re-download even if unchanged")
    p.add_argument("--mirror", action="store_true", help="copy templates of sources with mirror: true")
    p.add_argument("--strict", action="store_true", help="exit 2 when a source fails")
    p.add_argument("--backfill", type=int, default=0, metavar="N",
                   help="also add up to N past releases per product (sources with a history block)")
    p.set_defaults(fn=cmd_build)

    command("validate", "validate the source definitions").set_defaults(fn=cmd_validate)

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except (CliError, SourceError, DiscoveryError, FetchError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except httpx2.HTTPError as e:
        print(f"network error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
