from __future__ import annotations
import argparse, json, os, urllib.parse, urllib.request
from typing import Any

META_URL = "https://kosis.kr/openapi/statisticsData.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

def _get(url, params, timeout=10):
    q = urllib.parse.urlencode({k:v for k,v in params.items() if v not in (None,"")})
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent":"kiee-kosis-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _rows(p):
    if isinstance(p, list): return [r for r in p if isinstance(r, dict)]
    if isinstance(p, dict):
        if p.get("err"): return []
        for k in ("result","data","rows"):
            if isinstance(p.get(k),list): return [r for r in p[k] if isinstance(r,dict)]
    return []

def probe(table_id, api_key, org_id="101"):
    print(f"\n{'='*50}\nTABLE={table_id} (org={org_id})")
    try:
        rows = _rows(_get(META_URL, {"method":"getMeta","type":"ITM","apiKey":api_key,
                                     "orgId":org_id,"tblId":table_id,"format":"json","jsonVD":"Y"}))
        print(f"META rows={len(rows)}")
        for r in rows[:3]:
            print(" ", {k:v for k,v in r.items() if k in ("ITM_ID","ITM_NM","ITM_NM_ENG") and v})
    except Exception as e:
        print(f"META_SKIP: {type(e).__name__}: {e}")
    try:
        data = _get(DATA_URL, {"method":"getList","apiKey":api_key,"orgId":org_id,
                               "tblId":table_id,"prdSe":"M","newEstPrdCnt":2,
                               "itmId":"ALL","objL1":"ALL","format":"json","jsonVD":"Y"})
        rows = _rows(data)
        if rows:
            c1 = sorted(set(str(r.get("C1_NM","")).strip() for r in rows if r.get("C1_NM")))
            itm = sorted(set(str(r.get("ITM_NM","")).strip() for r in rows if r.get("ITM_NM")))
            print(f"DATA rows={len(rows)}")
            print(f"  C1_NM({len(c1)}): {c1[:20]}")
            print(f"  ITM_NM: {itm[:5]}")
        elif isinstance(data,dict) and data.get("err"):
            print(f"DATA_ERR {data['err']}: {data.get('errMsg','')}")
        else:
            print("DATA rows=0")
    except Exception as e:
        print(f"DATA_SKIP: {type(e).__name__}: {e}")

# 서비스업 KOSIS 테이블 후보
SERVICE_TABLES = [
    # 서비스업생산지수 (통계청 서비스업동향조사)
    "DT_1JH30001",  # 서비스업생산지수 업종별
    "DT_1JH30000",  # 서비스업 생산·판매 종합
    # 소매판매액지수 (산업활동동향)
    "DT_1K9A001",   # 소매판매액지수 업태별
    # 음식·숙박
    "DT_1JH20202",  # 음식점업 매출
    "DT_1JH10100",  # 숙박업 이용
    # 운수·창고
    "DT_1JH40101",  # 육상운송업 물동량
    "DT_1JH40201",  # 해운업
    # 금융보험
    "DT_1JH50001",  # 금융보험업 생산지수
    # 정보통신
    "DT_1JH70001",  # 정보통신업 생산지수
]

# 제조업 확인용 (기존 테이블 키워드 매핑 검증)
MFG_TABLES = [
    "DT_1F32001",   # 제조업 가동률지수 (현재 utilization 사용 중)
    "DT_1F02011",   # 광공업 생산·출하·재고 (현재 inventory_cycle 사용 중)
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", nargs="+", default=SERVICE_TABLES + MFG_TABLES)
    parser.add_argument("--service-only", action="store_true")
    parser.add_argument("--mfg-only", action="store_true")
    args = parser.parse_args()
    api_key = os.getenv("KOSIS_API_KEY","").strip()
    if not api_key:
        print("KOSIS_KEY_CONFIGURED=false"); return 0
    print("KOSIS_KEY_CONFIGURED=true")
    tables = args.tables
    if args.service_only:
        tables = SERVICE_TABLES
    elif args.mfg_only:
        tables = MFG_TABLES
    for t in tables:
        probe(t, api_key)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
