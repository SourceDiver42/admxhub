import io
import zipfile

import pytest

ADMX = """<?xml version="1.0" encoding="utf-8"?>
<policyDefinitions xmlns="http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions" revision="1.0" schemaVersion="1.0">
  <policyNamespaces><target prefix="t" namespace="{ns}"/></policyNamespaces>
  <resources minRequiredRevision="1.0"/>
  <categories><category name="Root" displayName="$(string.Root)"/></categories>
  <policies>{policies}</policies>
</policyDefinitions>"""
POLICY = """<policy name="{name}" class="Machine" displayName="$(string.{name})" key="Software\\Policies\\Test" valueName="{name}"><parentCategory ref="Root"/></policy>"""
ADML = """<?xml version="1.0" encoding="utf-8"?>
<policyDefinitionResources xmlns="http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions" revision="1.0" schemaVersion="1.0">
  <displayName/><description/><resources><stringTable><string id="Root">Test Root</string>{strings}</stringTable></resources>
</policyDefinitionResources>"""


def make_zip(policies=("Alpha", "Beta"), ns="Test.Policies", langs=("en-US", "de-DE"), extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("windows/admx/test.admx", ADMX.format(ns=ns, policies="".join(POLICY.format(name=p) for p in policies)))
        for lang in langs:
            z.writestr(f"windows/admx/{lang}/test.adml", ADML.format(strings="".join(f'<string id="{p}">{p} policy</string>' for p in policies)))
        z.writestr("windows/examples/test.admx", "duplicate that must be ignored")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def zip_bytes():
    return make_zip()
