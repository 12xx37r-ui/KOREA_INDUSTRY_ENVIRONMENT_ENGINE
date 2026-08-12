from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from typing import Any

META_URL = "https://kosis.kr/openapi/statisticsData.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"


def _get(url: str, params: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kiee-kosis-probe/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("result", "data", "rows"):
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
    return []


def probe_table(table_id: str, api_key: str) -> None:
    print(f"\n{'='*60}")
    print(f"TABLE={table_id}")

    # 1) 메타(ITM) - 실제 필드명 전체 출력
    try:
        meta = _get(META_URL, {"method": "getMeta", "type": "ITM", "apiKey": api_key,
                               "orgId": "101", "tblId": table_id, "format": "json", "jsonVD": "Y"})
        rows = _rows(meta)
        print(f"META_ITM rows={len(rows)}")
        if rows:
            print("  first row keys:", list(rows[0].keys()))
            for r in rows[:5]:
                print(" ", {k: v for k, v in r.items() if v not in (None, "", "0")})
    except Exception as e:
        print(f"META_ITM ERROR: {e}")

    # 2) 실제 데이터 - C1_NM 레이블 확인 (가장 중요)
    try:
        data = _get(DATA_URL, {"method": "getList", "apiKey": api_key, "orgId": "101",
                               "tblId": table_id, "prdSe": "M", "newEstPrdCnt": 1,
                               "itmId": "ALL", "objL1": "ALL", "format": "json", "jsonVD": "Y"})
        drows = _rows(data)
        print(f"DATA rows={len(drows)}")
        if drows:
            print("  data row keys:", list(drows[0].keys()))
            # C1_NM 레이블 유니크 목록 - 키워드 매핑에 필요한 핵심 정보
            labels = sorted(set(str(r.get("C1_NM") or r.get("C1") or "").strip() for r in drows if r.get("C1_NM") or r.get("C1")))
            print(f"  C1_NM unique({len(labels)}):", labels[:30])
            labels2 = sorted(set(str(r.get("C2_NM") or "").strip() for r in drows if r.get("C2_NM")))
            if labels2:
                print(f"  C2_NM unique({len(labels2)}):", labels2[:20])
        elif isinstance(data, dict) and data.get("err"):
            print(f"  DATA_ERR: {data.get('err')} {data.get('errMsg','')}")
    except Exception as e:
        print(f"DATA ERROR: {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", nargs="+",
                        default=["DT_1F02001", "DT_1F02004", "DT_1F02013", "DT_512Y007", "DT_115100001"])
    args = parser.parse_args()
    api_key = os.getenv("KOSIS_API_KEY", "").strip()
    if not api_key:
        print("KOSIS_KEY_CONFIGURED=false")
        return 0
    print("KOSIS_KEY_CONFIGURED=true")
    for table_id in args.tables:
        probe_table(table_id, api_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
