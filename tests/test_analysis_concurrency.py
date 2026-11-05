"""Synchronization-based checks for concurrent, independent source retrieval."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event, Lock, local
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tests.test_engine import sample_bars


def install_sources(monkeypatch, market, rendezvous, failing=None, release=None, finished=None):
    import app as module

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    bars = sample_bars()
    bars["open"] = bars["close"]
    bars["date"] = pd.bdate_range(
        end=today - timedelta(days=1), periods=len(bars)
    ).date.astype(str)
    meta = {"source": "YAHOO_US_ONLY" if market == "US" else "NAVER",
            "provider": "offline test", "fetched_at": datetime.now().astimezone().isoformat()}
    payloads = {
        "daily": (bars, meta),
        "news": [{"title": "일반 사업 소식", "age_hours": 1, "link": "https://example.com/news"}],
        "research": [{"title": "전망 관련 보도", "link": "https://example.com/research"}],
        "filings": [{"title": "정기보고서", "age_days": 20, "link": "https://example.com/filing"}],
        "financials": [{"name": name, "value": 100, "unit": "USD" if market == "US" else "KRW",
                        "period": str(today - timedelta(days=30)), "basis": "연결"}
                       for name in ("매출액", "영업이익", "순이익")],
        "quote": {"price": 117, "provider": "offline test", "verified_live": False},
    }

    def provider(kind):
        def fetch(*args, **kwargs):
            try:
                # All six providers must start before any provider can complete.
                rendezvous.wait(timeout=10)
                if kind == failing:
                    raise ValueError(f"offline {kind} unavailable")
                if release is not None:
                    assert release.wait(timeout=10), "dependent response failed to release source"
                return payloads[kind]
            finally:
                if finished is not None:
                    finished[kind].set()
        return fetch

    monkeypatch.setattr(module, "ON_VERCEL", False)
    monkeypatch.setattr(module, "fetch_daily", provider("daily"))
    monkeypatch.setattr(module, "fetch_news", provider("news"))
    monkeypatch.setattr(module, "fetch_research_mentions", provider("research"))
    monkeypatch.setattr(module, "fetch_dart_filings", provider("filings"))
    monkeypatch.setattr(module, "fetch_sec_filings", provider("filings"))
    monkeypatch.setattr(module, "fetch_financial_facts", provider("financials"))
    monkeypatch.setattr(module, "fetch_naver_quote", provider("quote"))
    monkeypatch.setattr(module, "fetch_kis_quote", provider("quote"))
    monkeypatch.setattr(module, "kis_configured", lambda: True)
    return TestClient(module.app)


@pytest.mark.parametrize("market,symbol", [("KOSPI", "005930"), ("US", "AAPL")])
@pytest.mark.parametrize("failing", ["news", "financials"])
def test_sources_overlap_and_partial_failure_keeps_other_evidence(monkeypatch, market, symbol, failing):
    client = install_sources(monkeypatch, market, Barrier(6), failing=failing)
    response = client.post("/api/analyze", json={"symbol": symbol, "market": market})
    assert response.status_code == 200
    result = response.json()
    expected_error=("재무계정 확인 불가" if failing=="financials" and market!="US"
                    else f"offline {failing} unavailable")
    assert expected_error in result[f"{failing}_error"]
    assert result[failing] == []
    assert result["research"][0]["link"] == "https://example.com/research"
    assert result["filings"][0]["link"] == "https://example.com/filing"
    assert result["quote"]["price"] == 117
    assert result["data_quality"]["price_levels_eligible"] is True
    assert result["analysis"]["confirmed"] is False
    if failing == "financials":
        assert any(expected_error in w for w in result["data_quality"]["warnings"])
    else:
        assert len(result["financials"]) == 3


def test_fatal_daily_failure_returns_before_unrelated_sources_finish(monkeypatch):
    release = Event()
    finished = {kind: Event() for kind in ("daily", "news", "research", "filings", "financials", "quote")}
    client = install_sources(monkeypatch, "KOSPI", Barrier(6), failing="daily", release=release, finished=finished)
    with ThreadPoolExecutor(max_workers=1) as caller:
        request = caller.submit(client.post, "/api/analyze", json={"symbol": "005930", "market": "KOSPI"})
        try:
            response = request.result(timeout=5)
            assert response.status_code == 502
            assert "offline daily unavailable" in response.json()["detail"]
            assert not finished["news"].is_set()
            assert not finished["financials"].is_set()
        finally:
            release.set()
            assert all(event.wait(timeout=5) for event in finished.values())


def test_http_sessions_are_reused_only_within_their_own_thread(monkeypatch):
    import sources

    created = []
    lock = Lock()

    class FakeSession:
        def __init__(self):
            self.headers = {}
            with lock:
                created.append(self)

    monkeypatch.setattr(sources, "_sessions", local())
    monkeypatch.setattr(sources.requests, "Session", FakeSession)
    rendezvous = Barrier(2)

    def worker():
        first = sources._get_session()
        rendezvous.wait(timeout=5)
        assert sources._get_session() is first
        assert "User-Agent" in first.headers
        return first

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(worker) for _ in range(2)]
        sessions = [job.result(timeout=10) for job in jobs]
    assert sessions[0] is not sessions[1]
    assert len(created) == 2
