# Model Specification — Korea Industry Environment Engine V1.0.0

## 1. 핵심 원칙

- 현재 산업환경과 3개월 전망은 별도 가중치를 사용한다.
- 산업마다 민감도와 가중치를 다르게 사용한다.
- 연결되지 않은 값은 0점으로 넣지 않는다.
- 품질이 낮은 입력은 `base_weight × quality`로 자동 축소한다.
- 전체 품질가중 coverage가 낮으면 최종점수를 50(중립) 쪽으로 수축한다.
- 산업붐 V70은 `investment_use_allowed=false` 동안 선행 보조자료로만 사용한다.
- 현재 수급을 3개월 미래까지 단순 연장하지 않는다.
- 산업 3개월 전망은 prospective OOS 통과 전 개별종목 주가방향의 primary signal이 아니다.

## 2. 6축

### A. 산업 실적 모멘텀

현재 제공 데이터에서 업종별 유료 애널리스트 EPS 컨센서스가 없으므로 두 계층을 사용한다.

1. 산업붐 V70이 연결된 산업: 실물 산업신호 + 상업화 + 투자단계 + source diffusion
2. 미연결 산업: 한국증시 직접환경의 후행 EPS 대용치를 큰 폭으로 축소해 사용

미연결 산업 값을 `EPS 컨센서스`라고 부르지 않는다.

### B. 수요·경기

- 산업붐 V70의 산업 선행신호
- Global Card 9 고용·소비 현재/3M
- Global Card 11 종합 경기환경
- 산업별 `consumer_cycle` 민감도

Card 9 3M 검증이 약하면 자동 품질축소된다.

### C. 가격·마진

- 산업붐 상업화/산업신호
- Global Card 10 원자재·에너지·공급비용 현재/3M
- 산업별 `cost_relief` 부호

원가 소비 산업과 원자재 생산 산업은 동일 방향으로 처리하지 않는다.

### D. 금융환경

- 한국 기준금리 현재/3M
- 국고채 3Y
- 원화강도 현재/3M
- 원화 유동성 현재/3M
- 신용스프레드 환경
- 산업별 금리/환율/유동성/신용 민감도

수출주는 원화약세 민감도가 상대적으로 높고, 건설/성장주는 금리 민감도를 높게 둔다.

### E. 증시 내부환경

현재:
- KRX 대표바스켓 35일 중앙수익률
- 상승종목 비율
- 외국인+기관 순매수 방향 확산도
- 한국증시 직접환경
- 글로벌 equity 환경

3개월:
- 현재 수급은 25%만 잔존시킨다.
- 검증된 Global Card 12 equity 3M에 더 높은 비중을 둔다.
- 산업붐 leading signal을 제한적으로 결합한다.

### F. 밸류에이션

- 대표바스켓 PER/PBR 중앙값
- 전체 수집 종목 횡단면 PER/PBR 중앙값 대비 상대가치
- KRX 직접값이 없으면 한국증시 broad valuation proxy를 축소 사용

현재 버전은 장기 업종 역사백분위가 아니므로 품질을 제한한다.

## 3. 품질가중 집계

각 축 `i`에 대해:

- `effective_weight_i = base_weight_i × quality_i / 100`
- 사용 가능한 축만 다시 정규화하여 합산
- `quality_weighted_coverage = Σ effective_weight_i`
- coverage가 0.65 미만이면 50 중립 방향으로 추가 수축

따라서 자료 일부가 누락됐다고 그 축을 0점으로 가정하지 않는다.

## 4. 산업붐 pre-validation guard

제공된 V70 snapshot의 `investment_use_allowed=false` 상태에서는:

- 점수의 50 이탈폭을 55%만 인정
- 산업별 `theme_relevance(0~1)`를 한 번 더 적용하여 좁은 테마가 전체 전통산업을 대표하지 못하게 제한
- source diffusion을 반영하되 품질 65 상한
- 오래되어 최대 허용연령을 넘으면 해당 테마 입력을 아예 제외

## 5. 현재/3M 등급

- 0–15 매우 불리
- 16–30 불리
- 31–42 약불리
- 43–57 중립
- 58–69 약우호
- 70–84 우호
- 85–100 매우 우호

## 6. 3개월 변화 강도

절대 점수차:

- <= 1: 거의 변화 없음
- <= 3: 매우 조금
- <= 7: 조금
- <= 12: 꽤
- <= 20: 많이
- > 20: 매우 많이

## 7. 개별종목 주가방향 bridge

`signal_normalized = 0.60 × future_distance_from_50 + 0.40 × delta_signal`

- prospective OOS 미통과: 최대 ±5점
- OOS 통과: 최대 ±10점 설정 가능
- 현재 정책에서는 primary use는 false
- 미래 품질점수가 최소 60 미만이면 보조점수도 0

## 8. Prospective OOS

산업별 월 1개 전망만 immutable registration하여 overlapping sample inflation을 막는다.
75일 이후 당시와 현재에 모두 있는 동일 구성종목의 equal-weight return으로 평가한다.

통과 기본조건:

- evaluated cases >= 24
- directional cases >= 12
- direction accuracy >= 55%
- positive-signal mean return - negative-signal mean return >= 2%p
