"""Download template bundles, extract them on any OS and collect the ADMX/ADML files."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from .discovery import UA, Release

LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8}){1,2}$", re.I)
MAX_BYTES = 1024 * 1024 * 1024  # 1 GB safety limit


class FetchError(RuntimeError):
    pass


@dataclass
class TemplateFile:
    name: str            # file name, e.g. msedge.admx
    kind: str            # admx | adml
    lang: str | None     # e.g. en-US for adml
    path: Path           # extracted location
    sha256: str = ""
    size: int = 0

    @property
    def target(self) -> str:
        """Relative path inside a PolicyDefinitions folder."""
        return self.name if self.kind == "admx" else f"{self.lang}/{self.name}"

    def to_json(self) -> dict:
        return {"name": self.name, "kind": self.kind, "lang": self.lang, "sha256": self.sha256, "size": self.size}


@dataclass
class Bundle:
    archive: Path
    sha256: str
    size: int
    files: list[TemplateFile] = field(default_factory=list)
    workdir: Path | None = None

    @property
    def languages(self) -> list[str]:
        return sorted({f.lang for f in self.files if f.lang})

    def cleanup(self) -> None:
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)


def normalize_lang(lang: str) -> str:
    parts = lang.split("-")
    return "-".join([parts[0].lower()] + [p.upper() if len(p) <= 3 else p.title() for p in parts[1:]])


def download(release: Release, dest_dir: Path, progress=None) -> tuple[Path, str, int]:
    name = re.sub(r"[^\w .()+-]", "_", os.path.basename(release.url.split("?")[0])) or "download.bin"
    path = dest_dir / name
    h = hashlib.sha256()
    total = 0
    with httpx2.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=120) as c:
        with c.stream("GET", release.url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise FetchError("download larger than 1 GB")
                    h.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(total)
    digest = h.hexdigest()
    if release.sha256 and release.sha256.lower() != digest:
        raise FetchError(f"checksum mismatch: vendor says {release.sha256}, got {digest}")
    return path, digest, total


def _run(cmd: list[str], timeout=600) -> str:
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        return (r.stdout + r.stderr).decode(errors="replace").strip()
    except FileNotFoundError:
        raise FetchError(f"'{cmd[0]}' is required to extract this file but was not found")
    except subprocess.CalledProcessError as e:
        out = (e.stderr or e.stdout or b"").decode(errors="replace").strip()[:300]
        raise FetchError(f"{cmd[0]} failed ({e.returncode}): {out}")


def _extracted_something(d: Path, archive: Path) -> bool:
    """True if d contains files other than a mere copy of the archive itself
    (expand.exe "extracts" some cabinets by writing the cabinet out again)."""
    return d.is_dir() and any(p.is_file() and p.name.lower() != archive.name.lower() for p in d.rglob("*"))


def _first_that_works(path: Path, dest: Path, commands: list[list[str]]) -> None:
    """Try extractors in order until one actually produces files. Some tools (expand.exe)
    report success even when they extract nothing, so the result is checked, not the exit code."""
    notes = []
    for cmd in commands:
        if not shutil.which(cmd[0]):
            notes.append(f"{cmd[0]}: not available")
            continue
        try:
            out = _run(cmd)
        except FetchError as e:
            notes.append(str(e))
            continue
        if _extracted_something(dest, path):
            return
        for p in dest.rglob("*"):  # clean up a useless copy before the next tool runs
            if p.is_file():
                p.unlink()
        notes.append(f"{cmd[0]}: produced no files ({out[:200]!r})")
    raise FetchError(f"could not extract {path.name}: " + "; ".join(notes))


def _msi_log_excerpt(log: Path) -> str:
    """The interesting lines of a verbose msiexec log (errors and the failing action)."""
    try:
        text = log.read_bytes().decode("utf-16", errors="replace") if log.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff") \
            else log.read_text(errors="replace")
    except OSError:
        return ""
    keep = [ln.strip() for ln in text.splitlines()
            if re.search(r"(Error \d{4}|Note: 1: \d{4}|Return value 3|error code)", ln)]
    return ("msiexec log: " + " | ".join(keep[-8:])) if keep else ""


def _extract_zip(path: Path, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > 4 * MAX_BYTES:
            raise FetchError("archive too large when extracted")
        for i in z.infolist():
            target = (dest / i.filename).resolve()
            if root not in target.parents and target != root:
                continue  # zip-slip
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def extract(path: Path, dest: Path, archive: dict | None = None) -> None:
    """Extract one archive with what the current OS offers. archive.exe_args (from the source
    definition) tells how a self-extracting .exe unpacks itself on Windows."""
    dest.mkdir(parents=True, exist_ok=True)
    low = path.name.lower()
    win = sys.platform == "win32"
    if win:
        # long path names: msiexec /a rejects 8.3 components like C:\Users\RUNNER~1 in TARGETDIR
        dest = Path(os.path.realpath(dest))
        path = Path(os.path.realpath(path))
    if low.endswith(".zip"):
        _extract_zip(path, dest)
    elif low.endswith(".msi"):
        if win:  # administrative install = plain extraction, no elevation needed
            log = dest.parent / (path.name + ".log")
            try:
                _run(["msiexec", "/a", str(path), "/qn", f"TARGETDIR={dest}", "/l*v", str(log)])
            except FetchError as e:
                raise FetchError(f"{e} {_msi_log_excerpt(log)}") from None
        else:  # (7z is no alternative here: it yields internal MSI file ids instead of file names)
            if not shutil.which("msiextract"):
                raise FetchError("msiextract (package 'msitools') is required to unpack .msi files on this OS")
            _run(["msiextract", "-C", str(dest), str(path)])
    elif low.endswith(".cab"):
        # tar = libarchive's bsdtar (built into Windows 10+ and macOS), which reads cabinets too
        if win:
            _first_that_works(path, dest, [["tar", "-xf", str(path), "-C", str(dest)], ["expand.exe", str(path), "-F:*", str(dest)]])
        else:
            _first_that_works(path, dest, [["cabextract", "-q", "-d", str(dest), str(path)], ["7z", "x", "-y", f"-o{dest}", str(path)],
                                           ["tar", "-xf", str(path), "-C", str(dest)]])
    elif low.endswith(".exe"):
        # self-extracting packages usually embed a cabinet, which cabextract/7z can read directly
        exe_args = (archive or {}).get("exe_args")
        if win and exe_args:
            _run([str(path)] + [a.replace("{dest}", str(dest)) for a in exe_args])
        elif shutil.which("cabextract"):  # (7z mangles the embedded file names)
            _run(["cabextract", "-q", "-d", str(dest), str(path)])
        else:
            raise FetchError("this .exe package needs archive.exe_args in its source (Windows) or cabextract")


def expand_all(root: Path, depth: int = 0) -> None:
    """Recursively extract archives found inside archives (e.g. Edge: cab → zip)."""
    if depth > 3:
        return
    for p in list(root.rglob("*")):
        # nested .exe files are installers inside bundles (e.g. FSLogix), not template packages
        if p.is_file() and p.name.lower().endswith((".zip", ".msi", ".cab")):
            out = p.with_name(p.name + ".x")
            if out.exists():
                continue
            extract(p, out)
            expand_all(out, depth + 1)


def collect(root: Path, include: list[str] | None = None) -> list[TemplateFile]:
    """Find ADMX/ADML files; ADML language comes from the nearest language-named folder."""
    pats = [re.compile(p, re.I) for p in include or []]
    found: dict[str, TemplateFile] = {}
    for p in sorted(root.rglob("*")):
        low = p.name.lower()
        if not p.is_file() or not low.endswith((".admx", ".adml")):
            continue
        rel = p.relative_to(root).as_posix()
        if pats and not any(x.search(rel) for x in pats):
            continue
        if low.endswith(".admx"):
            tf = TemplateFile(p.name, "admx", None, p)
        else:
            lang = next((x for x in reversed(rel.split("/")[:-1]) if LANG_RE.match(x)), "en-US")
            tf = TemplateFile(p.name, "adml", normalize_lang(lang), p)
        if tf.target.lower() in found:
            continue  # first one wins (bundles sometimes ship duplicates/examples)
        data = p.read_bytes()
        tf.sha256 = hashlib.sha256(data).hexdigest()
        tf.size = len(data)
        found[tf.target.lower()] = tf
    return sorted(found.values(), key=lambda f: (f.lang or "", f.name.lower()))


def fetch(release: Release, archive: dict | None = None, workdir: Path | None = None, progress=None) -> Bundle:
    """Download and extract; archive = the source's archive settings (include, exe_args)."""
    archive = archive or {}
    work = Path(os.path.realpath(workdir or tempfile.mkdtemp(prefix="admxhub-")))
    dl = work / "download"
    dl.mkdir(parents=True, exist_ok=True)
    path, digest, size = download(release, dl, progress)
    if path.name.lower().endswith((".admx", ".adml")):
        files_root = dl
    else:
        files_root = work / "x"
        extract(path, files_root, archive)
        expand_all(files_root)
    files = collect(files_root, archive.get("include"))
    if not files:
        found = [p.relative_to(work).as_posix() for p in sorted(work.rglob("*"))[:15]]
        raise FetchError(f"no .admx/.adml files found in the download (extracted: {', '.join(found) or 'nothing'})")
    return Bundle(archive=path, sha256=digest, size=size, files=files, workdir=work)
