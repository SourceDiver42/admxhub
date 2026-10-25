import json

from admxhub.cli import main


def test_version_and_validate(capsys):
    assert main(["validate"]) == 0
    assert "valid" in capsys.readouterr().out


def test_sources_json_anywhere(capsys):
    assert main(["sources", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(s["id"] == "edge" for s in data)


def test_unknown_product(capsys):
    assert main(["check", "nope"]) == 1
    assert "unknown product" in capsys.readouterr().err
