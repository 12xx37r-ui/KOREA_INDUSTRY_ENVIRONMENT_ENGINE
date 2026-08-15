from pathlib import Path
import json


def test_service_and_retail_have_explicit_series_keywords():
    root = Path(__file__).resolve().parents[1]
    cfg = json.loads((root / "config" / "industry_kosis_sources.json").read_text(encoding="utf-8"))
    service = cfg["series"]["service_production"]
    retail = cfg["series"]["retail_sales"]
    assert "travel" in service["industry_keywords"]
    assert "finance" in service["industry_keywords"]
    assert "hotel" in service["industry_keywords"]
    assert "department_store" in retail["industry_keywords"]
    assert "online_shopping" in retail["industry_keywords"]
