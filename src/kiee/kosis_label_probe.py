"""
kosis_label_probe.py
DT_1F32001(가동률), DT_1F02011(생산지수) 테이블의 실제 C1_NM 라벨을
모두 출력해서 keyword 매핑을 정밀화할 수 있게 한다.

실행: PYTHONPATH=src python -m kiee.kosis_label_probe --root .
출력: input/kosis_labels_raw.json
"""
from __future__ import annotations

import json, os
from pathlib import Path
from .util import utc_now_iso, write_json
from .industry_kosis_collector import _get_data_json, _CallBudget

TABLES = [
    ("101", "DT_1F32001"),   # 제조업 가동률
    ("101", "DT_1F02011"),   # 광공업 생산·출하·재고
]

def probe(api_key: str, org_id: str, table_id: str, budget: _CallBudget) -> list[str]:
    params = {
        "method": "getList", "apiKey": api_key,
        "orgId": org_id, "tblId": table_id,
        "prdSe": "M", "newEstPrdCnt": 1,
        "itmId": "ALL", "objL1": "ALL",
        "format": "json", "jsonVD": "Y",
    }
    rows = _get_data_json(params, budget)
    labels = []
    seen = set()
    for row in rows:
        for key in ("C1_NM", "c1_nm", "PRD_DE", "ITM_NM"):
            v = str(row.get(key) or "").strip()
            if v and v not in seen:
                seen.add(v)
                labels.append(v)
    return sorted(labels)

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    api_key = os.getenv("KOSIS_API_KEY", "").strip()
    if not api_key:
        print("KOSIS_KEY_CONFIGURED=false"); return 0
    print("KOSIS_KEY_CONFIGURED=true")
    budget = _CallBudget(20, errors=[], events=[])
    result = {"generated_at_utc": utc_now_iso(), "tables": {}}
    for org_id, table_id in TABLES:
        try:
            labels = probe(api_key, org_id, table_id, budget)
            result["tables"][table_id] = {"org_id": org_id, "label_count": len(labels), "labels": labels}
            print(f"{table_id}: {len(labels)} labels")
            for lb in labels[:30]:
                print(f"  {lb}")
        except Exception as e:
            result["tables"][table_id] = {"error": str(e)}
            print(f"{table_id}: ERROR {e}")
    root = Path(args.root).resolve()
    write_json(root / "input" / "kosis_labels_raw.json", result)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
