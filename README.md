# Korea Industry Environment Engine V1.0.0

한국 상장기업의 **현재 산업 유불리(0~100)**와 **향후 약 3개월 산업환경 전망(0~100)**을 산업별로 사전 계산하는 독립 Python 엔진입니다.

## 목적

기존 종목엔진의 단순 산업전망을 대체할 수 있는 공통 산업 레이어를 제공합니다. 기업을 조회할 때 API를 다시 여러 번 호출하지 않고, 이 엔진이 미리 만든 JSON을 읽어 현재 산업환경·3개월 전망·주가방향 보조 오버레이를 연결하는 구조입니다.

## 6축 모델

1. 산업 실적 모멘텀
2. 수요·경기
3. 가격·마진
4. 금융환경
5. 증시 내부환경
6. 밸류에이션

산업마다 현재/3개월 가중치와 금리·환율·유동성·원가·경기 민감도를 별도로 정의합니다. 동일 공식을 모든 업종에 무차별 적용하지 않습니다.

## 입력: 기존 엔진을 읽기 전용으로 재사용

정상 실행당 upstream JSON은 **각각 1회, 총 4회 이하**로 읽고 실행 메모리에서 재사용합니다.

- `korea-rate-fx-engine/output/korea_rate_fx_outlook_v3.json`
- `korea-rate-fx-engine/output/korea_equity_environment.json`
- `global-macro-data-collector/public/data/cards_8_12_bundle.json`
- `industry-boom-leading-engine/outputs/v70_final_engine/v70_current_operational_snapshot.json`

토큰이 있으면 GitHub Contents API를 파일당 한 번 사용하고, 없으면 public raw를 파일당 한 번 사용합니다. 실패 시 디스크 LKG 캐시를 제한된 기간만 재사용합니다.

## KRX 직접 산업자료

`pykrx`의 시장 전체 표를 KOSPI/KOSDAQ별로 한 번씩 수집한 뒤 25개 산업을 로컬 필터링합니다.

정상 목표 호출량:

- 35일 가격변화: 2회
- 외국인/기관 수급: 4회
- PER/PBR: 2회
- 합계: **8회/실행**
- 기업 검색당 live call: **0회**

직접 데이터가 일시 실패하면 마지막 정상 KRX 스냅샷을 재사용하며, 마지막 정상값도 없으면 해당 축 품질을 낮추고 점수를 50 중립 방향으로 수축합니다. 연결 대기 데이터를 0점으로 위조하지 않습니다.

## 산업붐 V7 신호 처리

제공된 V70 스냅샷이 `investment_use_allowed=false`인 동안 해당 테마 신호를 투자신호처럼 전량 사용하지 않습니다. 중립 방향으로 축소하고 품질상한을 적용하며, 산업별 `theme_relevance`로 한 번 더 제한해 **좁은 테마가 전체 산업을 대표하지 못하도록 한 선행 보조자료**로만 사용합니다.

## 3개월 전망과 주가예측 연결

3개월 점수는 현재점수를 단순 연장하지 않습니다. 검증된 글로벌 3개월 주식·원가·수요 전망과 한국 금리·환율·유동성·원화강도 전망, 산업 테마의 선행신호를 별도 가중합니다.

`output/stock_prediction_bridge.json`은 기존 종목예측 엔진이 읽을 수 있는 profile-key별 보조값을 제공합니다. 자체 prospective OOS가 통과하기 전에는 개별종목 방향점수에 최대 ±5점만 허용하고 primary signal로는 사용하지 않습니다.

## Prospective OOS

동일 산업의 매일 겹치는 전망을 표본으로 부풀리지 않기 위해 **산업당 월 1개 전망만 등록**합니다. 75일 이상 지난 뒤 당시와 현재에 모두 존재하는 동일 바스켓 종목의 등가중 수익률로 평가합니다.

기본 통과조건:

- 평가 24건 이상
- 방향적중률 55% 이상
- 긍정신호군과 부정신호군 실제 3개월 수익률 차이 2%p 이상

통과 전에는 산업전망의 개별종목 주가방향 영향도를 제한합니다.

## 출력

- `output/industry_environment_latest.json` : 전체 산업 현재/3M 점수와 근거
- `output/industries/<industry>.json` : 산업별 상세
- `output/industry_mapping.json` : 산업 key/alias 연결
- `output/stock_prediction_bridge.json` : 종목엔진 연결용 compact bridge
- `output/engine_health.json` : 연결상태·호출량·검증상태
- `output/industry_environment_history.json` : 일별 점수 역사
- `output/validation/forecast_registry.json` : prospective forecast registry
- `output/validation/prospective_validation.json` : OOS 성적

## 로컬 검증

```bash
python -m compileall -q src tests
pytest -q
PYTHONPATH=src python -m kiee.cli --root . --fixture-dir fixtures/upstream --no-live-krx
```

실전 실행:

```bash
PYTHONPATH=src python -m kiee.cli --root .
```

## 데이터 한계

- 유료 증권사 업종 EPS 컨센서스 revision 데이터는 사용하지 않습니다. 이를 보유하지 않으면서 `컨센서스`라고 표시하지 않습니다.
- 현재 earnings fallback은 제공된 한국증시 엔진의 후행 EPS 대용치이고 품질을 낮춰 사용합니다.
- 현재 KRX valuation은 대표바스켓의 **횡단면 상대 PER/PBR**입니다. 장기 업종 역사백분위와 동일하지 않습니다.
- 산업전망은 확률적 환경점수이며 개별종목 주가 상승/하락을 보장하지 않습니다.
