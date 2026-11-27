import importlib.util
import json
from pathlib import Path

import pytest

from bambu_mcp.printer.hms import HmsTexts, describe, hms_code, severity
from bambu_mcp.printer.state import Hms

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "extract_hms.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("extract_hms", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_code_format():
    assert hms_code(Hms(attr=50331904, code=65543)) == "0300_0100_0001_0007"


def test_severity_levels():
    attr = 0x03000100
    assert severity(Hms(attr, 0x00010007)) == "fatal"
    assert severity(Hms(attr, 0x00020001)) == "serious"
    assert severity(Hms(attr, 0x00030001)) == "common"
    assert severity(Hms(attr, 0x00040001)) == "info"
    assert severity(Hms(attr, 0x00090001)) == "unknown"


def test_describe_without_texts():
    text = describe(Hms(50331904, 65543), HmsTexts.load(None))
    assert text == "HMS_0300_0100_0001_0007 (fatal): no description"


def test_describe_with_texts(tmp_path):
    path = tmp_path / "hms.json"
    path.write_text(
        json.dumps({"hms": {"0300_0100_0001_0007": "The heatbed temperature is abnormal"}}),
        encoding="utf-8",
    )
    text = describe(Hms(50331904, 65543), HmsTexts.load(path))
    assert "The heatbed temperature is abnormal" in text


def test_load_missing_file_gives_empty_texts(tmp_path):
    texts = HmsTexts.load(tmp_path / "absent.json")
    assert texts.text("0300_0100_0001_0007") is None


def test_error_text(tmp_path):
    path = tmp_path / "hms.json"
    path.write_text(json.dumps({"hms": {}, "errors": {"0500C010": "SD card error"}}), encoding="utf-8")
    texts = HmsTexts.load(path)
    assert texts.error_text(0x0500C010) == "SD card error"
    assert texts.error_text(0x0500C011) is None


def _studio_file(path: Path, intro: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "data": {
                    "device_hms": {
                        "en": [
                            {"ecode": "0300010000010007", "intro": intro},
                            {"ecode": "0300010000010008", "intro": ""},
                        ]
                    },
                    "device_error": {
                        "en": [
                            {"ecode": "0500C010", "intro": "SD card error"},
                            {"ecode": "0500C011", "intro": ""},
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_extractor_roundtrip(tmp_path):
    src = _studio_file(tmp_path / "studio" / "hms" / "hms_en_22E.json", "Heatbed abnormal")
    out = tmp_path / "out.json"
    assert _load_script().main(["-o", str(out), str(tmp_path / "studio")]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "hms": {"0300_0100_0001_0007": "Heatbed abnormal"},
        "errors": {"0500C010": "SD card error"},
    }
    texts = HmsTexts.load(out)
    assert "Heatbed abnormal" in describe(Hms(0x03000100, 0x00010007), texts)
    assert src.exists()


def test_extractor_prefers_first_source_and_accepts_file(tmp_path):
    first = _studio_file(tmp_path / "a" / "hms" / "hms_en_22E.json", "From first")
    _studio_file(tmp_path / "b" / "resources" / "hms" / "hms_en_22E.json", "From second")
    module = _load_script()
    assert module.find_source([tmp_path / "a", tmp_path / "b"], "22E") == first
    assert module.find_source([first], "22E") == first
    with pytest.raises(FileNotFoundError):
        module.find_source([tmp_path / "b"], "ZZZ")
