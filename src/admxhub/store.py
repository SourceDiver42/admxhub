"""Install templates into a PolicyDefinitions folder (domain central store or local)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .catalog import namespace_of
from .fetch import LANG_RE, Bundle
from .sources import Source


def default_store() -> Path | None:
    """The domain central store when the machine is domain-joined, else the local folder (Windows only)."""
    env = os.environ.get("ADMXHUB_STORE")
    if env:
        return Path(env)
    if sys.platform != "win32":
        return None
    dom = os.environ.get("USERDNSDOMAIN")
    if dom:
        central = Path(rf"\\{dom}\SYSVOL\{dom}\Policies\PolicyDefinitions")
        if central.exists():
            return central
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "PolicyDefinitions"


def _state_file() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "admxhub" / "state.json"


def load_state(store: Path) -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8")).get(str(store).lower(), {})
    except (FileNotFoundError, ValueError):
        return {}


def save_state(store: Path, product: str, version: str, files: list[str], fingerprint: str = "") -> None:
    p = _state_file()
    try:
        allstate = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        allstate = {}
    allstate.setdefault(str(store).lower(), {})[product] = {"version": version, "files": files, "fingerprint": fingerprint}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(allstate, indent=1), encoding="utf-8")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_dir()

    def admx_files(self) -> dict[str, Path]:
        if not self.exists():
            return {}
        return {p.name.lower(): p for p in self.path.iterdir() if p.is_file() and p.name.lower().endswith(".admx")}

    def languages(self) -> dict[str, Path]:
        if not self.exists():
            return {}
        return {p.name.lower(): p for p in self.path.iterdir() if p.is_dir() and LANG_RE.match(p.name)}

    def installed(self, sources: dict[str, Source]) -> dict[str, dict]:
        files = self.admx_files()
        state = load_state(self.path)
        out = {}
        for sid, src in sources.items():
            if any(m in files for m in src.markers):
                st = state.get(sid) or {}
                out[sid] = {"version": st.get("version"), "fingerprint": st.get("fingerprint", "")}
        return out

    def namespaces(self) -> dict[str, str]:
        out = {}
        for name, p in self.admx_files().items():
            try:
                ns = namespace_of(p.read_bytes())
            except OSError:
                continue
            if ns:
                out[ns] = name
        return out


@dataclass
class PlanItem:
    target: str
    source: Path
    status: str          # new | update | identical
    warning: str | None = None

    def to_json(self) -> dict:
        return {"target": self.target, "status": self.status, "warning": self.warning}


def plan(bundle: Bundle, store: Store, languages: list[str] | None) -> list[PlanItem]:
    """Which files would be written. languages=None: the store's existing languages (en-US if none)."""
    existing_langs = store.languages()
    if languages is None:
        wanted = set(existing_langs) or {"en-us"}
    else:
        wanted = {l.lower() for l in languages}
    ns_existing = store.namespaces()
    items = []
    for f in bundle.files:
        if f.kind == "adml" and f.lang.lower() not in wanted and "*" not in wanted:
            continue
        lang_dir = existing_langs.get(f.lang.lower()).name if f.kind == "adml" and f.lang.lower() in existing_langs else f.lang
        target = f.name if f.kind == "admx" else f"{lang_dir}/{f.name}"
        dest = store.path / target
        if dest.exists():
            status = "identical" if hashlib.sha256(dest.read_bytes()).hexdigest() == f.sha256 else "update"
        else:
            status = "new"
        warning = None
        if f.kind == "admx":
            ns = namespace_of(f.path.read_bytes())
            other = ns_existing.get(ns) if ns else None
            if other and other != f.name.lower():
                warning = f"namespace {ns} is already defined by {other}; remove it or policies will appear twice"
        items.append(PlanItem(target, f.path, status, warning))
    return items


def apply(items: list[PlanItem], store: Store) -> int:
    store.path.mkdir(parents=True, exist_ok=True)
    n = 0
    for it in items:
        if it.status == "identical":
            continue
        dest = store.path / it.target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(it.source, dest)
        n += 1
    return n
