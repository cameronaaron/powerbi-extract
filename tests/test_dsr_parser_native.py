import importlib
import sys

import orjson
import pytest

import powerbi_extract.dsr_parser as dsr_parser
from powerbi_extract.dsr_parser import parse_powerbi_dsr, parse_powerbi_dsr_native
from tests.test_dsr_parser import _wrap


def _payloads():
    return [
        _wrap({
            "ValueDicts": {"D0": ["Alice", "Bob"]},
            "PH": [{"DM0": [
                {"S": [{"N": "G0", "DN": "D0"}, {"N": "G1"}], "C": [0, 85]},
                {"C": [1, 92]},
            ]}],
        }),
        _wrap({
            "ValueDicts": {"D0": ["Alice", "Bob"]},
            "PH": [{"DM0": [
                {"S": [{"N": "G0", "DN": "D0"}, {"N": "G1"}], "C": [1, 92]},
                {"R": 1, "C": [78]},
            ]}],
        }),
        _wrap({
            "ValueDicts": {},
            "PH": [{"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "Ø": 2, "C": ["Carol"]}]}],
        }),
        _wrap({
            "ValueDicts": {},
            "RT": {"some": "token"},
            "PH": [{"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "C": ["Dan", 10]}]}],
        }),
        _wrap({"ValueDicts": {}, "PH": []}),
        {"results": []},
    ]


@pytest.mark.skipif(dsr_parser._native is None, reason="native extension not built")
@pytest.mark.parametrize("payload", _payloads())
def test_native_matches_pure_python(payload):
    python_rows, python_restart = parse_powerbi_dsr(payload)
    native_rows, native_restart = parse_powerbi_dsr_native(payload)

    assert native_rows == python_rows
    assert native_restart == python_restart


def test_parse_powerbi_dsr_native_raises_without_the_extension(monkeypatch):
    monkeypatch.setattr(dsr_parser, "_native", None)

    with pytest.raises(RuntimeError):
        parse_powerbi_dsr_native({"results": []})


def test_module_import_falls_back_when_native_extension_missing(monkeypatch):
    import powerbi_extract

    monkeypatch.setitem(sys.modules, "powerbi_extract._native", None)
    monkeypatch.delattr(powerbi_extract, "_native", raising=False)
    try:
        reloaded = importlib.reload(dsr_parser)
        assert reloaded._native is None
    finally:
        importlib.reload(dsr_parser)
