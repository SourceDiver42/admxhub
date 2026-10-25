"""ADMX/ADML parsing and Administrative Template policy evaluation (shared with ADWeb).

The semantics (how a policy's state is detected from a Registry.pol and which
registry values are written for Enabled/Disabled) follow what gpedit.msc does,
as documented in MS "ADMX schema" and implemented by PolicyPlus.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from .regpol import (
    REG_DWORD,
    REG_EXPAND_SZ,
    REG_MULTI_SZ,
    REG_QWORD,
    REG_SZ,
    RegistryPol,
)

STATE_NOT_CONFIGURED = "NotConfigured"
STATE_ENABLED = "Enabled"
STATE_DISABLED = "Disabled"


class PolicyValueError(ValueError):
    pass


# ---------------------------------------------------------------------------
# data model


@dataclass
class ValueSpec:
    kind: str  # decimal | longDecimal | string | delete
    value: object = None


@dataclass
class ListItem:
    key: str
    value_name: str
    spec: ValueSpec


@dataclass
class Element:
    type: str  # boolean | decimal | longDecimal | text | multiText | enum | list
    id: str
    key: str
    value_name: Optional[str]
    attrs: dict = field(default_factory=dict)
    # boolean
    true_value: Optional[ValueSpec] = None
    false_value: Optional[ValueSpec] = None
    true_list: list[ListItem] = field(default_factory=list)
    false_list: list[ListItem] = field(default_factory=list)
    # enum: list of (display, ValueSpec, [ListItem])
    items: list[tuple[str, ValueSpec, list[ListItem]]] = field(default_factory=list)


@dataclass
class Policy:
    id: str  # namespace:name
    name: str
    cls: str  # Machine | User | Both
    display: str
    explain: str
    key: str
    value_name: Optional[str]
    category: Optional[str]
    supported: str
    file: str
    enabled_value: Optional[ValueSpec] = None
    disabled_value: Optional[ValueSpec] = None
    enabled_list: list[ListItem] = field(default_factory=list)
    disabled_list: list[ListItem] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)
    presentation: list[dict] = field(default_factory=list)

    def applies_to(self, scope: str) -> bool:
        return self.cls == "Both" or self.cls == scope


@dataclass
class Category:
    id: str
    name: str
    display: str
    explain: str
    parent: Optional[str]
    file: str


# ---------------------------------------------------------------------------
# parsing


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def _decode_xml(raw: bytes) -> str:
    """Decode ADMX/ADML bytes; some files declare bogus encodings like 'unicode'."""
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    elif raw[:2] == b"<\x00":
        text = raw.decode("utf-16-le")
    else:
        head = raw[:200].decode("ascii", "replace")
        m = re.search(r'encoding=["\']([A-Za-z0-9_.-]+)', head)
        enc = (m.group(1) if m else "utf-8").lower()
        try:
            text = raw.decode("utf-8" if enc in ("unicode", "utf8") else enc)
        except (LookupError, UnicodeDecodeError):
            text = raw.decode("utf-8", "replace")
    return _XML_DECL.sub("", text, count=1)


def _parse_xml(raw: bytes) -> ET.Element:
    return _strip_ns(ET.fromstring(_decode_xml(raw)))


_STRING_REF = re.compile(r"^\$\(string\.([^)]+)\)$")
_PRES_REF = re.compile(r"^\$\(presentation\.([^)]+)\)$")


class _Adml:
    def __init__(self, raw: Optional[bytes]):
        self.strings: dict[str, str] = {}
        self.presentations: dict[str, ET.Element] = {}
        if not raw:
            return
        root = _parse_xml(raw)
        res = root.find("resources")
        if res is None:
            return
        st = res.find("stringTable")
        if st is not None:
            for s in st.findall("string"):
                self.strings[s.get("id")] = s.text or ""
        pt = res.find("presentationTable")
        if pt is not None:
            for p in pt.findall("presentation"):
                self.presentations[p.get("id")] = p

    def resolve(self, ref: Optional[str]) -> str:
        if not ref:
            return ""
        m = _STRING_REF.match(ref.strip())
        if not m:
            return ref
        return self.strings.get(m.group(1), m.group(1))


def _value_spec(el: Optional[ET.Element]) -> Optional[ValueSpec]:
    """Parse a <value>-like element (enabledValue, trueValue, value, ...)."""
    if el is None:
        return None
    for child in el:
        tag = child.tag
        if tag == "decimal":
            return ValueSpec("decimal", _num(child.get("value"), 0))
        if tag == "longDecimal":
            return ValueSpec("longDecimal", _num(child.get("value"), 0))
        if tag == "string":
            return ValueSpec("string", child.text or "")
        if tag == "delete":
            return ValueSpec("delete")
    return None


def _value_list(el: Optional[ET.Element], default_key: str) -> list[ListItem]:
    if el is None:
        return []
    dk = el.get("defaultKey") or default_key
    out = []
    for item in el.findall("item"):
        spec = _value_spec(item.find("value"))
        if spec is None:
            continue
        out.append(ListItem(item.get("key") or dk, item.get("valueName", ""), spec))
    return out


def _num(v: Optional[str], default: int) -> int:
    """Lenient integer parse: vendor templates sometimes carry empty or odd numeric attributes."""
    try:
        return int(str(v).strip()) if v is not None and str(v).strip() != "" else default
    except ValueError:
        return default


def _bool(v: Optional[str], default=False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("true", "1")


class AdmxSet:
    """All policies/categories from a set of ADMX files, localised via ADML."""

    def __init__(self):
        self.policies: dict[str, Policy] = {}
        self.categories: dict[str, Category] = {}
        self.supported: dict[str, str] = {}
        self.errors: list[str] = []
        self.files: list[str] = []

    @classmethod
    def load(cls, admx_files: dict[str, bytes], adml_files: dict[str, bytes]) -> "AdmxSet":
        """admx_files: {basename.admx: bytes}; adml_files: {basename.adml: bytes} (one language)."""
        s = cls()
        adml_by_base = {k.lower()[:-5]: v for k, v in adml_files.items()}
        parsed = []
        for fname, raw in sorted(admx_files.items()):
            base = fname.lower()[:-5]
            try:
                root = _parse_xml(raw)
                adml = _Adml(adml_by_base.get(base))
            except (ET.ParseError, ValueError) as e:
                s.errors.append(f"{fname}: {e}")
                continue
            parsed.append((fname, root, adml))
            s.files.append(fname)
        # pass 1: namespaces + supportedOn definitions + categories
        ctx = []
        for fname, root, adml in parsed:
            nsmap, target = _namespaces(root)
            ctx.append((fname, root, adml, nsmap, target))
            so = root.find("supportedOn")
            if so is not None:
                defs = so.find("definitions")
                if defs is not None:
                    for d in defs.findall("definition"):
                        s.supported[f"{target}:{d.get('name')}"] = adml.resolve(d.get("displayName"))
            cats = root.find("categories")
            if cats is not None:
                for c in cats.findall("category"):
                    cid = f"{target}:{c.get('name')}"
                    pc = c.find("parentCategory")
                    parent = _qualify(pc.get("ref"), nsmap, target) if pc is not None else None
                    s.categories[cid] = Category(
                        cid, c.get("name"), adml.resolve(c.get("displayName")) or c.get("name"),
                        adml.resolve(c.get("explainText")), parent, fname,
                    )
        # pass 2: policies
        for fname, root, adml, nsmap, target in ctx:
            pols = root.find("policies")
            if pols is None:
                continue
            for p in pols.findall("policy"):
                try:
                    pol = _parse_policy(p, fname, adml, nsmap, target, s.supported)
                except Exception as e:  # keep going on odd vendor files
                    s.errors.append(f"{fname}: policy {p.get('name')}: {e}")
                    continue
                s.policies[pol.id] = pol
        return s

    # -- tree ----------------------------------------------------------------
    def tree(self, scope: str) -> list[dict]:
        """Category tree containing policies applicable to scope (Machine/User)."""
        counts: dict[Optional[str], int] = {}
        for p in self.policies.values():
            if p.applies_to(scope):
                counts[p.category] = counts.get(p.category, 0) + 1
        children: dict[Optional[str], list[str]] = {}
        for cid, c in self.categories.items():
            parent = c.parent if c.parent in self.categories else None
            children.setdefault(parent, []).append(cid)

        def build(cid: str, seen: frozenset) -> Optional[dict]:
            if cid in seen:
                return None
            kids = [build(k, seen | {cid}) for k in children.get(cid, [])]
            kids = sorted([k for k in kids if k], key=lambda k: k["name"].lower())
            n = counts.get(cid, 0)
            total = n + sum(k["total"] for k in kids)
            if total == 0:
                return None
            return {"id": cid, "name": self.categories[cid].display, "count": n, "total": total, "children": kids}

        roots = [build(c, frozenset()) for c in children.get(None, [])]
        roots = sorted([r for r in roots if r], key=lambda r: r["name"].lower())
        orphans = sum(1 for p in self.policies.values() if p.applies_to(scope) and p.category not in self.categories)
        if orphans:
            roots.append({"id": "__orphans__", "name": "(Uncategorised)", "count": orphans, "total": orphans, "children": []})
        return roots

    def policies_in(self, category: str, scope: str) -> list[Policy]:
        if category == "__orphans__":
            sel = [p for p in self.policies.values() if p.applies_to(scope) and p.category not in self.categories]
        else:
            sel = [p for p in self.policies.values() if p.applies_to(scope) and p.category == category]
        return sorted(sel, key=lambda p: p.display.lower())

    def category_path(self, cid: Optional[str]) -> list[str]:
        out, seen = [], set()
        while cid and cid in self.categories and cid not in seen:
            seen.add(cid)
            out.append(self.categories[cid].display)
            cid = self.categories[cid].parent
        return list(reversed(out))

    def search(self, q: str, scope: str, limit=300) -> list[Policy]:
        q = q.lower().strip()
        out = [
            p for p in self.policies.values()
            if p.applies_to(scope) and (q in p.display.lower() or q in p.name.lower() or q in p.key.lower()
                                        or (p.value_name and q in p.value_name.lower()))
        ]
        return sorted(out, key=lambda p: p.display.lower())[:limit]


def _namespaces(root: ET.Element) -> tuple[dict, str]:
    nsmap, target = {}, ""
    pn = root.find("policyNamespaces")
    if pn is not None:
        t = pn.find("target")
        if t is not None:
            target = t.get("namespace", "")
            nsmap[t.get("prefix")] = target
        for u in pn.findall("using"):
            nsmap[u.get("prefix")] = u.get("namespace")
    return nsmap, target


def _qualify(ref: Optional[str], nsmap: dict, target: str) -> Optional[str]:
    if not ref:
        return None
    if ":" in ref:
        prefix, name = ref.split(":", 1)
        return f"{nsmap.get(prefix, prefix)}:{name}"
    return f"{target}:{ref}"


def _parse_policy(p: ET.Element, fname: str, adml: _Adml, nsmap, target, supported) -> Policy:
    key = p.get("key", "")
    pc = p.find("parentCategory")
    so = p.find("supportedOn")
    so_ref = _qualify(so.get("ref"), nsmap, target) if so is not None else None
    pol = Policy(
        id=f"{target}:{p.get('name')}",
        name=p.get("name"),
        cls=p.get("class", "Both"),
        display=adml.resolve(p.get("displayName")) or p.get("name"),
        explain=adml.resolve(p.get("explainText")),
        key=key,
        value_name=p.get("valueName"),
        category=_qualify(pc.get("ref"), nsmap, target) if pc is not None else None,
        supported=supported.get(so_ref, so_ref.split(":", 1)[1] if so_ref else ""),
        file=fname,
        enabled_value=_value_spec(p.find("enabledValue")),
        disabled_value=_value_spec(p.find("disabledValue")),
        enabled_list=_value_list(p.find("enabledList"), key),
        disabled_list=_value_list(p.find("disabledList"), key),
    )
    els = p.find("elements")
    if els is not None:
        for e in els:
            pol.elements.append(_parse_element(e, key, adml))
    pres_ref = p.get("presentation")
    if pres_ref:
        m = _PRES_REF.match(pres_ref.strip())
        pres_el = adml.presentations.get(m.group(1)) if m else None
        if pres_el is not None:
            pol.presentation = _parse_presentation(pres_el, adml)
    if pol.elements and not pol.presentation:
        pol.presentation = [{"type": "auto", "refId": e.id, "label": e.id} for e in pol.elements]
    return pol


def _parse_element(e: ET.Element, policy_key: str, adml: _Adml) -> Element:
    el = Element(
        type=e.tag,
        id=e.get("id"),
        key=e.get("key") or policy_key,
        value_name=e.get("valueName"),
        attrs=dict(e.attrib),
    )
    if e.tag == "boolean":
        tv, fv = e.find("trueValue"), e.find("falseValue")
        el.true_value = _value_spec(tv)
        el.false_value = _value_spec(fv)
        el.true_list = _value_list(e.find("trueList"), el.key)
        el.false_list = _value_list(e.find("falseList"), el.key)
    elif e.tag == "enum":
        for it in e.findall("item"):
            spec = _value_spec(it.find("value")) or ValueSpec("delete")
            el.items.append((adml.resolve(it.get("displayName")), spec, _value_list(it.find("valueList"), el.key)))
    return el


def _parse_presentation(pres: ET.Element, adml: _Adml) -> list[dict]:
    out = []
    for c in pres:
        t = c.tag
        d: dict = {"type": t, "refId": c.get("refId")}
        if t == "text":
            d["label"] = (c.text or "").strip()
        elif t in ("textBox", "comboBox"):
            lbl = c.find("label")
            d["label"] = (lbl.text or "").strip() if lbl is not None else ""
            dv = c.find("defaultValue") if t == "textBox" else c.find("default")
            d["default"] = (dv.text or "") if dv is not None else ""
            if t == "comboBox":
                d["suggestions"] = [(s.text or "") for s in c.findall("suggestion")]
        elif t == "checkBox":
            d["label"] = (c.text or "").strip()
            d["default"] = _bool(c.get("defaultChecked"))
        elif t in ("decimalTextBox", "longDecimalTextBox"):
            d["label"] = (c.text or "").strip()
            d["default"] = _num(c.get("defaultValue"), 1)
            d["spin"] = _bool(c.get("spin"), True)
            d["spinStep"] = _num(c.get("spinStep"), 1)
        elif t == "dropdownList":
            d["label"] = (c.text or "").strip()
            d["default"] = _num(c.get("defaultItem"), 0) if c.get("defaultItem") is not None else None
            d["noSort"] = _bool(c.get("noSort"))
        elif t in ("listBox", "multiTextBox"):
            d["label"] = (c.text or "").strip()
        else:
            d["label"] = (c.text or "").strip()
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# evaluation against a Registry.pol


def _spec_matches(pol: RegistryPol, key: str, vn: str, spec: ValueSpec) -> bool:
    if spec.kind == "delete":
        return pol.is_deleted(key, vn)
    e = pol.find(key, vn)
    if e is None:
        return False
    try:
        v = e.decoded()
    except Exception:
        return False
    if spec.kind in ("decimal", "longDecimal"):
        if isinstance(v, str):
            try:
                v = int(v)
            except ValueError:
                return False
        return v == spec.value
    return isinstance(v, str) and v == spec.value


def _list_matches(pol: RegistryPol, items: list[ListItem]) -> bool:
    return bool(items) and all(_spec_matches(pol, i.key, i.value_name, i.spec) for i in items)


def _write_spec(pol: RegistryPol, key: str, vn: str, spec: ValueSpec) -> None:
    if spec.kind == "decimal":
        pol.set(key, vn, REG_DWORD, int(spec.value))
    elif spec.kind == "longDecimal":
        pol.set(key, vn, REG_QWORD, int(spec.value))
    elif spec.kind == "string":
        pol.set(key, vn, REG_SZ, str(spec.value))
    elif spec.kind == "delete":
        pol.mark_deleted(key, vn)


def _write_list(pol: RegistryPol, items: list[ListItem]) -> None:
    for i in items:
        _write_spec(pol, i.key, i.value_name, i.spec)


def _real_values(pol: RegistryPol, key: str) -> list:
    return [e for e in pol.values_under(key) if not e.is_delete_marker]


def get_state(p: Policy, pol: RegistryPol) -> str:
    en = dis = ambiguous = 0
    if p.value_name:
        if p.enabled_value:
            en += _spec_matches(pol, p.key, p.value_name, p.enabled_value)
        else:
            e = pol.find(p.key, p.value_name)
            if e is not None and e.type == REG_DWORD and e.decoded() == 1:
                en += 1
        if p.disabled_value:
            dis += _spec_matches(pol, p.key, p.value_name, p.disabled_value)
        else:
            e = pol.find(p.key, p.value_name)
            if pol.is_deleted(p.key, p.value_name) or (e is not None and e.type == REG_DWORD and e.decoded() == 0 and not p.enabled_value):
                dis += 1
    if _list_matches(pol, p.enabled_list):
        en += 1
    if _list_matches(pol, p.disabled_list):
        dis += 1
    elif not p.disabled_list and p.enabled_list and all(pol.is_deleted(i.key, i.value_name) for i in p.enabled_list):
        dis += 1
    for el in p.elements:
        if el.type == "list":
            if _real_values(pol, el.key):
                en += 1
            elif pol.has_delvals(el.key):
                dis += 1
            continue
        if not el.value_name:
            if el.type == "boolean" and (_list_matches(pol, el.true_list) or _list_matches(pol, el.false_list)):
                en += 1
            continue
        if pol.find(el.key, el.value_name) is not None:
            en += 1
        elif pol.is_deleted(el.key, el.value_name):
            # an enum may legitimately choose "delete": ambiguous, so neutral
            if el.type == "enum" and any(spec.kind == "delete" for _, spec, _ in el.items):
                ambiguous += 1
            else:
                dis += 1
    if en == 0 and dis == 0:
        # only "deleted" markers that an enum could also produce: same effect as Disabled
        return STATE_DISABLED if ambiguous else STATE_NOT_CONFIGURED
    return STATE_ENABLED if en >= dis else STATE_DISABLED


def get_element_values(p: Policy, pol: RegistryPol) -> dict:
    out: dict = {}
    for el in p.elements:
        val = None
        e = pol.find(el.key, el.value_name) if el.value_name else None
        dv = None
        if e is not None:
            try:
                dv = e.decoded()
            except Exception:
                dv = None
        if el.type == "boolean":
            if el.true_value and el.value_name:
                val = _spec_matches(pol, el.key, el.value_name, el.true_value)
            elif el.true_list and not el.value_name:
                val = _list_matches(pol, el.true_list)
            elif dv is not None:
                val = dv == 1 or dv == "1"
        elif el.type in ("decimal", "longDecimal"):
            if dv is not None:
                try:
                    val = int(dv)
                except (TypeError, ValueError):
                    val = None
        elif el.type == "text":
            val = dv if isinstance(dv, str) else None
        elif el.type == "multiText":
            val = dv if isinstance(dv, list) else ([dv] if isinstance(dv, str) else None)
        elif el.type == "enum":
            for idx, (_, spec, _vl) in enumerate(el.items):
                if el.value_name and _spec_matches(pol, el.key, el.value_name, spec):
                    val = idx
                    break
        elif el.type == "list":
            entries = _real_values(pol, el.key)
            if _bool(el.attrs.get("explicitValue")):
                val = [[x.value, _as_str(x)] for x in entries]
            elif "valuePrefix" in el.attrs:
                prefix = el.attrs["valuePrefix"]

                def order(x):
                    suffix = x.value[len(prefix):]
                    return int(suffix) if suffix.isdigit() else 1 << 30

                val = [_as_str(x) for x in sorted(entries, key=order)]
            else:
                val = [_as_str(x) for x in entries]
        out[el.id] = val
    return out


def _as_str(e) -> str:
    try:
        v = e.decoded()
    except Exception:
        return ""
    return v if isinstance(v, str) else str(v)


def _clear(p: Policy, pol: RegistryPol) -> None:
    if p.value_name:
        pol.remove(p.key, p.value_name)
    for i in p.enabled_list + p.disabled_list:
        pol.remove(i.key, i.value_name)
    for el in p.elements:
        if el.type == "list":
            pol.remove_key(el.key)
            continue
        if el.value_name:
            pol.remove(el.key, el.value_name)
        for i in el.true_list + el.false_list:
            pol.remove(i.key, i.value_name)
        for _, _, vl in el.items:
            for i in vl:
                pol.remove(i.key, i.value_name)


def set_state(p: Policy, pol: RegistryPol, state: str, values: Optional[dict] = None) -> None:
    """Apply state (+ element values when Enabled) to pol in place."""
    values = values or {}
    _clear(p, pol)
    if state == STATE_NOT_CONFIGURED:
        pol.sort()
        return
    if state == STATE_ENABLED:
        if p.value_name:
            _write_spec(pol, p.key, p.value_name, p.enabled_value or ValueSpec("decimal", 1))
        _write_list(pol, p.enabled_list)
        for el in p.elements:
            _write_element(el, pol, values.get(el.id), p)
    elif state == STATE_DISABLED:
        if p.value_name:
            if p.disabled_value:
                _write_spec(pol, p.key, p.value_name, p.disabled_value)
            else:
                pol.mark_deleted(p.key, p.value_name)
        if p.disabled_list:
            _write_list(pol, p.disabled_list)
        else:
            for i in p.enabled_list:
                pol.mark_deleted(i.key, i.value_name)
        for el in p.elements:
            if el.type == "list":
                pol.mark_delvals(el.key)
            elif el.value_name:
                pol.mark_deleted(el.key, el.value_name)
    else:
        raise PolicyValueError(f"unknown state {state}")
    pol.sort()


def _pres_label(p: Policy, el: Element) -> str:
    for c in p.presentation:
        if c.get("refId") == el.id and c.get("label"):
            return c["label"]
    return el.id


def _write_element(el: Element, pol: RegistryPol, v, p: Policy) -> None:
    label = _pres_label(p, el)
    required = _bool(el.attrs.get("required"))
    if el.type == "boolean":
        checked = bool(v)
        if checked:
            if el.value_name:
                _write_spec(pol, el.key, el.value_name, el.true_value or ValueSpec("decimal", 1))
            _write_list(pol, el.true_list)
        else:
            if el.value_name:
                _write_spec(pol, el.key, el.value_name, el.false_value or ValueSpec("decimal", 0))
            _write_list(pol, el.false_list)
    elif el.type in ("decimal", "longDecimal"):
        if v is None or v == "":
            if required:
                raise PolicyValueError(f"'{label}' is required")
            return
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise PolicyValueError(f"'{label}' must be a number")
        lo = _num(el.attrs.get("minValue"), 0)
        hi = _num(el.attrs.get("maxValue"), 9999 if el.type == "decimal" else 2**64 - 1)
        if not lo <= n <= hi:
            raise PolicyValueError(f"'{label}' must be between {lo} and {hi}")
        if _bool(el.attrs.get("storeAsText")):
            pol.set(el.key, el.value_name, REG_SZ, str(n))
        else:
            pol.set(el.key, el.value_name, REG_DWORD if el.type == "decimal" else REG_QWORD, n)
    elif el.type == "text":
        s = "" if v is None else str(v)
        if not s and required:
            raise PolicyValueError(f"'{label}' is required")
        maxlen = _num(el.attrs.get("maxLength"), 1023)
        if len(s) > maxlen:
            raise PolicyValueError(f"'{label}' is longer than {maxlen} characters")
        pol.set(el.key, el.value_name, REG_EXPAND_SZ if _bool(el.attrs.get("expandable")) else REG_SZ, s)
    elif el.type == "multiText":
        lines = v if isinstance(v, list) else [x for x in str(v or "").splitlines()]
        lines = [x for x in lines if x != ""]
        if not lines and required:
            raise PolicyValueError(f"'{label}' is required")
        pol.set(el.key, el.value_name, REG_MULTI_SZ, lines)
    elif el.type == "enum":
        if v is None or v == "":
            if required:
                raise PolicyValueError(f"'{label}' is required")
            return
        idx = int(v)
        if not 0 <= idx < len(el.items):
            raise PolicyValueError(f"'{label}': invalid choice")
        _, spec, vl = el.items[idx]
        if el.value_name:
            _write_spec(pol, el.key, el.value_name, spec)
        _write_list(pol, vl)
    elif el.type == "list":
        items = v or []
        if not items and required:
            raise PolicyValueError(f"'{label}' needs at least one entry")
        rtype = REG_EXPAND_SZ if _bool(el.attrs.get("expandable")) else REG_SZ
        if not _bool(el.attrs.get("additive")):
            pol.mark_delvals(el.key)
        if _bool(el.attrs.get("explicitValue")):
            for pair in items:
                name, data = (pair[0], pair[1]) if isinstance(pair, (list, tuple)) else (pair, "")
                if not str(name):
                    continue
                pol.set(el.key, str(name), rtype, str(data))
        elif "valuePrefix" in el.attrs:
            prefix = el.attrs["valuePrefix"]
            for i, item in enumerate([x for x in items if str(x) != ""], start=1):
                pol.set(el.key, f"{prefix}{i}", rtype, str(item))
        else:
            for item in items:
                if str(item) != "":
                    pol.set(el.key, str(item), rtype, str(item))


# ---------------------------------------------------------------------------
# JSON helpers for the API


def policy_summary(p: Policy, pol: Optional[RegistryPol]) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "display": p.display,
        "state": get_state(p, pol) if pol is not None else STATE_NOT_CONFIGURED,
        "supported": p.supported,
        "cls": p.cls,
    }


def policy_detail(p: Policy, pol: RegistryPol, admx: AdmxSet) -> dict:
    els = {}
    for el in p.elements:
        d = {"type": el.type, "id": el.id, "key": el.key, "valueName": el.value_name, **{
            k: el.attrs[k] for k in ("required", "minValue", "maxValue", "maxLength", "explicitValue",
                                     "valuePrefix", "additive", "expandable", "storeAsText") if k in el.attrs
        }}
        if el.type == "enum":
            d["items"] = [name for name, _, _ in el.items]
        els[el.id] = d
    return {
        **policy_summary(p, pol),
        "explain": p.explain,
        "key": p.key,
        "valueName": p.value_name,
        "file": p.file,
        "path": admx.category_path(p.category),
        "elements": els,
        "presentation": p.presentation,
        "values": get_element_values(p, pol),
    }


def registry_touched(p: Policy) -> list[tuple[str, Optional[str]]]:
    """(key, valueName) pairs a policy owns; valueName None means the whole key (lists)."""
    out: list[tuple[str, Optional[str]]] = []
    if p.value_name:
        out.append((p.key, p.value_name))
    for i in p.enabled_list + p.disabled_list:
        out.append((i.key, i.value_name))
    for el in p.elements:
        if el.type == "list":
            out.append((el.key, None))
            continue
        if el.value_name:
            out.append((el.key, el.value_name))
        for i in el.true_list + el.false_list:
            out.append((i.key, i.value_name))
        for _, _, vl in el.items:
            for i in vl:
                out.append((i.key, i.value_name))
    return out
