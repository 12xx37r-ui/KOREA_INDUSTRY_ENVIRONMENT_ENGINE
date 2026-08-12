from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

META_URL = "https://kosis.kr/openapi/statisticsData.do"


def _json(table_id: str, api_key: str) -> Any:
    query = urllib.parse.urlencode({
        "method": "getMeta", "type": "ITM", "apiKey": api_key,
        "orgId": "101", "tblId": table_id, "format": "json", "jsonVD": "Y",
    })
    request = urllib.request.Request(f"{META_URL}?{query}", headers={"User-Agent": "kiee-kosis-probe/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("objId", "objNm", "itmId", "itmNm", "objIdSn", "upItmId", "unitNm")
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Print KOSIS table classifier/item metadata without exposing the API key")
    parser.add_argument("--tables", nargs="+", default=["DT_1F02001", "DT_512Y007", "DT_1F02013"])
    args = parser.parse_args()
    api_key = os.getenv("KOSIS_API_KEY", "").strip()
    if not api_key:
        print("KOSIS_KEY_CONFIGURED=false")
        return 0
    print("KOSIS_KEY_CONFIGURED=true")
    for table_id in args.tables:
        try:
            payload = _json(table_id, api_key)
            rows = payload if isinstance(payload, list) else []
            compact = [_compact(row) for row in rows if isinstance(row, dict)]
            print(f"TABLE={table_id} META_ROWS={len(compact)}")
            print(json.dumps(compact[:200], ensure_ascii=False))
        except Exception as exc:
            print(f"TABLE={table_id} META_ERROR={type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
