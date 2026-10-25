"""Product source definitions (one YAML file per product in admxhub/sources/)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import jmespath
import yaml

DISCOVERY_TYPES = {
    "static": {"url"},
    "redirect": {"url"},
    "json": {"url", "fields"},
    "html": {"url", "match"},
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


class SourceError(ValueError):
    pass


@dataclass
class Source:
    id: str
    name: str
    vendor: str
    description: str
    homepage: str
    license: str
    discovery: dict
    markers: list[str] = field(default_factory=list)
    mirror: bool = False
    version_from: dict | None = None
    archive: dict = field(default_factory=dict)
    path: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "vendor": self.vendor, "description": self.description,
                "homepage": self.homepage, "license": self.license, "markers": self.markers,
                "discovery": self.discovery["type"], "mirror": self.mirror}


def validate(data: dict, path: str = "") -> Source:
    where = path or data.get("id", "?")
    for key in ("id", "name", "vendor", "description", "homepage", "license", "discovery"):
        if not data.get(key):
            raise SourceError(f"{where}: missing '{key}'")
    if not ID_RE.match(data["id"]):
        raise SourceError(f"{where}: id must be lowercase letters, digits and dashes")
    if path and Path(path).stem != data["id"]:
        raise SourceError(f"{where}: file name must match id '{data['id']}'")
    disc = data["discovery"]
    t = disc.get("type")
    if t not in DISCOVERY_TYPES:
        raise SourceError(f"{where}: unknown discovery type '{t}' (one of {', '.join(DISCOVERY_TYPES)})")
    missing = DISCOVERY_TYPES[t] - set(disc)
    if missing:
        raise SourceError(f"{where}: discovery type {t} needs {', '.join(sorted(missing))}")
    if not str(disc["url"]).startswith("https://"):
        raise SourceError(f"{where}: discovery.url must be https")
    regexes = {k: disc[k] for k in ("match", "version", "links") if k in disc}
    if isinstance(disc.get("filter"), dict):
        regexes["filter.regex"] = disc["filter"].get("regex", "")
    for k, v in regexes.items():
        try:
            re.compile(v)
        except re.error as e:
            raise SourceError(f"{where}: invalid regex in discovery.{k}: {e}")
    if t == "json":
        fields = disc.get("fields") or {}
        if not {"version", "url"} <= set(fields):
            raise SourceError(f"{where}: discovery.fields needs version and url")
        exprs = {**{f"fields.{k}": v for k, v in fields.items()},
                 **{k: disc[k] for k in ("items", "order_by") if k in disc}}
        if isinstance(disc.get("filter"), dict):
            exprs["filter.field"] = disc["filter"].get("field", "")
        for k, v in exprs.items():
            try:
                jmespath.compile(str(v))
            except jmespath.exceptions.JMESPathError as e:
                raise SourceError(f"{where}: invalid JMESPath in discovery.{k}: {e}")
    markers = [m.lower() for m in data.get("markers") or []]
    if not markers or not all(m.endswith(".admx") for m in markers):
        raise SourceError(f"{where}: 'markers' must list at least one .admx file name")
    if data.get("mirror") and data["license"].lower() == "proprietary":
        raise SourceError(f"{where}: mirroring is not allowed for proprietary templates")
    vf = data.get("version_from")
    if vf and not {"file", "pattern"} <= set(vf):
        raise SourceError(f"{where}: version_from needs 'file' and 'pattern'")
    return Source(id=data["id"], name=data["name"], vendor=data["vendor"], description=data["description"].strip(),
                  homepage=data["homepage"], license=str(data["license"]), discovery=disc, markers=markers,
                  mirror=bool(data.get("mirror")), version_from=vf, archive=data.get("archive") or {}, path=path)


def load_all(directory: str | Path | None = None) -> dict[str, Source]:
    """Load every source definition (bundled ones unless a directory is given)."""
    out: dict[str, Source] = {}
    if directory:
        files = sorted(Path(directory).glob("*.yaml"))
        items = [(str(f), f.read_text(encoding="utf-8")) for f in files]
    else:
        pkg = resources.files("admxhub") / "sources"
        items = [(f.name, f.read_text(encoding="utf-8")) for f in sorted(pkg.iterdir(), key=lambda p: p.name) if f.name.endswith(".yaml")]
    for name, text in items:
        data = yaml.safe_load(text) or {}
        src = validate(data, name)
        if src.id in out:
            raise SourceError(f"duplicate source id {src.id}")
        out[src.id] = src
    return out
