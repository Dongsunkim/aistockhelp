"""Offline API checks for stock question validation, context isolation, and limits."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import stock_qa


def server_packet(symbol="005930", market="KOSPI", name="삼성전자"):
    today = datetime.now(timezone.utc).date()
    return {
        "symbol": symbol, "market": market, "company_name": name,
        "meta": {"fetched_at": datetime.now(timezone.utc).isoformat()},
        "analysis": {"date": str(today - timedelta(days=1)), "close": 105,
                     "levels": {"entry_low": 98, "entry_high": 102, "stop": 94,
                                "target_resistance": 122, "rr": 2.5},
                     "conditions": [], "red_flags": [], "confirmed": False},
        "data_quality": {"price_levels_eligible": True, "financials_eligible": False,
                         "verified_for_trade": False, "warnings": ["재무 원문 확인 필요"]},
        "financials": [], "news": [], "filings": [], "research": [],
        "macro": {"eligible": False, "metrics": {}, "warnings": ["미국 자료 없음"]},
        "evidence_pack": {"documents": [
            {"id": "P1", "title": f"{symbol} 확정 일봉 / OHLCV", "kind": "가격 데이터",
             "url": f"https://finance.naver.com/item/main.naver?code={symbol}"}]},
    }


@pytest.fixture
def qa_api(monkeypatch):
    import app as module

    def clear_state():
        with module._limiter_lock:
            module._qa_limiter.clear()
            module._limiter.clear()
        with module._analysis_context_lock:
            module._analysis_contexts.clear()

    def unexpected_call(*args, **kwargs):
        raise AssertionError("This API case must not refresh data or call a paid provider")

    clear_state()
    monkeypatch.setattr(module, "ON_VERCEL", False)
    monkeypatch.setattr(module, "run_analysis", unexpected_call)
    monkeypatch.setattr(module, "answer_question", unexpected_call)
    monkeypatch.setattr(stock_qa.requests, "post", unexpected_call)
    yield module, TestClient(module.app)
    clear_state()


@pytest.mark.parametrize("changes", [
    {"symbol": "AAPL", "market": "KOSPI"},
    {"symbol": "00593"},
    {"market": "INVALID"},
    {"question": ""},
    {"question": "한"},
    {"question": "  "},
    {"question": "가" * 601},
])
def test_invalid_symbol_or_question_is_rejected_before_data_or_paid_calls(qa_api, changes):
    _, client = qa_api
    payload = {"symbol": "005930", "market": "KOSPI", "name": "삼성전자", "question": "가격대는?"}
    response = client.post("/api/ask", json=dict(payload, **changes))
    assert response.status_code == 422


def test_selected_stock_uses_server_context_and_ignores_client_supplied_analysis(qa_api, monkeypatch):
    module, client = qa_api
    packet = server_packet("AAPL", "US", "Apple")
    analyze = Mock(return_value=packet)
    answer = Mock(return_value={"mode": "ai", "answer": "서버 자료를 설명합니다.", "sources": []})
    monkeypatch.setattr(module, "run_analysis", analyze)
    monkeypatch.setattr(module, "answer_question", answer)
    response = client.post("/api/ask", json={
        "symbol": "AAPL", "market": "US", "name": "Apple", "question": "  관찰 가격대는?  ",
        "analysis": {"levels": {"entry_low": 999999}}, "company_name": "다른 기업",
    })
    assert response.status_code == 200
    analyze.assert_called_once_with("AAPL", "Apple", "US")
    answer.assert_called_once_with("관찰 가격대는?", packet)


def test_fresh_cached_stock_context_is_reused_without_provider_refresh(qa_api, monkeypatch):
    module, client = qa_api
    packet = server_packet()
    module._remember_context(packet)
    answer = Mock(return_value={"mode": "data_only", "answer": "캐시 자료 안내", "sources": []})
    monkeypatch.setattr(module, "answer_question", answer)
    response = client.post("/api/ask", json={
        "symbol": "005930", "market": "KOSPI", "name": "삼성전자", "question": "가격대는?",
    })
    assert response.status_code == 200
    assert answer.call_args.args == ("가격대는?", packet)
    assert answer.call_args.args[1] is not packet


def test_expired_context_refreshes_stock_data_before_answering(qa_api, monkeypatch):
    module, client = qa_api
    old = server_packet()
    old["analysis"]["close"] = 1
    key = module._context_key("005930", "KOSPI", "삼성전자")
    with module._analysis_context_lock:
        module._analysis_contexts[key] = (module.time.monotonic() - 91, old)
    fresh = server_packet()
    analyze = Mock(return_value=fresh)
    answer = Mock(return_value={"mode": "data_only", "answer": "새 자료 안내", "sources": []})
    monkeypatch.setattr(module, "run_analysis", analyze)
    monkeypatch.setattr(module, "answer_question", answer)
    response = client.post("/api/ask", json={
        "symbol": "005930", "market": "KOSPI", "name": "삼성전자", "question": "가격대는?",
    })
    assert response.status_code == 200
    analyze.assert_called_once_with("005930", "삼성전자", "KOSPI")
    answer.assert_called_once_with("가격대는?", fresh)
    assert answer.call_args.args[1]["analysis"]["close"] == 105


@pytest.mark.parametrize("symbol,name", [("000660", "SK하이닉스"), ("005930", "다른 기업명")])
def test_different_ticker_or_name_cannot_reuse_another_cached_context(qa_api, monkeypatch, symbol, name):
    module, client = qa_api
    module._remember_context(server_packet())
    fresh = server_packet(symbol, "KOSPI", name)
    analyze = Mock(return_value=fresh)
    answer = Mock(return_value={"mode": "data_only", "answer": "선택 자료 안내", "sources": []})
    monkeypatch.setattr(module, "run_analysis", analyze)
    monkeypatch.setattr(module, "answer_question", answer)
    response = client.post("/api/ask", json={
        "symbol": symbol, "market": "KOSPI", "name": name, "question": "가격대는?",
    })
    assert response.status_code == 200
    analyze.assert_called_once_with(symbol, name, "KOSPI")
    answer.assert_called_once_with("가격대는?", fresh)


def test_missing_ai_key_returns_honest_data_only_mode_through_api(qa_api, monkeypatch):
    module, client = qa_api
    packet = server_packet()
    module._remember_context(packet)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(module, "answer_question", stock_qa.answer_question)
    response = client.post("/api/ask", json={
        "symbol": "005930", "market": "KOSPI", "name": "삼성전자", "question": "매수 가격대는?",
    })
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "data_only"
    assert "AI 연결이 없어" in result["notice"]
    assert "98~102 KRW" in result["answer"]
    assert result["asof"] == packet["analysis"]["date"]
    assert result["sources"][0]["id"] == "P1"


def test_vercel_allows_explicit_stock_questions_with_configured_helper(qa_api, monkeypatch):
    module, client = qa_api
    module._remember_context(server_packet())
    monkeypatch.setattr(module, "ON_VERCEL", True)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-binding")
    monkeypatch.setattr(module, "answer_question", stock_qa.answer_question)
    upstream = Mock()
    upstream.json.return_value = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "관찰 구간은 확정 일봉을 기준으로 계산했습니다. [P1]"}]}]}
    post = Mock(return_value=upstream)
    monkeypatch.setattr(stock_qa.requests, "post", post)
    response = client.post("/api/ask", json={
        "symbol": "005930", "market": "KOSPI", "name": "삼성전자", "question": "관찰 가격대는?",
    })
    assert response.status_code == 200
    assert response.json()["mode"] == "ai"
    assert "[P1]" in response.json()["answer"]
    post.assert_called_once()
    assert post.call_args.args[0] == "https://api.openai.com/v1/responses"


def test_seventh_question_from_same_client_is_throttled_without_blocking_analysis(qa_api, monkeypatch):
    module, client = qa_api
    packet = server_packet()
    module._remember_context(packet)
    monkeypatch.setattr(module, "ON_VERCEL", True)
    answer = Mock(return_value={"mode": "ai", "answer": "자료 설명", "sources": []})
    analyze = Mock(return_value=packet)
    monkeypatch.setattr(module, "answer_question", answer)
    monkeypatch.setattr(module, "run_analysis", analyze)
    for count in range(6):
        response = client.post(f"/api/ask?attempt={count}", json={
            "symbol": "005930", "market": "KOSPI", "name": "삼성전자", "question": f"가격대 질문 {count}",
        })
        assert response.status_code == 200
    blocked = client.post("/api/ask?attempt=another", json={
        "symbol": "005930", "market": "KOSPI", "name": "삼성전자", "question": "다른 질문이면 되나요?",
    })
    assert blocked.status_code == 429
    assert answer.call_count == 6
    assert analyze.call_count == 0
    analysis = client.post("/api/analyze", json={"symbol": "005930", "market": "KOSPI", "name": "삼성전자"})
    assert analysis.status_code == 200
    analyze.assert_called_once_with("005930", "삼성전자", "KOSPI", False)
