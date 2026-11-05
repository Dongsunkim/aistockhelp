"""Price display remains available when financial verification is incomplete."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tests.test_engine import sample_bars


@pytest.fixture
def analysis_api(monkeypatch):
    import app as module

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    bars = sample_bars()
    bars["date"] = pd.bdate_range(
        end=today - timedelta(days=1), periods=len(bars)
    ).date.astype(str)
    bars["open"] = bars["close"]
    monkeypatch.setattr(module, "ON_VERCEL", False)
    monkeypatch.setattr(module, "fetch_daily", lambda symbol, market: (bars, {
        "source": "YAHOO_US_ONLY" if market == "US" else "NAVER",
        "provider": "offline test provider",
        "fetched_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "adjustment_verified": False,
    }))
    monkeypatch.setattr(module, "fetch_news", lambda *args: [
        {"title": "일반 사업 소식", "age_hours": 1}
    ])
    monkeypatch.setattr(module, "fetch_research_mentions", lambda *args: [])
    monkeypatch.setattr(module, "fetch_dart_filings", lambda *args: [
        {"title": "정기보고서", "age_days": 20}
    ])
    monkeypatch.setattr(module, "fetch_sec_filings", lambda *args: [
        {"title": "Quarterly report", "age_days": 20}
    ])
    monkeypatch.setattr(module, "fetch_naver_quote", lambda *args: {
        "price": 117, "provider": "offline test", "verified_live": False
    })
    monkeypatch.setattr(module, "kis_configured", lambda: False)
    return module, TestClient(module.app), bars, today


@pytest.mark.parametrize("market,symbol", [("KOSPI", "005930"), ("US", "AAPL")])
@pytest.mark.parametrize("financial_state", ["missing", "error", "mismatch"])
def test_financial_verification_does_not_hide_technical_prices(
    analysis_api, monkeypatch, market, symbol, financial_state
):
    module, client, _, today = analysis_api

    def financials(*args, **kwargs):
        if financial_state == "error":
            detail="금융자료 공급원 응답 없음"
            if market!="US":detail+="?crtfc_key=offline-test-private"
            raise ValueError(detail)
        if financial_state == "missing":
            return []
        facts = [
            {"name": name, "value": 100, "unit": "USD" if market == "US" else "KRW",
             "period": str(today - timedelta(days=30)), "basis": "연결"}
            for name in ("매출액", "영업이익", "순이익")
        ]
        facts[-1]["basis"] = "별도"
        return facts

    monkeypatch.setattr(module, "fetch_financial_facts", financials)
    response = client.post("/api/analyze", json={"symbol": symbol, "market": market})
    assert response.status_code == 200
    result = response.json()
    quality = result["data_quality"]
    assert quality["price_levels_eligible"] is True
    assert quality["price_level_blockers"] == []
    assert quality["verified_for_trade"] is False
    assert result["analysis"]["confirmed"] is False
    assert any("재무" in warning for warning in quality["warnings"])
    if financial_state == "error":
        if market=="US":assert "금융자료 공급원 응답 없음" in result["financials_error"]
        else:
            assert "재무계정 확인 불가" in result["financials_error"]
            assert "offline-test-private" not in response.text
    if financial_state == "mismatch":
        assert any("기준 불일치" in warning for warning in quality["warnings"])
    levels = result["analysis"]["levels"]
    assert 0 < levels["stop"] < levels["entry_low"] <= levels["entry_high"]
    assert levels["entry_high"] < levels["target_resistance"]
    reasons = " ".join(result["evidence_pack"]["price_reason"])
    assert f'{levels["entry_low"]:,.2f}~{levels["entry_high"]:,.2f}' in reasons


def test_stale_prices_return_a_concrete_display_blocker(analysis_api, monkeypatch):
    module, client, bars, today = analysis_api
    bars["date"] = pd.bdate_range(
        end=today - timedelta(days=30), periods=len(bars)
    ).date.astype(str)
    monkeypatch.setattr(module, "fetch_financial_facts", lambda *args, **kwargs: [])
    response = client.post("/api/analyze", json={"symbol": "005930", "market": "KOSPI"})
    assert response.status_code == 200
    result = response.json()
    quality = result["data_quality"]
    assert quality["price_levels_eligible"] is False
    assert any("종가" in blocker for blocker in quality["price_level_blockers"])
    assert quality["verified_for_trade"] is False
    assert result["analysis"]["confirmed"] is False
    assert any(
        blocker in " ".join(result["evidence_pack"]["price_reason"])
        for blocker in quality["price_level_blockers"]
    )
