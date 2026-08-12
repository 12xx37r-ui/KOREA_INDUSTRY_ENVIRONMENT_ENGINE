# Industry cycle input contract

`industry_cycle_latest.json` is the only authoritative input for the industry
physical-cycle score. The engine does not infer an industry score from an ETF,
theme, company price, or broad macro series when this feed is absent.

## Required shape

```json
{
  "schema_version": "1.0.0",
  "status": "scored",
  "generated_at_utc": "2026-08-12T00:00:00+00:00",
  "industries": [
    {
      "industry_key": "semiconductor",
      "current": {
        "score": 72.0,
        "band": "유리",
        "cycle_phase": "확장",
        "quality_score": 84.0,
        "data_coverage_pct": 88.0,
        "metrics": [
          {
            "id": "production",
            "label": "산업생산",
            "value": 5.4,
            "unit": "% YoY",
            "change_1m": 1.2,
            "change_3m": 4.1,
            "change_6m": 8.0,
            "long_run_percentile": 72.0,
            "score": 74.0,
            "quality": 90.0,
            "source": "official source name and series id",
            "as_of": "2026-07-31"
          }
        ],
        "positive_indicators": ["산업생산", "출하"],
        "negative_indicators": ["재고/출하 비율"]
      },
      "forecasts": {
        "3m": {
          "score": 78.0,
          "direction": "개선",
          "cycle_phase": "확장",
          "quality_score": 80.0,
          "data_coverage_pct": 82.0,
          "metrics": [],
          "global_impact_score": 76.0,
          "korea_impact_score": 68.0,
          "industry_leading_score": 80.0,
          "sensitivity_used": {"global_pmi": 0.8, "korea_exports": 0.6}
        },
        "3_6m": {"score": 75.0, "direction": "소폭 개선", "quality_score": 65.0, "data_coverage_pct": 65.0, "metrics": []},
        "6_12m": {"score": 70.0, "direction": "중립", "quality_score": 55.0, "data_coverage_pct": 60.0, "metrics": []}
      },
      "specialized_metrics": ["dram_nand_price", "server_capex", "memory_cycle"]
    }
  ]
}
```

`score` must be a reproducible 0–100 normalization of the underlying series.
The collector must retain the raw value, unit, period changes, long-run
percentile, source, series identifier, and as-of date for every metric. Missing
metrics are omitted or marked unavailable; they are never replaced with 50.

Current scoring uses observed industry metrics only. Forecast scoring uses
industry leading metrics first and adds only the global/Korea series listed in
the industry's sensitivity profile. A feed without the minimum coverage gate is
published as `status: pending`, and the engine returns `insufficient_data`.

The forecast macro inputs are loaded once per run from the existing engines:

* `global-macro-data-collector` → `public/data/cards_8_12_bundle.json`
* `korea-rate-fx-engine` → `output/korea_rate_fx_outlook_v3.json` and `output/korea_equity_environment.json`
* `fed-futures-collector` → `public/data/latest.json`
* `industry-boom-leading-engine` → its validated industry-leading snapshot

These sources are cached and memoized. They are not used to manufacture the
current industry score. The industry-cycle collector must publish the
industry-specific current/leading metrics and its sensitivity mapping in the
contract above; the renderer then exposes the macro impact fields for audit.

## Raw input and local batch builder

The collection job may write `input/industry_cycle_raw.json`. The Action first
runs `python -m kiee.industry_kosis_collector --root .` with the optional
`KOSIS_API_KEY` secret, then runs `python -m kiee.industry_cycle_feed --root .`, which normalizes raw
metrics, groups them by the configured factor weights, and writes
`input/industry_cycle_latest.json`. Current scoring uses only industry
observations. Forecast stages may include the explicitly mapped sensitive macro
observations, but they must remain in the forecast stage provenance.

Each raw metric must include `factor`, a reproducible `score` or
`long_run_percentile`, positive `quality`, `source`, and `as_of`. A numeric
`value` without an explicit normalization is not scored. The builder applies
the coverage gates from `config/scoring_policy.json`; missing factors are not
replaced with a neutral value. If the raw file is absent or below the gate, the
feed remains `pending` and the engine reports `insufficient_data`.
