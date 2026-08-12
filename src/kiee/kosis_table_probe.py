from __future__ import annotations
import argparse, json, os, urllib.parse, urllib.request
from typing import Any

META_URL = "https://kosis.kr/openapi/statisticsData.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

def _get(url, params):
    q = urllib.parse.urlencode({k:v for k,v in params.items() if v not in (None,"")})
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent":"kiee-kosis-probe/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def _rows(p):
    if isinstance(p, list): return [r for r in p if isinstance(r,dict)]
    if isinstance(p, dict):
        if p.get("err"): return []
        for k in ("result","data","rows"):
            if isinstance(p.get(k),list): return [r for r in p[k] if isinstance(r,dict)]
    return []

def probe(table_id, api_key, org_id="101"):
    print(f"\n{'='*60}\nTABLE={table_id}")
    # 메타(ITM) - 항목 목록
    try:
        rows = _rows(_get(META_URL, {"method":"getMeta","type":"ITM","apiKey":api_key,
                                     "orgId":org_id,"tblId":table_id,"format":"json","jsonVD":"Y"}))
        print(f"META_ITM rows={len(rows)}")
        for r in rows[:4]:
            print(" ", {k:v for k,v in r.items() if v not in (None,"","0") and k in ("ITM_ID","ITM_NM","ITM_NM_ENG","OBJ_ID")})
    except Exception as e:
        print(f"META_ERR: {e}")
    # 실제 데이터 - C1_NM 레이블 (산업별인지 확인)
    for extra in [{}]:
        try:
            params = {"method":"getList","apiKey":api_key,"orgId":org_id,"tblId":table_id,
                      "prdSe":"M","newEstPrdCnt":1,"itmId":"ALL","objL1":"ALL","format":"json","jsonVD":"Y"}
            params.update(extra)
            rows = _rows(_get(DATA_URL, params))
            if not rows:
                try:
                    raw = _get(DATA_URL, params)
                    if isinstance(raw,dict) and raw.get("err"):
                        print(f"DATA_ERR: {raw.get('err')} {raw.get('errMsg','')}")
                        continue
                except: pass
                print("DATA rows=0")
                continue
            labels = sorted(set(str(r.get("C1_NM") or "").strip() for r in rows if r.get("C1_NM")))
            itms  = sorted(set(str(r.get("ITM_NM") or "").strip() for r in rows if r.get("ITM_NM")))
            print(f"DATA rows={len(rows)}")
            print(f"  C1_NM unique({len(labels)}): {labels[:30]}")
            print(f"  ITM_NM unique({len(itms)}): {itms[:10]}")
        except Exception as e:
            print(f"DATA_ERR: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", nargs="+",
        default=["DT_1F02011","DT_1F02031","DT_512Y019","DT_1JH20201"])
    args = parser.parse_args()
    api_key = os.getenv("KOSIS_API_KEY","").strip()
    if not api_key:
        print("KOSIS_KEY_CONFIGURED=false"); return 0
    print("KOSIS_KEY_CONFIGURED=true")
    for t in args.tables:
        probe(t, api_key)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
