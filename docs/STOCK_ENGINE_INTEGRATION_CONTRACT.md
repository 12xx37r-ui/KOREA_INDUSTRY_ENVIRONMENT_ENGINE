# Stock Engine Integration Contract

## 목적

기존 종목엔진의 단순 산업전망 current/future 값을 `Korea Industry Environment Engine` 출력으로 교체할 때 사용한다.

## 읽을 파일

우선 `output/stock_prediction_bridge.json` 하나만 읽는다.

기업의 검증된 산업 profile key가 예를 들어 `shipbuilding`이면:

`by_profile_key.shipbuilding`

에서 다음 값을 사용한다.

- `current_score`
- `current_band`
- `forecast_3m_score`
- `forecast_3m_band`
- `delta_points`
- `direction`
- `quality_score`
- `bounded_direction_adjustment_points`
- `allowed_as_auxiliary`
- `allowed_as_primary`

산업명만 있는 경우 `alias_to_profile_key`를 이용해 key로 변환한다.

## 기존 산업전망 대체 규칙

1. 새 산업엔진 bridge가 존재하고 `quality_score >= 50`이면 화면의 현재/3M 산업환경은 새 엔진값을 우선한다.
2. bridge가 연결되지 않거나 품질 50 미만이면 기존 산업전망을 최종값으로 사용하지 말고 `산업환경 확인 중/자료부족`으로 표시하거나 LKG 정책을 적용한다.
3. 연결 대기값을 50으로 가장하지 않는다.
4. 개별종목 주가방향에는 `bounded_direction_adjustment_points`만 사용한다. `forecast_3m_score` 자체를 통째로 기존 주가예측 점수에 더하지 않는다.
5. `allowed_as_primary=false` 동안 산업전망 때문에 단독 매수/매도 방향을 결정하지 않는다.

## 호출 최소화 권장

종목마다 산업엔진 repo를 여러 번 읽지 않는다.

- stock engine 실행당 `stock_prediction_bridge.json` 1회
- 메모리/로컬 캐시 재사용
- 여러 종목 배치에서는 같은 bridge를 공용
- GAS는 가능하면 stock engine이 게시한 최종 종목 JSON의 산업환경 섹션을 읽기만 한다.
