"""Reader/writer for Registry.pol (PReg) files used by Group Policy.

Format: b'PReg' + uint32 version (1), followed by entries of the form
    [key;value;type;size;data]
where brackets/semicolons and key/value are UTF-16LE (key/value null-terminated),
type and size are little-endian uint32 and data is `size` raw bytes.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

SIGNATURE = b"PReg"
VERSION = 1

REG_NONE = 0
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4
REG_DWORD_BIG_ENDIAN = 5
REG_MULTI_SZ = 7
REG_QWORD = 11

TYPE_NAMES = {
    REG_NONE: "REG_NONE",
    REG_SZ: "REG_SZ",
    REG_EXPAND_SZ: "REG_EXPAND_SZ",
    REG_BINARY: "REG_BINARY",
    REG_DWORD: "REG_DWORD",
    REG_DWORD_BIG_ENDIAN: "REG_DWORD_BIG_ENDIAN",
    REG_MULTI_SZ: "REG_MULTI_SZ",
    REG_QWORD: "REG_QWORD",
}
TYPE_BY_NAME = {v: k for k, v in TYPE_NAMES.items()}

_LB = "[".encode("utf-16-le")
_RB = "]".encode("utf-16-le")
_SEMI = ";".encode("utf-16-le")


class RegPolError(ValueError):
    pass


@dataclass
class Entry:
    key: str
    value: str
    type: int
    data: bytes = b""

    # --- typed accessors -------------------------------------------------
    def decoded(self):
        """Return the data as a Python value according to the type."""
        return decode_data(self.type, self.data)

    @property
    def is_delete_marker(self) -> bool:
        return self.value.lower().startswith("**")

    def to_json(self) -> dict:
        try:
            val = self.decoded()
        except Exception:
            val = self.data.hex()
        if isinstance(val, bytes):
            val = val.hex()
        return {
            "key": self.key,
            "value": self.value,
            "type": TYPE_NAMES.get(self.type, str(self.type)),
            "data": val,
        }


def decode_data(rtype: int, data: bytes):
    if rtype in (REG_SZ, REG_EXPAND_SZ):
        return data.decode("utf-16-le").split("\x00", 1)[0]
    if rtype == REG_MULTI_SZ:
        s = data.decode("utf-16-le")
        return [p for p in s.split("\x00") if p != ""]
    if rtype == REG_DWORD:
        if len(data) < 4:
            data = data.ljust(4, b"\0")
        return struct.unpack("<I", data[:4])[0]
    if rtype == REG_DWORD_BIG_ENDIAN:
        return struct.unpack(">I", data[:4])[0]
    if rtype == REG_QWORD:
        if len(data) < 8:
            data = data.ljust(8, b"\0")
        return struct.unpack("<Q", data[:8])[0]
    return data


def encode_data(rtype: int, value) -> bytes:
    if rtype in (REG_SZ, REG_EXPAND_SZ):
        return (str(value) + "\x00").encode("utf-16-le")
    if rtype == REG_MULTI_SZ:
        items = list(value or [])
        return ("".join(f"{s}\x00" for s in items) + "\x00").encode("utf-16-le")
    if rtype == REG_DWORD:
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if rtype == REG_DWORD_BIG_ENDIAN:
        return struct.pack(">I", int(value) & 0xFFFFFFFF)
    if rtype == REG_QWORD:
        return struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
    if rtype in (REG_BINARY, REG_NONE):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return bytes.fromhex(str(value or ""))
    raise RegPolError(f"unsupported registry type {rtype}")


@dataclass
class RegistryPol:
    entries: list[Entry] = field(default_factory=list)

    # --- parsing / serialising -------------------------------------------
    @classmethod
    def parse(cls, raw: bytes) -> "RegistryPol":
        if not raw:
            return cls()
        if raw[:4] != SIGNATURE:
            raise RegPolError("not a PReg file (bad signature)")
        (version,) = struct.unpack("<I", raw[4:8])
        if version != VERSION:
            raise RegPolError(f"unsupported PReg version {version}")
        pos = 8
        entries: list[Entry] = []
        n = len(raw)
        while pos < n:
            if raw[pos : pos + 2] != _LB:
                raise RegPolError(f"expected '[' at offset {pos}")
            pos += 2
            key, pos = _read_wstr(raw, pos)
            pos = _expect(raw, pos, _SEMI)
            value, pos = _read_wstr(raw, pos)
            pos = _expect(raw, pos, _SEMI)
            (rtype,) = struct.unpack("<I", raw[pos : pos + 4])
            pos += 4
            pos = _expect(raw, pos, _SEMI)
            (size,) = struct.unpack("<I", raw[pos : pos + 4])
            pos += 4
            pos = _expect(raw, pos, _SEMI)
            data = raw[pos : pos + size]
            if len(data) != size:
                raise RegPolError("truncated data")
            pos += size
            pos = _expect(raw, pos, _RB)
            entries.append(Entry(key, value, rtype, data))
        return cls(entries)

    def serialize(self) -> bytes:
        out = bytearray(SIGNATURE + struct.pack("<I", VERSION))
        for e in self.entries:
            out += _LB
            out += (e.key + "\x00").encode("utf-16-le")
            out += _SEMI
            out += (e.value + "\x00").encode("utf-16-le")
            out += _SEMI
            out += struct.pack("<I", e.type)
            out += _SEMI
            out += struct.pack("<I", len(e.data))
            out += _SEMI
            out += e.data
            out += _RB
        return bytes(out)

    # --- queries -----------------------------------------------------------
    def find(self, key: str, value: str) -> Entry | None:
        k, v = key.lower(), value.lower()
        for e in self.entries:
            if e.key.lower() == k and e.value.lower() == v:
                return e
        return None

    def values_under(self, key: str) -> list[Entry]:
        k = key.lower()
        return [e for e in self.entries if e.key.lower() == k]

    def is_deleted(self, key: str, value: str) -> bool:
        """True if a **del.<value> marker exists for the value."""
        return self.find(key, f"**del.{value}") is not None

    def has_delvals(self, key: str) -> bool:
        return self.find(key, "**delvals.") is not None

    # --- mutations ---------------------------------------------------------
    def remove(self, key: str, value: str) -> None:
        """Remove a value and any delete marker for it."""
        k = key.lower()
        names = {value.lower(), f"**del.{value}".lower()}
        self.entries = [
            e for e in self.entries if not (e.key.lower() == k and e.value.lower() in names)
        ]

    def remove_key(self, key: str) -> None:
        k = key.lower()
        self.entries = [e for e in self.entries if e.key.lower() != k]

    def set(self, key: str, value: str, rtype: int, data) -> None:
        raw = bytes(data) if isinstance(data, (bytes, bytearray)) else encode_data(rtype, data)
        self.remove(key, value)
        self.entries.append(Entry(key, value, rtype, raw))

    def mark_deleted(self, key: str, value: str) -> None:
        self.remove(key, value)
        # Windows writes a single space as data for delete markers
        self.entries.append(Entry(key, f"**del.{value}", REG_SZ, encode_data(REG_SZ, " ")))

    def mark_delvals(self, key: str) -> None:
        self.entries.append(Entry(key, "**delvals.", REG_SZ, encode_data(REG_SZ, " ")))

    def sort(self) -> None:
        """Group entries by key (stable), keeping **delvals. first within a key."""
        order: dict[str, int] = {}
        for e in self.entries:
            order.setdefault(e.key.lower(), len(order))
        self.entries.sort(
            key=lambda e: (order[e.key.lower()], 0 if e.value.lower() == "**delvals." else 1)
        )


def _read_wstr(raw: bytes, pos: int) -> tuple[str, int]:
    start = pos
    while pos + 1 < len(raw):
        if raw[pos] == 0 and raw[pos + 1] == 0:
            s = raw[start:pos].decode("utf-16-le")
            return s, pos + 2
        pos += 2
    raise RegPolError("unterminated string")


def _expect(raw: bytes, pos: int, token: bytes) -> int:
    if raw[pos : pos + len(token)] != token:
        raise RegPolError(f"expected {token.decode('utf-16-le')!r} at offset {pos}")
    return pos + len(token)
