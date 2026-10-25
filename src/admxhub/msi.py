"""Extract files from a Windows Installer (.msi) package without msiexec or msitools.

An MSI is an OLE compound file holding a small relational database (string pool +
column-major tables) and, usually, the payload as an embedded cabinet. File names in the
cabinet are the File table keys; the real names and folders come from the File, Component
and Directory tables. Layout knowledge follows the format as implemented by Wine/msitools.
"""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import olefile

MIME = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"

TYPE_STRING = 0x0800
TYPE_VALID = 0x0100
TYPE_NULLABLE = 0x1000

# The _Columns table describes every other table, but its own layout is fixed.
COLUMNS_SCHEMA = [("Table", TYPE_VALID | TYPE_STRING | 64), ("Number", TYPE_VALID | 2),
                  ("Name", TYPE_VALID | TYPE_STRING | 64), ("Type", TYPE_VALID | 2)]


class MsiError(RuntimeError):
    pass


def decode_stream_name(name: str) -> str:
    out = []
    for ch in name:
        c = ord(ch)
        if 0x3800 <= c < 0x4800:
            c -= 0x3800
            out.append(MIME[c & 0x3F])
            out.append(MIME[(c >> 6) & 0x3F])
        elif 0x4800 <= c < 0x4840:
            out.append(MIME[c - 0x4800])
        elif c == 0x4840:
            out.append("!")  # table prefix
        else:
            out.append(ch)
    return "".join(out)


class MsiDatabase:
    def __init__(self, path: Path):
        if not olefile.isOleFile(str(path)):
            raise MsiError(f"{path.name} is not an MSI (OLE) file")
        self.ole = olefile.OleFileIO(str(path))
        self.streams = {decode_stream_name(e[0]): e[0] for e in self.ole.listdir(streams=True, storages=False) if len(e) == 1}
        self._load_strings()
        self.columns = self._load_columns()

    def close(self) -> None:
        self.ole.close()

    def stream(self, name: str) -> bytes:
        raw = self.streams.get(name)
        if raw is None:
            raise MsiError(f"stream {name!r} not found")
        return self.ole.openstream(raw).read()

    def _load_strings(self) -> None:
        pool = self.stream("!_StringPool")
        data = self.stream("!_StringData")
        words = struct.unpack(f"<{len(pool) // 2}H", pool[: len(pool) // 2 * 2])
        codepage = words[0] | ((words[1] & 0x7FFF) << 16)
        self.strref_size = 3 if words[1] & 0x8000 else 2
        enc = "utf-8" if codepage == 65001 else f"cp{codepage}" if codepage else "cp1252"
        try:
            "".encode(enc)
        except LookupError:
            enc = "cp1252"
        self.strings: dict[int, str] = {0: ""}
        count = len(words) // 2
        n, i, offset = 1, 1, 0
        while i < count:
            length, refs = words[i * 2], words[i * 2 + 1]
            if length == 0 and refs == 0:  # unused slot
                i += 1
                n += 1
                continue
            if length == 0:  # long string: high word of the length is in the next entry
                length = (words[i * 2 + 3] << 16) + words[i * 2 + 2]
                i += 2
            else:
                i += 1
            self.strings[n] = data[offset: offset + length].decode(enc, errors="replace")
            offset += length
            n += 1

    def _col_size(self, ctype: int) -> int:
        if ctype & TYPE_STRING:
            return self.strref_size if (ctype & ~TYPE_NULLABLE) != (TYPE_STRING | TYPE_VALID) else 2  # binary refs are 2
        return 2 if (ctype & 0xFF) <= 2 else 4

    def _read_table(self, name: str, schema: list[tuple[str, int]]) -> list[dict]:
        try:
            raw = self.stream("!" + name)
        except MsiError:
            return []
        sizes = [self._col_size(t) for _, t in schema]
        row_size = sum(sizes)
        nrows = len(raw) // row_size if row_size else 0
        cols, pos = [], 0
        for (cname, ctype), size in zip(schema, sizes):
            vals = []
            for r in range(nrows):
                chunk = raw[pos + r * size: pos + (r + 1) * size]
                v = int.from_bytes(chunk, "little")
                if ctype & TYPE_STRING:
                    vals.append(self.strings.get(v, ""))
                elif size == 2:
                    vals.append(None if v == 0 else v - 0x8000)
                else:
                    vals.append(None if v == 0 else v - 0x80000000)
            cols.append(vals)
            pos += size * nrows
        return [{schema[c][0]: cols[c][r] for c in range(len(schema))} for r in range(nrows)]

    def _load_columns(self) -> dict[str, list[tuple[str, int]]]:
        out: dict[str, list[tuple[int, str, int]]] = {}
        for row in self._read_table("_Columns", COLUMNS_SCHEMA):
            out.setdefault(row["Table"], []).append((row["Number"], row["Name"], row["Type"]))
        return {t: [(n, ty) for _, n, ty in sorted(cols)] for t, cols in out.items()}

    def table(self, name: str) -> list[dict]:
        if name not in self.columns:
            raise MsiError(f"table {name} not found")
        return self._read_table(name, self.columns[name])


def _long(name: str) -> str:
    return name.split("|", 1)[1] if "|" in name else name


def directory_paths(db: MsiDatabase) -> dict[str, Path]:
    """Directory key -> relative path of the administrative image (source layout)."""
    rows = {r["Directory"]: r for r in db.table("Directory")}
    cache: dict[str, Path] = {}

    def path(key: str, depth=0) -> Path:
        if key in cache:
            return cache[key]
        r = rows.get(key)
        if not r or depth > 64:
            return Path()
        parent = r["Directory_Parent"]
        default = r["DefaultDir"] or ""
        target, _, source = default.partition(":")
        name = _long(source or target)
        if not parent or parent == key:  # root (TARGETDIR = "SourceDir")
            p = Path()
        else:
            base = path(parent, depth + 1)
            p = base if name in (".", "") else base / name
        cache[key] = p
        return p

    return {k: path(k) for k in rows}


def extract_msi(path: Path, dest: Path, extract_cab) -> int:
    """Extract all files of an MSI into dest, preserving folders. extract_cab(cab, out_dir)
    must unpack a cabinet. Returns the number of files written."""
    db = MsiDatabase(path)
    try:
        dirs = directory_paths(db)
        comp_dir = {c["Component"]: c["Directory_"] for c in db.table("Component")}
        files = db.table("File")
        work = dest / ".msi-cab"
        work.mkdir(parents=True, exist_ok=True)
        for m in db.table("Media"):
            cab = m.get("Cabinet") or ""
            if not cab:
                continue
            if not cab.startswith("#"):
                raise MsiError(f"external cabinet {cab} is not supported")
            cab_file = work / cab[1:]
            cab_file.write_bytes(db.stream(cab[1:]))
            extract_cab(cab_file, work / (cab[1:] + ".x"))
    finally:
        db.close()
    extracted = {p.name: p for p in work.rglob("*") if p.is_file() and not p.name.lower().endswith(".cab")}
    n = 0
    for f in files:
        src = extracted.get(f["File"])
        if not src:
            continue  # file not compressed into an embedded cabinet
        target = dest / dirs.get(comp_dir.get(f["Component_"], ""), Path()) / _long(f["FileName"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), target)
        n += 1
    shutil.rmtree(work, ignore_errors=True)
    if not n:
        raise MsiError("no files could be extracted from the MSI")
    return n
