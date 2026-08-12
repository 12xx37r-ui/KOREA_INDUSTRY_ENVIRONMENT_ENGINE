import json
import shutil
from pathlib import Path

from kiee.engine import run_engine


def _runtime(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / "runtime"
    for folder in ("config", "fixtures"):
        shutil.copytree(source / folder, root / folder)
    (root / "output" / "industries").mkdir(parents=True)
    (root / "input_cache" / "latest").mkdir(parents=True)
    return root


def test_compact_dashboard_export_is_written_once(tmp_path):
    root = _runtime(tmp_path)
    result = run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    payload = json.loads((root / "output" / "industry_dashboard.json").read_text(encoding="utf-8"))
    assert payload["industry_count"] == result["industry_count"]
    assert len(payload["industries"]) == result["industry_count"]
    assert all("current" in row and "forecast_3m" in row for row in payload["industries"])
    assert all("companies" in row for row in payload["industries"])


def test_dashboard_contains_no_network_configuration(tmp_path):
    root = _runtime(tmp_path)
    run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    payload = json.loads((root / "output" / "industry_dashboard.json").read_text(encoding="utf-8"))
    text = json.dumps(payload, ensure_ascii=False)
    assert "http://" not in text
    assert "https://" not in text
