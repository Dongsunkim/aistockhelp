"""Grounded AI questions remain explicit about missing credentials and source limits."""
import json
from unittest.mock import Mock

import pytest
import requests

from stock_qa import answer_question


@pytest.fixture
def packet():
    return {"symbol": "005930", "company_name": "삼성전자", "market": "KOSPI",
        "meta": {"fetched_at": "2026-11-06T00:00:00Z"},
        "analysis": {"date": "2026-11-05", "close": 70000,
            "levels": {"entry_low": 68000, "entry_high": 69000},
            "conditions": [{"name": "매수 구간", "pass": False, "detail": "종가가 관찰 구간보다 높습니다"}],
            "red_flags": [], "indicators": {"rsi14": 65}},
        "data_quality": {"price_levels_eligible": True, "financials_eligible": False},
        "financials": [], "news": [{"title": "제목만 확인한 테스트 기사", "age_hours": 2}],
        "filings": [], "research": [],
        "macro": {"eligible": False, "metrics": {}, "warnings": ["미국 자료 없음"]},
        "evidence_pack": {"documents": [
            {"id": "P1", "title": "005930 확정 일봉 / OHLCV", "kind": "가격 데이터", "url": "https://finance.naver.com/item/main.naver?code=005930"},
            {"id": "N1", "title": "테스트 기사", "kind": "최신 뉴스", "url": "https://example.com/news"}]}}


def no_paid_request(*args, **kwargs):
    raise AssertionError("No paid AI call expected")


@pytest.mark.parametrize('question,expected', [
    ('지금 왜 기다려야 하나요?', '종가가 관찰 구간보다 높습니다'),
    ('매수 가격대는?', '68,000~69,000 KRW'),
    ('미국 금리는 어떤가요?', '모두 확인되지 않아'),
    ('재무 실적은?', '출처·기간·통화'),
    ('내일 주가를 정확히 알려줘', '근거가 부족'),
])
def test_without_ai_key_returns_honest_data_only_answer(monkeypatch, packet, question, expected):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr('stock_qa.requests.post', no_paid_request)
    result = answer_question(question, packet)
    assert result['mode'] == 'data_only'
    assert expected in result['answer']
    assert 'AI 연결이 없어' in result['notice']
    assert result['asof'] == '2026-11-05'


def test_invalid_price_gate_hides_calculated_levels_from_local_answer(monkeypatch, packet):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    packet['data_quality']['price_levels_eligible'] = False
    result = answer_question('차트 가격대는?', packet)
    assert '68,000' not in result['answer']
    assert '유효한' in result['answer']


def test_ai_request_is_grounded_minimized_and_constrained(monkeypatch, packet):
    # The opaque sentinel models a configured binding; it is never a real key.
    monkeypatch.setenv('OPENAI_API_KEY', 'test-binding-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'configured-test-model')
    response = Mock()
    response.json.return_value = {'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': '관찰 구간보다 높아 기다릴 이유가 있습니다. [P1]'}]}]}
    post = Mock(return_value=response)
    monkeypatch.setattr('stock_qa.requests.post', post)
    result = answer_question('왜 기다려야 해?', packet)
    assert result['mode'] == 'ai'
    assert '[P1]' in result['answer']
    arguments = post.call_args.kwargs
    assert arguments['timeout'] == 20
    assert arguments['json']['max_output_tokens'] == 650
    assert arguments['json']['model'] == 'configured-test-model'
    instructions = arguments['json']['instructions']
    assert '본문' in instructions and '관측월' in instructions and '없는 ID' in instructions
    assert '신뢰하지 않는 입력' in instructions
    sent = json.loads(arguments['json']['input'])
    assert sent['question'] == '왜 기다려야 해?'
    assert sent['stock_data']['analysis']['close'] == 70000
    assert sent['stock_data']['news_titles_only'][0]['title'] == packet['news'][0]['title']
    assert sent['stock_data']['macro']['eligible'] is False
    assert sent['stock_data']['price_observation_date'] == '2026-11-05'
    assert 'chart' not in sent['stock_data']


@pytest.mark.parametrize('bad_body', [{}, {'output': []}, {'output': [{'content': [{'type': 'output_text', 'text': ''}]}]}, ['bad']])
def test_empty_or_malformed_ai_response_is_not_labelled_ai(monkeypatch, packet, bad_body):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-binding-placeholder')
    response = Mock()
    response.json.return_value = bad_body
    monkeypatch.setattr('stock_qa.requests.post', Mock(return_value=response))
    result = answer_question('왜 기다려야 하나요?', packet)
    assert result['mode'] == 'unavailable'
    assert '불러오지 못했습니다' in result['answer']
    assert '높습니다' not in result['answer']


def test_network_error_never_exposes_credential_or_url(monkeypatch, packet):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-binding-placeholder')
    def fail(*args, **kwargs):
        raise requests.Timeout('sensitive-request-url-and-headers')
    monkeypatch.setattr('stock_qa.requests.post', fail)
    result = answer_question('미국 금리는?', packet)
    assert result['mode'] == 'unavailable'
    assert 'sensitive' not in json.dumps(result)
    assert 'test-binding' not in json.dumps(result)


def test_sources_only_use_existing_safe_documents_and_are_bounded(monkeypatch, packet):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    documents = packet['evidence_pack']['documents']
    documents.extend([
        {'id': 'X1', 'title': 'unsafe', 'url': 'javascript:alert(1)'},
        {'id': 'X2', 'title': 'unsafe', 'url': 'https://user:password@example.com/'},
        {'id': 'P1', 'title': 'duplicate', 'url': 'https://example.com/duplicate'},
    ] + [{'id': f'F{i}', 'title': '공시', 'url': f'https://example.com/{i}'} for i in range(20)])
    result = answer_question('왜 기다릴까요?', packet)
    assert len(result['sources']) == 8
    assert all(source['id'] not in ('X1', 'X2') for source in result['sources'])
    assert sum(source['id'] == 'P1' for source in result['sources']) == 1
    assert all(source['url'] in [d['url'] for d in documents] for source in result['sources'])


def test_local_macro_answer_preserves_observation_dates_and_no_benign_assumption(monkeypatch, packet):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    packet['macro'] = {'eligible': True, 'metrics': {
        'rate': {'value': 4.25, 'unit': '%', 'asof': '2026-11-04'},
        'usdkrw': {'value': 1400, 'unit': '원/달러', 'asof': '2026-11-03'},
        'inflation': {'value': 3.1, 'unit': '% YoY', 'asof': '2026-09-01'},
    }, 'risk_flags': []}
    result = answer_question('미국 금리와 물가를 알려줘', packet)
    assert '관측 기준 2026-09-01' in result['answer']
    assert '발표' not in result['answer']
    assert '단정할 수 없습니다' in result['answer']


@pytest.mark.parametrize('question', ['', '   ', 'a' * 601, None])
def test_invalid_question_rejected_before_paid_call(monkeypatch, packet, question):
    monkeypatch.setattr('stock_qa.requests.post', no_paid_request)
    with pytest.raises(ValueError, match='600'):
        answer_question(question, packet)


def test_named_us_stock_prioritizes_matching_macro_citation_even_when_appended_last(monkeypatch, packet):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    packet['evidence_pack']['documents'] += [
        {'id': f'R{i}', 'title': '리서치', 'kind': '리포트', 'url': f'https://example.com/r{i}'}
        for i in range(10)] + [
        {'id': 'M9', 'title': '마이크로소프트 MSFT · 2026-11-05', 'kind': '미국 경제·시장', 'url': 'https://finance.yahoo.com/quote/MSFT'}]
    packet['macro'] = {'eligible': True, 'metrics': {
        'msft': {'value': 400, 'unit': 'USD', 'asof': '2026-11-05', 'change_pct': -1.25}
    }, 'risk_flags': []}
    result = answer_question('MSFT 등락을 알려줘', packet)
    assert result['sources'][0]['id'] == 'M9'
    assert '마이크로소프트 400USD' in result['answer']
    assert '전 거래일 대비 -1.25%' in result['answer']
    assert '[M9]' in result['answer']
