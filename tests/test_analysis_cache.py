"""Short analysis reuse preserves original observations and isolates each stock."""
import json
from unittest.mock import Mock

import pytest

from tests.test_macro_api import install_offline_sources


@pytest.fixture(autouse=True)
def clear_stream_cache():
    import app as module
    with module._analysis_stream_cache_lock:
        module._analysis_stream_cache.clear()
    yield
    with module._analysis_stream_cache_lock:
        module._analysis_stream_cache.clear()


def stream(client, **overrides):
    request = {'symbol': '005930', 'name': '삼성전자', 'market': 'KOSPI'}
    request.update(overrides)
    response = client.post('/api/analyze/stream', json=request)
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines()]


def count_daily(monkeypatch):
    import app as module
    daily = Mock(wraps=module.fetch_daily)
    monkeypatch.setattr(module, 'fetch_daily', daily)
    return daily


def test_completed_stream_reuses_all_data_without_changing_source_timestamps(monkeypatch):
    import app as module

    client, snapshot, _ = install_offline_sources(monkeypatch)
    wrapped = {}
    for name in ('fetch_daily', 'fetch_news', 'fetch_research_mentions', 'fetch_dart_filings',
                 'fetch_financial_facts', 'fetch_naver_quote', 'fetch_macro_context'):
        wrapped[name] = Mock(wraps=getattr(module, name))
        monkeypatch.setattr(module, name, wrapped[name])
    first = stream(client)[-1]['result']
    second_events = stream(client)
    assert len(second_events) == 1
    assert second_events[0]['stage'] == 'complete'
    second = second_events[0]['result']
    assert first['cache']['hit'] is False
    assert first['cache']['stored_at']
    assert second['cache']['hit'] is True
    assert 0 <= second['cache']['age_seconds'] < 30
    assert second['cache']['stored_at'] == first['cache']['stored_at']
    assert second['meta']['fetched_at'] == first['meta']['fetched_at']
    assert second['macro']['checked_at'] == snapshot['checked_at']
    assert second['macro']['metrics'] == snapshot['metrics']
    assert second['evidence_pack']['generated_at'] == first['evidence_pack']['generated_at']
    assert all(provider.call_count == 1 for provider in wrapped.values())
    assert module._recent_context('005930', 'KOSPI', '삼성전자')['cache']['hit'] is True


def test_symbols_markets_and_company_names_do_not_share_cached_evidence(monkeypatch):
    client, _, _ = install_offline_sources(monkeypatch, market='US')
    daily = count_daily(monkeypatch)
    first = stream(client, symbol='aapl', name='Apple', market='US')[-1]['result']
    assert first['symbol'] == 'AAPL'
    canonical = stream(client, symbol='AAPL', name=' Apple ', market='US')
    assert len(canonical) == 1
    assert canonical[0]['result']['cache']['hit'] is True
    for symbol, name, market in (
        ('MSFT', 'Microsoft', 'US'), ('AAPL', 'Apple Inc.', 'US'),
        ('005930', '삼성전자', 'KOSPI'), ('005930', '삼성전자', 'KOSDAQ'),
    ):
        result = stream(client, symbol=symbol, name=name, market=market)[-1]['result']
        assert result['cache']['hit'] is False
        assert result['symbol'] == symbol
        assert result['market'] == market
    assert daily.call_count == 5


def test_returned_results_cannot_mutate_cached_evidence(monkeypatch):
    import app as module

    _, snapshot, _ = install_offline_sources(monkeypatch)
    original = snapshot['metrics']['rate']['value']
    first = list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))[-1]['result']
    first['macro']['metrics']['rate']['value'] = -100
    first['chart'][0]['close'] = -100
    second = list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))[0]['result']
    assert second['macro']['metrics']['rate']['value'] == original
    assert second['chart'][0]['close'] > 0
    second['macro']['metrics']['rate']['value'] = -200
    second['data_quality']['warnings'].append('mutated by caller')
    third = list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))[0]['result']
    assert third['macro']['metrics']['rate']['value'] == original
    assert 'mutated by caller' not in third['data_quality']['warnings']


def test_cache_expires_at_thirty_seconds_and_refresh_bypasses_it(monkeypatch):
    import app as module

    client, _, _ = install_offline_sources(monkeypatch)
    daily = count_daily(monkeypatch)
    clock = [module.time.monotonic()]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    stream(client)
    clock[0] += 29
    hit = stream(client)[0]['result']
    assert hit['cache']['hit'] is True
    assert hit['cache']['age_seconds'] == 29
    assert daily.call_count == 1
    clock[0] += 1
    expired = stream(client)[-1]['result']
    assert expired['cache']['hit'] is False
    assert daily.call_count == 2
    refreshed = stream(client, refresh=True)[-1]['result']
    assert refreshed['cache']['hit'] is False
    assert daily.call_count == 3


def test_incomplete_macro_and_ai_results_are_not_reused(monkeypatch):
    import app as module

    client, snapshot, _ = install_offline_sources(monkeypatch)
    daily = count_daily(monkeypatch)
    incomplete = dict(snapshot, eligible=False, warnings=['미국 금리 확인 필요'])
    monkeypatch.setattr(module, 'fetch_macro_context', lambda: incomplete)
    assert stream(client)[-1]['result']['cache']['stored_at'] is None
    assert stream(client)[-1]['result']['cache']['hit'] is False
    assert daily.call_count == 2
    assert not module._analysis_stream_cache
    monkeypatch.setattr(module, 'fetch_macro_context', lambda: snapshot)
    monkeypatch.setattr(module, 'summarize_with_openai', lambda bundle: 'offline AI fixture')
    ai = stream(client, with_ai=True)[-1]['result']
    assert ai['ai_summary'] == 'offline AI fixture'
    assert ai['cache']['stored_at'] is None
    assert stream(client, with_ai=True)[-1]['result']['cache']['hit'] is False
    assert daily.call_count == 4
    assert not module._analysis_stream_cache


def test_cache_has_a_fixed_memory_bound_and_oldest_stock_is_refetched(monkeypatch):
    import app as module

    client, _, _ = install_offline_sources(monkeypatch)
    daily = count_daily(monkeypatch)
    for i in range(33):
        stream(client, symbol=f'{i:06d}', name=f'종목{i}')
    assert len(module._analysis_stream_cache) == 32
    assert daily.call_count == 33
    assert stream(client, symbol='000000', name='종목0')[-1]['result']['cache']['hit'] is False
    assert daily.call_count == 34
    assert len(module._analysis_stream_cache) == 32


def test_legacy_analyze_remains_a_fresh_complete_request(monkeypatch):
    client, _, _ = install_offline_sources(monkeypatch)
    daily = count_daily(monkeypatch)
    stream(client)
    response = client.post('/api/analyze', json={'symbol': '005930', 'name': '삼성전자'})
    assert response.status_code == 200
    assert daily.call_count == 2
    assert 'cache' not in response.json()


def test_unconfigured_optional_us_quote_does_not_prevent_completed_result_cache(monkeypatch):
    import app as module
    client, _, _ = install_offline_sources(monkeypatch, market='US')
    monkeypatch.setattr(module, 'kis_configured', lambda: False)
    daily = count_daily(monkeypatch)
    first = stream(client, symbol='AAPL', name='Apple', market='US')[-1]['result']
    assert first['completion']['status'] == 'complete'
    assert 'quote' not in first['completion']['unavailable_sources']
    assert '미연결' in first['quote_error']
    second = stream(client, symbol='AAPL', name='Apple', market='US')
    assert len(second) == 1
    assert second[0]['result']['cache']['hit'] is True
    assert second[0]['result']['quote_error'] == first['quote_error']
    assert daily.call_count == 1
