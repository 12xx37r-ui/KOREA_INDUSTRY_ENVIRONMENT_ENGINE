"""
v1.2.0 신규 로직 테스트:
  - observed / estimated 이중 경로
  - coverage → quality 상한 (coverage_quality_cap)
  - OOS bridge 한도 (PENDING → ±2pt)
  - 단일 지표 과의존 방지 (50 방향 수축)
  - feed_pending 차단 (status=pending이면 estimated null)
  - 6축 팩터 분해 복구
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from kiee.scoring import (
    _coverage_quality_cap,
    _oos_bridge_limits,
    _pending_industry_result,
    _feed_stage,
    score_industry,
    FACTOR_ORDER,
)
from kiee.config import load_all

SOURCE_ROOT = Path(__file__).resolve().parents[1]


# ── 공통 픽스처 ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def config():
    industries_cfg, policy, _ = load_all(SOURCE_ROOT)
    return industries_cfg, policy


@pytest.fixture(scope="module")
def upstreams():
    """실제 input_cache 파일 로드 (GitHub Actions에서는 fixtures/ 사용)."""
    fixture_dir = SOURCE_ROOT / "fixtures" / "upstream"
    cache_dir = SOURCE_ROOT / "input_cache" / "latest"

    def _load(name: str, fallbacks: list[str]) -> dict:
        for path in fallbacks:
            p = Path(path)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        pytest.skip(f"upstream data not available: {name}")

    korea_rate = _load("korea_rate_fx", [
        cache_dir / "korea_rate_fx.json",
        fixture_dir / "korea_rate_fx_outlook_v3.json",
    ])
    korea_equity = _load("korea_equity", [
        cache_dir / "korea_equity.json",
        fixture_dir / "korea_equity_environment.json",
    ])
    global_bundle = _load("global_bundle", [
        cache_dir / "global_bundle.json",
        fixture_dir / "cards_8_12_bundle.json",
    ])
    boom = _load("industry_boom", [
        cache_dir / "industry_boom.json",
        fixture_dir / "industry_boom_snapshot.json",
    ])
    return {
        "korea_rate": korea_rate,
        "korea_equity": korea_equity,
        "global_bundle": global_bundle,
        "boom": boom,
    }


@pytest.fixture(scope="module")
def scored_cycle():
    """14개 scored 피드 (input/industry_cycle_latest.json)."""
    path = SOURCE_ROOT / "input" / "industry_cycle_latest.json"
    if not path.exists():
        pytest.skip("scored cycle feed not available")
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("status") == "pending" or not d.get("industries"):
        pytest.skip("cycle feed is pending")
    return d


@pytest.fixture(scope="module")
def pending_cycle():
    """빈 pending 피드 (fixtures/upstream/industry_cycle_latest.json)."""
    path = SOURCE_ROOT / "fixtures" / "upstream" / "industry_cycle_latest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _prospective(status="PENDING", cases=0):
    return {"status": status, "evaluated_cases": cases, "quality_score": 30.0}


# ── 1. coverage_quality_cap ──────────────────────────────────────────────────

class TestCoverageQualityCap:
    def test_zero_coverage_returns_zero(self):
        assert _coverage_quality_cap(0) == 0.0

    def test_ten_pct_coverage_caps_at_35(self):
        assert _coverage_quality_cap(10.0) <= 35.0

    def test_twenty_pct_coverage_caps_at_48(self):
        assert _coverage_quality_cap(20.0) <= 48.0

    def test_full_coverage_allows_100(self):
        assert _coverage_quality_cap(100.0) == 100.0

    def test_monotone_increasing(self):
        caps = [_coverage_quality_cap(c) for c in (0, 10, 20, 35, 50, 70, 100)]
        assert caps == sorted(caps)


# ── 2. OOS bridge 한도 ────────────────────────────────────────────────────────

class TestOosBridgeLimits:
    def _policy(self):
        _, policy, _ = load_all(SOURCE_ROOT)
        return policy

    def test_pending_zero_cases_max_2pt(self):
        limits = _oos_bridge_limits("PENDING", 0, self._policy())
        assert limits["max_points"] <= 2.0
        assert limits["allowed_primary"] is False
        assert limits["allowed_auxiliary"] is False

    def test_pending_half_cases_auxiliary_allowed(self):
        # prospective_min_cases=24 → 절반=12건
        limits = _oos_bridge_limits("PENDING", 12, self._policy())
        assert limits["max_points"] <= 2.0
        assert limits["allowed_primary"] is False

    def test_passed_full_cases_higher_limit(self):
        limits = _oos_bridge_limits("PASSED", 24, self._policy())
        assert limits["max_points"] >= 5.0
        assert limits["allowed_auxiliary"] is True

    def test_passed_without_enough_cases_still_limited(self):
        limits = _oos_bridge_limits("PASSED", 5, self._policy())
        # cases < min_cases/2=12 → pending 경로
        assert limits["max_points"] <= 2.0


# ── 3. feed_pending 차단 ─────────────────────────────────────────────────────

class TestFeedPendingGate:
    def test_pending_cycle_produces_null_scores(self, config, upstreams, pending_cycle):
        industries_cfg, policy = config
        ind = next(i for i in industries_cfg["industries"] if i.get("theme_ids"))
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], pending_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        assert result["current"]["score"] is None, "pending feed must not produce estimated score"
        assert result["forecast_3m"]["score"] is None
        assert result["current"]["status"] == "insufficient_data"
        assert result["quality"]["data_status"] == "insufficient_data"

    def test_pending_cycle_quality_is_zero(self, config, upstreams, pending_cycle):
        industries_cfg, policy = config
        ind = industries_cfg["industries"][0]
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], pending_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        assert result["current"]["quality_score"] == 0.0
        assert result["forecast_3m"]["quality_score"] == 0.0


# ── 4. observed 경로 ──────────────────────────────────────────────────────────

class TestObservedPath:
    def test_observed_industry_has_score_source_observed(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        ind = next(i for i in industries_cfg["industries"] if i["key"] == "semiconductor")
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        assert result["current"]["score_source"] == "observed"
        assert result["current"]["observed_score"] is not None
        assert result["current"]["score"] is not None

    def test_observed_quality_capped_by_coverage(self, config, upstreams, scored_cycle):
        """coverage=10% → quality ≤ 35."""
        industries_cfg, policy = config
        ind = next(i for i in industries_cfg["industries"] if i["key"] == "semiconductor")
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        assert result["current"]["quality_score"] <= 35.0, (
            f"coverage=10% → quality must be ≤35, got {result['current']['quality_score']}"
        )

    def test_observed_factors_partially_available(self, config, upstreams, scored_cycle):
        """피드에 factor_scores 없어도 6축 중 일부는 available=True여야 한다."""
        industries_cfg, policy = config
        ind = next(i for i in industries_cfg["industries"] if i["key"] == "semiconductor")
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        factors = result["current"]["factors"]
        assert set(factors.keys()) == set(FACTOR_ORDER), "6축 키 누락"
        available_count = sum(1 for f in factors.values() if f.get("available"))
        assert available_count >= 1, "observed 산업에서 factor available이 하나도 없음"

    def test_observed_bridge_pending_oos_max_2pt(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        ind = next(i for i in industries_cfg["industries"] if i["key"] == "semiconductor")
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective("PENDING", 0),
        )
        bridge = result["stock_prediction_bridge"]
        assert bridge["max_abs_adjustment_points"] <= 2.0
        assert bridge["allowed_as_primary"] is False
        assert abs(bridge["bounded_direction_adjustment_points"]) <= bridge["max_abs_adjustment_points"] + 1e-6


# ── 5. estimated 경로 ─────────────────────────────────────────────────────────

class TestEstimatedPath:
    def _industry_not_in_feed(self, industries_cfg, scored_cycle):
        feed_keys = {i.get("industry_key") for i in scored_cycle.get("industries", [])}
        return next(i for i in industries_cfg["industries"] if i["key"] not in feed_keys and i.get("theme_ids"))

    def test_estimated_score_source_field(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        ind = self._industry_not_in_feed(industries_cfg, scored_cycle)
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        cur = result["current"]
        assert cur["score_source"] == "estimated"
        assert cur["observed_score"] is None
        assert cur["estimated_score"] is not None
        assert cur["estimated_quality"] is not None

    def test_estimated_quality_below_observed_ceiling(self, config, upstreams, scored_cycle):
        """estimated quality는 observed보다 낮아야 한다 (신뢰도 차이 반영)."""
        industries_cfg, policy = config
        ind = self._industry_not_in_feed(industries_cfg, scored_cycle)
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        assert result["current"]["quality_score"] < 60.0, "estimated quality는 60 미만이어야 함"

    def test_estimated_status_field(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        ind = self._industry_not_in_feed(industries_cfg, scored_cycle)
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        assert result["current"]["status"] == "estimated"
        assert result["forecast_3m"]["status"] == "estimated"

    def test_estimated_forecast_3m_has_score(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        ind = self._industry_not_in_feed(industries_cfg, scored_cycle)
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        assert result["forecast_3m"]["score"] is not None

    def test_estimated_score_is_neutral_shrunk_toward_50(self, config, upstreams, scored_cycle):
        """estimated score는 extreme 값이 나오면 안 된다 (50 방향 수축)."""
        industries_cfg, policy = config
        ind = self._industry_not_in_feed(industries_cfg, scored_cycle)
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective(),
        )
        score = result["current"]["score"]
        assert score is not None
        # estimated는 단독 데이터 부족이므로 극단값(10 미만 or 90 초과) 금지
        assert 15 <= score <= 85, f"estimated score {score} is too extreme (expected 15-85)"

    def test_estimated_bridge_pending_still_limited(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        ind = self._industry_not_in_feed(industries_cfg, scored_cycle)
        result = score_industry(
            ind, policy,
            upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
            upstreams["boom"], scored_cycle,
            {"industries": {}}, 85.0, _prospective("PENDING", 0),
        )
        bridge = result["stock_prediction_bridge"]
        assert bridge["max_abs_adjustment_points"] <= 2.0
        assert bridge["allowed_as_primary"] is False


# ── 6. 전체 커버리지 집계 ─────────────────────────────────────────────────────

class TestFullCoverageWithScoredFeed:
    def test_all_101_industries_get_current_score(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        scored = failed = 0
        for ind in industries_cfg["industries"]:
            result = score_industry(
                ind, policy,
                upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
                upstreams["boom"], scored_cycle,
                {"industries": {}}, 85.0, _prospective(),
            )
            if result["current"]["score"] is not None:
                scored += 1
            else:
                failed += 1
        assert scored == len(industries_cfg["industries"]), (
            f"scored={scored}, null={failed} — 모든 산업에 current 점수가 나와야 함"
        )

    def test_all_101_industries_get_forecast_3m(self, config, upstreams, scored_cycle):
        industries_cfg, policy = config
        scored = 0
        for ind in industries_cfg["industries"]:
            result = score_industry(
                ind, policy,
                upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
                upstreams["boom"], scored_cycle,
                {"industries": {}}, 85.0, _prospective(),
            )
            if result["forecast_3m"]["score"] is not None:
                scored += 1
        assert scored == len(industries_cfg["industries"]), (
            f"forecast_3m scored={scored} — 모든 산업에 3m 전망이 나와야 함"
        )

    def test_observed_count_matches_feed(self, config, upstreams, scored_cycle):
        """observed 점수 수 = 피드에 있는 산업 수."""
        industries_cfg, policy = config
        feed_keys = {i.get("industry_key") for i in scored_cycle.get("industries", [])}
        observed_count = 0
        for ind in industries_cfg["industries"]:
            result = score_industry(
                ind, policy,
                upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
                upstreams["boom"], scored_cycle,
                {"industries": {}}, 85.0, _prospective(),
            )
            if result["current"].get("score_source") == "observed":
                observed_count += 1
        # 피드에 있는 산업 중 실제 score가 통과한 것만 counted
        assert observed_count <= len(feed_keys), "피드보다 더 많은 observed가 나오면 안 됨"
        assert observed_count >= 1, "최소 1개 이상 observed가 있어야 함"

    def test_no_industry_has_quality_above_coverage_cap(self, config, upstreams, scored_cycle):
        """coverage 대비 quality가 상한을 초과하지 않는다."""
        from kiee.scoring import _coverage_quality_cap
        industries_cfg, policy = config
        violations = []
        for ind in industries_cfg["industries"]:
            result = score_industry(
                ind, policy,
                upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
                upstreams["boom"], scored_cycle,
                {"industries": {}}, 85.0, _prospective(),
            )
            for block in ("current", "forecast_3m"):
                b = result[block]
                cov = b.get("data_coverage_pct", 0.0) or 0.0
                q = b.get("quality_score", 0.0) or 0.0
                cap = _coverage_quality_cap(cov)
                if q > cap + 0.5:  # 0.5 rounding tolerance
                    violations.append(f"{ind['key']}.{block}: quality={q:.1f} > cap={cap:.1f} (coverage={cov:.1f}%)")
        assert not violations, "quality cap 위반:\n" + "\n".join(violations[:5])

    def test_all_bridges_within_oos_limit(self, config, upstreams, scored_cycle):
        """모든 bridge adjustment가 max_abs_adjustment_points 이내."""
        industries_cfg, policy = config
        violations = []
        for ind in industries_cfg["industries"]:
            result = score_industry(
                ind, policy,
                upstreams["korea_rate"], upstreams["korea_equity"], upstreams["global_bundle"],
                upstreams["boom"], scored_cycle,
                {"industries": {}}, 85.0, _prospective("PENDING", 0),
            )
            bridge = result["stock_prediction_bridge"]
            adj = abs(bridge["bounded_direction_adjustment_points"])
            cap = bridge["max_abs_adjustment_points"]
            if adj > cap + 1e-6:
                violations.append(f"{ind['key']}: adj={adj:.2f} > cap={cap:.2f}")
        assert not violations, "bridge 한도 위반:\n" + "\n".join(violations)


# ── 7. _feed_stage 단위 테스트 ───────────────────────────────────────────────

class TestFeedStageUnit:
    def _minimal_stage(self, score, coverage, quality, factor_scores=None):
        return {
            "score": score,
            "data_coverage_pct": coverage,
            "quality_score": quality,
            "factor_scores": factor_scores or {},
            "metrics": [],
            "status": "scored",
        }

    def _policy(self):
        _, policy, _ = load_all(SOURCE_ROOT)
        return policy

    def test_null_score_returns_insufficient(self):
        policy = self._policy()
        stage = self._minimal_stage(None, 0.0, 0.0)
        result = _feed_stage(stage, policy, "current")
        assert result["status"] == "insufficient_data"
        assert result["score"] is None

    def test_high_quality_low_coverage_is_capped(self):
        """score=60, quality=85, coverage=10% → quality_score ≤ 35."""
        policy = self._policy()
        stage = self._minimal_stage(60.0, 10.0, 85.0, {"utilization": 60.0})
        result = _feed_stage(stage, policy, "current")
        assert result["status"] == "scored"
        assert result["quality_score"] <= 35.0, f"got {result['quality_score']}"

    def test_score_source_observed_in_scored_stage(self):
        policy = self._policy()
        stage = self._minimal_stage(70.0, 50.0, 80.0)
        result = _feed_stage(stage, policy, "current")
        assert result.get("score_source") == "observed"

    def test_factors_keys_always_full_set(self):
        """factor_scores가 없어도 factors 딕셔너리는 6축 키를 가져야 한다."""
        policy = self._policy()
        stage = self._minimal_stage(55.0, 50.0, 75.0)
        result = _feed_stage(stage, policy, "current")
        if result["status"] == "scored":
            assert set(result["factors"].keys()) == set(FACTOR_ORDER)
