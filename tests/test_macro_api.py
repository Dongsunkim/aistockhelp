"""Macro context is independent, concurrent evidence; every provider is offline."""
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient

from tests.test_engine import sample_bars


def macro_snapshot(today):
    observations = {
        "rate": (4.0, "%", 4.25),
        "usdkrw": (1380.0, "KRW/USD", 1375.0),
        "inflation": (2.5, "%", 2.7),
        "sp500": (6000.0, "points", 5980.0),
        "nasdaq": (20000.0, "points", 19900.0),
        "sox": (5000.0, "points", 4980.0),
        "nvda": (150.0, "USD", 149.0),
        "aapl": (240.0, "USD", 239.0),
        "msft": (480.0, "USD", 478.0),
    }
    metrics = {}
    for key, (value, unit, previous) in observations.items():
        metrics[key] = {
            "value": value, "unit": unit, "previous": previous,
            "asof": str(today - timedelta(days=1)),
            "source": "offline macro fixture", "url": f"https://example.org/macro/{key}",
            "change_pp" if key in ("rate", "inflation") else "change_pct": (
                value - previous if key in ("rate", "inflation") else (value / previous - 1) * 100
            ),
        }
    metrics["inflation"]["asof"] = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return {
        "eligible": True, "summary": "금리·환율·물가·미국 기술주 지표 조회 완료",
        "metrics": metrics, "risk_flags": ["기술주 변동성 확인 필요"], "warnings": [],
        "checked_at": datetime.now(timezone.utc).isoformat(), "cached_at": None,
    }


def install_offline_sources(monkeypatch, *, market="KOSPI", macro_failure=None, rendezvous=None):
    import app as module

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    bars = sample_bars()
    bars["open"] = bars["close"]
    bars["date"] = pd.bdate_range(end=today - timedelta(days=1), periods=len(bars)).date.astype(str)
    snapshot = macro_snapshot(today)
    payloads = {
        "daily": (bars, {"source": "YAHOO_US_ONLY" if market == "US" else "NAVER",
                         "provider": "offline daily fixture",
                         "fetched_at": datetime.now(timezone.utc).isoformat()}),
        "news": [{"title": "일반 사업 소식", "age_hours": 1,
                  "source": "네이버증권 뉴스", "link": "https://example.org/news"}],
        "research": [{"title": "전망 관련 보도", "link": "https://example.org/research"}],
        "filings": [{"title": "정기보고서", "age_days": 20, "link": "https://example.org/filing"}],
        "financials": [{"name": name, "value": 100_000_000, "unit": "USD" if market == "US" else "KRW",
                        "period": str(today - timedelta(days=30)), "basis": "연결",
                        "source": "SEC XBRL companyfacts" if market == "US" else "네이버증권 기업실적분석",
                        "basis_verified": True, "is_estimate": False}
                       for name in ("매출액", "영업이익", "순이익")],
        "quote": {"price": 117, "provider": "offline quote fixture", "verified_live": False},
        "macro": snapshot,
    }
    started = set()
    lock = Lock()

    def provider(kind):
        def fetch(*args, **kwargs):
            with lock:
                started.add(kind)
            if rendezvous is not None:
                # Sequential retrieval, or only six workers, cannot satisfy this barrier.
                rendezvous.wait(timeout=10)
            if kind == "macro" and macro_failure is not None:
                raise macro_failure
            return payloads[kind]
        return fetch

    monkeypatch.setattr(module, "ON_VERCEL", False)
    for name, kind in {
        "fetch_daily": "daily", "fetch_news": "news", "fetch_research_mentions": "research",
        "fetch_dart_filings": "filings", "fetch_sec_filings": "filings",
        "fetch_financial_facts": "financials", "fetch_naver_quote": "quote",
        "fetch_kis_quote": "quote", "fetch_macro_context": "macro",
    }.items():
        monkeypatch.setattr(module, name, provider(kind))
    monkeypatch.setattr(module, "kis_configured", lambda: True)
    return TestClient(module.app), snapshot, started


@pytest.mark.parametrize("failure", [requests.RequestException("offline macro timeout"), ValueError("offline macro invalid data")])
def test_macro_failure_is_nonfatal_and_preserves_domestic_analysis(monkeypatch, failure):
    client, _, started = install_offline_sources(monkeypatch, macro_failure=failure)
    response = client.post("/api/analyze", json={"symbol": "005930", "market": "KOSPI", "name": "삼성전자"})
    assert response.status_code == 200
    result = response.json()
    assert "macro" in started
    assert result["meta"]["source"] == "NAVER"
    assert result["news"][0]["source"] == "네이버증권 뉴스"
    assert len(result["financials"]) == 3
    assert result["quote"]["price"] == 117
    assert result["analysis"]["levels"]["entry_low"] > 0
    assert result["data_quality"]["price_levels_eligible"] is True
    assert result["analysis"]["confirmed"] is False
    assert result["data_quality"]["verified_for_trade"] is False
    macro = result["macro"]
    assert macro["eligible"] is False
    assert macro["warnings"]
    assert all(isinstance(warning, str) and warning.strip() for warning in macro["warnings"])
    assert macro["summary"]
    assert "metrics" in macro and "risk_flags" in macro and "checked_at" in macro


@pytest.mark.parametrize("market,symbol", [("KOSPI", "005930"), ("US", "AAPL")])
def test_macro_metrics_and_provenance_pass_through_without_trade_confirmation(monkeypatch, market, symbol):
    client, snapshot, _ = install_offline_sources(monkeypatch, market=market)
    response = client.post("/api/analyze", json={"symbol": symbol, "market": market})
    assert response.status_code == 200
    result = response.json()
    assert result["macro"] == snapshot
    assert set(result["macro"]["metrics"]) == {
        "rate", "usdkrw", "inflation", "sp500", "nasdaq", "sox", "nvda", "aapl", "msft",
    }
    assert result["analysis"]["confirmed"] is False
    assert result["data_quality"]["verified_for_trade"] is False


def test_macro_retrieval_starts_concurrently_with_all_six_stock_sources(monkeypatch):
    client, snapshot, started = install_offline_sources(monkeypatch, rendezvous=Barrier(7))
    response = client.post("/api/analyze", json={"symbol": "005930", "market": "KOSPI"})
    assert response.status_code == 200
    assert started == {"daily", "news", "research", "filings", "financials", "quote", "macro"}
    result = response.json()
    assert result["macro"] == snapshot
    assert result["data_quality"]["price_levels_eligible"] is True
    assert result["analysis"]["confirmed"] is False
