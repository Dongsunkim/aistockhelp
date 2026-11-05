"""Offline routing and provenance checks for domestic Naver-first source adapters."""
from datetime import datetime, timedelta
from types import ModuleType
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient

import sources
from tests.test_engine import sample_bars


ADAPTERS = {
    "news": ("naver_news", "fetch_naver_news", "fetch_news", "_fetch_news_rss", "네이버증권 종목뉴스"),
    "research": ("naver_research", "fetch_naver_research", "fetch_research_mentions",
                 "_fetch_research_mentions_rss", "네이버증권 리서치"),
    "financials": ("naver_financials", "fetch_naver_financials", "fetch_financial_facts",
                   "_fetch_official_financial_facts", "네이버증권 재무"),
}


def install_adapter(monkeypatch, kind, primary, fallback):
    module_name, adapter, _, original, _ = ADAPTERS[kind]
    # The imports are lazy; substituting the module prevents any actual HTTP access.
    module = ModuleType(module_name)
    setattr(module, adapter, primary)
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(sources, original, fallback)
    session = object()
    monkeypatch.setattr(sources, "_get_session", lambda: session)
    return session


def call(kind, market="KOSPI"):
    function = getattr(sources, ADAPTERS[kind][2])
    term = "005930" if market != "US" else "AAPL"
    if kind == "financials":
        return function(term, market, dart_key="test-dart", sec_user_agent="test@example.com")
    return function(term, market, limit=3)


@pytest.mark.parametrize("kind", ADAPTERS)
@pytest.mark.parametrize("market", ["KOSPI", "KOSDAQ"])
def test_domestic_naver_success_does_not_call_original_sources(monkeypatch, kind, market):
    primary_items = [{"source": "네이버증권", "verification": "목록 확인 · 원문 미확인",
                      "link": "https://finance.naver.com/original", "date": "2026-11-02"}]
    calls = []

    def primary(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return primary_items

    def unexpected_fallback(*args, **kwargs):
        pytest.fail("successful Naver retrieval must not call fallback")

    session = install_adapter(monkeypatch, kind, primary, unexpected_fallback)
    result = call(kind, market)
    assert result is primary_items
    assert calls[0][0] == "005930"
    assert calls[0][1]["session"] is session
    if kind != "financials":
        assert calls[0][1]["limit"] == 3
    assert "fallback_reason" not in result[0]


@pytest.mark.parametrize("kind", ADAPTERS)
@pytest.mark.parametrize("error_type", [ValueError, requests.Timeout])
def test_failed_primary_calls_original_then_retains_actual_source_and_failure_reason(monkeypatch, kind, error_type):
    order = []
    original_item = {"source": "실제 대체 공급원", "link": "https://example.com/original",
                     "published_at": "2026-11-02T02:00:00Z", "basis": "원문 미확인"}

    def primary(*args, **kwargs):
        order.append("naver")
        raise error_type("listing response unavailable")

    def fallback(*args, **kwargs):
        order.append("original")
        assert args[:2] == ("005930", "KOSPI")
        if kind == "financials":
            assert kwargs == {"dart_key": "test-dart", "sec_user_agent": "test@example.com"}
        else:
            assert args[2] == 3
        return [original_item]

    install_adapter(monkeypatch, kind, primary, fallback)
    result = call(kind)
    assert order == ["naver", "original"]
    for key, value in original_item.items():
        assert result[0][key] == value
    assert result[0]["preferred_source"] == ADAPTERS[kind][4]
    assert "listing response unavailable" in result[0]["fallback_reason"]
    assert "fallback_reason" not in original_item  # Original records are not mutated.


@pytest.mark.parametrize("kind", ADAPTERS)
def test_valid_empty_primary_is_not_treated_as_a_source_failure(monkeypatch, kind):
    def unexpected_fallback(*args, **kwargs):
        pytest.fail("empty valid source response must remain empty")

    install_adapter(monkeypatch, kind, lambda *args, **kwargs: [], unexpected_fallback)
    assert call(kind) == []


@pytest.mark.parametrize("kind", ADAPTERS)
def test_us_continues_original_sources_without_naver_calls_or_relabeling(monkeypatch, kind):
    original_items = [{"source": "SEC" if kind == "financials" else "Original RSS",
                       "link": "https://example.com/original"}]
    calls = []

    def unexpected_naver(*args, **kwargs):
        pytest.fail("US data must not be sent to domestic Naver adapters")

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return original_items

    install_adapter(monkeypatch, kind, unexpected_naver, original)
    assert call(kind, "US") is original_items
    assert calls[0][0][:2] == ("AAPL", "US")
    assert "preferred_source" not in original_items[0]
    assert "fallback_reason" not in original_items[0]


def test_failed_naver_and_official_financials_keep_both_error_reasons(monkeypatch):
    order = []

    def primary(*args, **kwargs):
        order.append("naver")
        raise ValueError("financial table missing")

    def official(*args, **kwargs):
        order.append("official")
        raise ValueError("DART key missing")

    install_adapter(monkeypatch, "financials", primary, official)
    with pytest.raises(ValueError) as caught:
        call("financials")
    assert order == ["naver", "official"]
    assert "financial table missing" in str(caught.value)
    assert "DART key missing" in str(caught.value)
    assert str(caught.value.__cause__) == "DART key missing"


def test_domestic_api_looks_up_naver_by_ticker_even_when_company_name_is_given(monkeypatch):
    import app as module

    seen = []
    for kind in ADAPTERS:
        def primary(symbol, _kind=kind, **kwargs):
            seen.append((_kind, symbol))
            return []

        def unexpected_original(*args, **kwargs):
            pytest.fail("valid primary response must not call original source")

        install_adapter(monkeypatch, kind, primary, unexpected_original)
    bars = sample_bars()
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    bars["date"] = pd.bdate_range(end=today - timedelta(days=1), periods=len(bars)).date.astype(str)
    bars["open"] = bars["close"]
    monkeypatch.setattr(module, "ON_VERCEL", False)
    monkeypatch.setattr(module, "fetch_daily", lambda *args: (bars, {
        "source": "NAVER", "provider": "offline test", "fetched_at": datetime.now().astimezone().isoformat()}))
    monkeypatch.setattr(module, "fetch_dart_filings", lambda *args: [])
    monkeypatch.setattr(module, "fetch_naver_quote", lambda *args: {"price": 117, "verified_live": False})
    response = TestClient(module.app).post("/api/analyze", json={
        "symbol": "005930", "market": "KOSPI", "name": "기업명 검색어"})
    assert response.status_code == 200
    assert set(seen) == {(kind, "005930") for kind in ADAPTERS}
    assert response.json()["company_name"] == "기업명 검색어"
