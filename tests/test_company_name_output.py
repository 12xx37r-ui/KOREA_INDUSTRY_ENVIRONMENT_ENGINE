from __future__ import annotations

import io
import zipfile
from pathlib import Path

from kiee import dart_earnings_collector as dart
from kiee.engine import _company_name_map, _company_rows
from kiee.util import read_json, utc_now_iso, write_json


def _corp_zip(rows: list[tuple[str, str, str]]) -> bytes:
    xml = ["<result>"]
    for corp_code, corp_name, stock_code in rows:
        xml.append(
            "<list>"
            f"<corp_code>{corp_code}</corp_code>"
            f"<corp_name>{corp_name}</corp_name>"
            f"<stock_code>{stock_code}</stock_code>"
            "</list>"
        )
    xml.append("</result>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("CORPCODE.xml", "".join(xml).encode("utf-8"))
    return buf.getvalue()


def test_legacy_corpcode_cache_without_names_is_refreshed(tmp_path, monkeypatch):
    cache = tmp_path / "input_cache" / "dart_corpcode_map.json"
    write_json(cache, {
        "fetched_at": utc_now_iso(),
        "map": {"010120": "00123456"},
        # legacy cache intentionally has no names
    })

    calls = {"n": 0}
    payload = _corp_zip([
        ("00123456", "엘에스일렉트릭", "010120"),
        ("00999999", "효성중공업", "298040"),
    ])

    def fake_get_bytes(endpoint, params, api_key, timeout=60):
        calls["n"] += 1
        assert endpoint == "corpCode.xml"
        return payload

    monkeypatch.setattr(dart, "_get_bytes", fake_get_bytes)
    counter = [0]
    mapping = dart._load_corpcode_map(tmp_path, "dummy-key", counter)

    assert mapping["010120"] == "00123456"
    assert mapping["298040"] == "00999999"
    assert calls["n"] == 1
    assert counter[0] == 1

    saved = read_json(cache, {})
    assert saved["names"]["010120"] == "엘에스일렉트릭"
    assert saved["names"]["298040"] == "효성중공업"


def test_complete_fresh_cache_does_not_add_network_call(tmp_path, monkeypatch):
    cache = tmp_path / "input_cache" / "dart_corpcode_map.json"
    write_json(cache, {
        "fetched_at": utc_now_iso(),
        "map": {"010120": "00123456", "298040": "00999999"},
        "names": {"010120": "엘에스일렉트릭", "298040": "효성중공업"},
    })

    def should_not_call(*args, **kwargs):
        raise AssertionError("complete fresh cache must not call corpCode ZIP")

    monkeypatch.setattr(dart, "_get_bytes", should_not_call)
    counter = [0]
    mapping = dart._load_corpcode_map(tmp_path, "dummy-key", counter)
    assert mapping["010120"] == "00123456"
    assert counter[0] == 0


def test_engine_company_rows_use_name_and_never_ticker_as_name(tmp_path):
    write_json(tmp_path / "input_cache" / "dart_corpcode_map.json", {
        "fetched_at": utc_now_iso(),
        "map": {"010120": "00123456"},
        "names": {"010120": "엘에스일렉트릭"},
    })
    names = _company_name_map(tmp_path)

    rows = _company_rows(
        {"krx_basket": ["010120", "999999"]},
        names,
    )
    assert rows[0]["ticker"] == "010120"
    assert rows[0]["name"] == "엘에스일렉트릭"
    assert rows[1]["ticker"] == "999999"
    assert rows[1]["name"] == ""
    assert all(r["name"] != r["ticker"] for r in rows)


def test_company_name_map_reads_cached_names_without_network(tmp_path):
    write_json(tmp_path / "input_cache" / "dart_corpcode_map.json", {
        "fetched_at": utc_now_iso(),
        "map": {"010120": "00123456", "298040": "00999999"},
        "names": {"010120": "엘에스일렉트릭", "298040": "효성중공업"},
    })
    assert _company_name_map(tmp_path) == {
        "010120": "엘에스일렉트릭",
        "298040": "효성중공업",
    }
