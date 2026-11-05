"""Progressive analysis must reveal prices before independent evidence is ready."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
import requests

from tests.test_macro_api import install_offline_sources


@pytest.fixture(autouse=True)
def clear_stream_cache():
    import app as module
    with module._analysis_stream_cache_lock:
        module._analysis_stream_cache.clear()
    yield
    with module._analysis_stream_cache_lock:
        module._analysis_stream_cache.clear()


def test_first_ndjson_line_does_not_wait_for_news_or_macro(monkeypatch):
    import app as module

    _, _, started = install_offline_sources(monkeypatch, rendezvous=Barrier(7))
    release = Event()
    waiting = {kind: Event() for kind in ('news', 'macro')}
    finished = {kind: Event() for kind in waiting}
    for kind, attr in (('news', 'fetch_news'), ('macro', 'fetch_macro_context')):
        original = getattr(module, attr)

        def blocked(*args, _kind=kind, _original=original, **kwargs):
            try:
                value = _original(*args, **kwargs)
                waiting[_kind].set()
                assert release.wait(timeout=10)
                return value
            finally:
                finished[_kind].set()

        monkeypatch.setattr(module, attr, blocked)

    lines = module._analysis_ndjson(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))
    with ThreadPoolExecutor(max_workers=1) as caller:
        first = caller.submit(next, lines)
        try:
            event = json.loads(first.result(timeout=5))
            assert all(signal.wait(timeout=5) for signal in waiting.values())
            assert started == {'daily', 'news', 'research', 'filings', 'financials', 'quote', 'macro'}
            assert not any(signal.is_set() for signal in finished.values())
            assert event['stage'] == 'partial'
            assert {'news', 'macro'} <= set(event['pending_sources'])
            bundle = event['result']
            assert {'analysis', 'meta', 'data_quality', 'evidence_pack', 'chart', 'news',
                    'research', 'filings', 'financials', 'macro', 'quote_error'} <= set(bundle)
            assert bundle['data_quality']['price_levels_eligible'] is True
            assert bundle['data_quality']['financials_eligible'] is False
            assert bundle['data_quality']['verified_for_trade'] is False
            assert bundle['macro']['eligible'] is False
            assert bundle['macro']['checked_at'] is None
            assert bundle['analysis']['confirmed'] is False
            assert bundle['news'] == []
            assert len(bundle['chart']) == 120
            assert bundle['cache']['stored_at'] is None
            lines.close()
            assert not module._analysis_stream_cache
        finally:
            release.set()
            assert all(signal.wait(timeout=5) for signal in finished.values())


def test_core_evidence_finishes_before_optional_sources_and_matches_full_analysis(monkeypatch):
    import app as module

    client, snapshot, _ = install_offline_sources(monkeypatch)
    optional_release = Event()
    finished = {kind: Event() for kind in ('research', 'quote')}
    for kind, attr in (('research', 'fetch_research_mentions'), ('quote', 'fetch_naver_quote')):
        original = getattr(module, attr)

        def blocked(*args, _kind=kind, _original=original, **kwargs):
            try:
                assert optional_release.wait(timeout=10)
                return _original(*args, **kwargs)
            finally:
                finished[_kind].set()

        monkeypatch.setattr(module, attr, blocked)

    events = module.iter_analysis_events('005930', '삼성전자', 'KOSPI')
    core = {'news', 'filings', 'financials', 'macro'}
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            assert next(events)['stage'] == 'partial'

            def until_core_ready():
                for event in events:
                    if not core.intersection(event['pending_sources']):
                        return event

            ready = caller.submit(until_core_ready).result(timeout=5)
            assert ready['stage'] == 'partial'
            assert set(ready['pending_sources']) == {'research', 'quote'}
            assert ready['result']['macro'] == snapshot
            assert ready['result']['data_quality']['financials_eligible'] is True
            assert not any(signal.is_set() for signal in finished.values())
            optional_release.set()
            remaining = list(events)
        finally:
            optional_release.set()
            events.close()
            assert all(signal.wait(timeout=5) for signal in finished.values())
    complete = remaining[-1]
    assert complete['stage'] == 'complete'
    assert complete['pending_sources'] == []
    result = complete['result']
    full = client.post('/api/analyze', json={'symbol': '005930', 'name': '삼성전자'}).json()
    for key in ('analysis', 'meta', 'chart', 'macro', 'data_quality', 'news', 'filings',
                'research', 'financials', 'quote'):
        assert result[key] == full[key]
    assert result['evidence_pack']['documents'] == full['evidence_pack']['documents']
    assert result['analysis']['confirmed'] is False
    assert result['data_quality']['verified_for_trade'] is False
    assert module._recent_context('005930', 'KOSPI', '삼성전자')['macro'] == snapshot


def test_stream_http_protocol_and_invalid_symbol(monkeypatch):
    client, _, _ = install_offline_sources(monkeypatch)
    response = client.post('/api/analyze/stream', json={'symbol': '005930', 'name': '삼성전자'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/x-ndjson')
    assert response.headers['cache-control'] == 'no-store'
    lines = response.text.splitlines()
    assert len(lines) >= 2
    events = list(map(json.loads, lines))
    assert events[0]['stage'] == 'partial'
    assert events[-1]['stage'] == 'complete'
    assert all(event['stage'] in ('partial', 'complete') for event in events)
    assert events[-1]['result']['meta']['source'] == 'NAVER'
    assert client.post('/api/analyze/stream', json={'symbol': 'BAD'}).status_code == 422


def test_fatal_daily_error_is_safe_and_does_not_wait_for_other_sources(monkeypatch):
    import app as module

    _, _, _ = install_offline_sources(monkeypatch)
    release = Event()
    waiting = Event()
    finished = Event()

    def bad_daily(*args, **kwargs):
        raise requests.RequestException('https://secret.invalid/?api_key=DO_NOT_EXPOSE')

    def blocked_news(*args, **kwargs):
        try:
            waiting.set()
            assert release.wait(timeout=10)
            return []
        finally:
            finished.set()

    monkeypatch.setattr(module, 'fetch_daily', bad_daily)
    monkeypatch.setattr(module, 'fetch_news', blocked_news)
    events = module.iter_analysis_events('005930', '', 'KOSPI')
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            future = caller.submit(next, events)
            assert waiting.wait(timeout=5)
            event = future.result(timeout=5)
            assert event['stage'] == 'error'
            assert event['result'] is None
            assert event['pending_sources'] == []
            assert 'DO_NOT_EXPOSE' not in json.dumps(event)
            assert 'secret.invalid' not in json.dumps(event)
            assert not finished.is_set()
            events.close()
            assert not module._analysis_stream_cache
        finally:
            release.set()
            assert finished.wait(timeout=5)


def test_failed_financial_and_macro_sources_complete_without_false_confirmation(monkeypatch):
    import app as module

    client, _, _ = install_offline_sources(monkeypatch)

    def fail(*args, **kwargs):
        raise ValueError('provider credential=DO_NOT_EXPOSE')

    monkeypatch.setattr(module, 'fetch_financial_facts', fail)
    monkeypatch.setattr(module, 'fetch_macro_context', fail)
    response = client.post('/api/analyze/stream', json={'symbol': '005930'})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]['stage'] == 'complete'
    result = events[-1]['result']
    assert result['financials'] == []
    assert result['financials_error']
    assert result['data_quality']['financials_eligible'] is False
    assert result['macro']['eligible'] is False
    assert result['macro']['warnings']
    assert result['analysis']['confirmed'] is False
    assert result['data_quality']['verified_for_trade'] is False
    assert 'DO_NOT_EXPOSE' not in response.text
    assert result['cache']['stored_at'] is None
    assert not module._analysis_stream_cache


def test_stream_uses_existing_public_analysis_limit(monkeypatch):
    import app as module

    client, _, _ = install_offline_sources(monkeypatch)
    monkeypatch.setattr(module, 'ON_VERCEL', True)
    with module._limiter_lock:
        module._limiter.clear()
        module._limiter['testclient'] = [module.time.monotonic()] * 24
    try:
        response = client.post('/api/analyze/stream', json={'symbol': '005930'})
        assert response.status_code == 429
        assert '분석 요청' in response.json()['detail']
    finally:
        with module._limiter_lock:
            module._limiter.clear()


def test_http_disconnect_closes_the_source_pool_without_waiting_for_news(monkeypatch):
    import asyncio
    import app as module
    from starlette.requests import ClientDisconnect

    _, _, _ = install_offline_sources(monkeypatch)
    release = Event()
    news_finished = Event()
    shutdown = Event()
    original = module.fetch_news

    def blocked(*args, **kwargs):
        try:
            assert release.wait(timeout=10)
            return original(*args, **kwargs)
        finally:
            news_finished.set()

    class TrackedPool(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            assert wait is False
            assert cancel_futures is True
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
            shutdown.set()

    monkeypatch.setattr(module, 'fetch_news', blocked)
    monkeypatch.setattr(module, 'ThreadPoolExecutor', TrackedPool)
    response = module.analyze_stream(module.AnalyzeReq(symbol='005930', name='삼성전자'))
    sent = []

    async def send(message):
        sent.append(message)
        if message['type'] == 'http.response.body':
            raise OSError('client disconnected')

    async def receive():
        return {'type': 'http.disconnect'}

    try:
        with pytest.raises(ClientDisconnect):
            asyncio.run(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send))
        body = next(item['body'] for item in sent if item['type'] == 'http.response.body')
        assert json.loads(body)['stage'] == 'partial'
        assert shutdown.wait(timeout=5)
        assert not news_finished.is_set()
        assert not module._analysis_stream_cache
    finally:
        release.set()
        assert news_finished.wait(timeout=5)


def test_disconnect_interrupts_an_active_wait_for_a_slow_source(monkeypatch):
    import asyncio
    import anyio
    import app as module

    _, _, _ = install_offline_sources(monkeypatch)
    release = Event()
    news_finished = Event()
    waiting_for_news = Event()
    sent_first_body = Event()
    shutdown = Event()
    original_done = module._analysis_done

    def blocked_news(*args, **kwargs):
        try:
            assert release.wait(timeout=10)
            return []
        finally:
            news_finished.set()

    def track_wait(jobs, cancelled):
        if sent_first_body.is_set() and not any(job.done() for job in jobs):
            waiting_for_news.set()
        return original_done(jobs, cancelled)

    class TrackedPool(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
            shutdown.set()

    monkeypatch.setattr(module, 'fetch_news', blocked_news)
    monkeypatch.setattr(module, '_analysis_done', track_wait)
    monkeypatch.setattr(module, 'ThreadPoolExecutor', TrackedPool)
    response = module.analyze_stream(module.AnalyzeReq(symbol='005930'))

    async def send(message):
        if message['type'] == 'http.response.body':
            sent_first_body.set()

    async def receive():
        assert await anyio.to_thread.run_sync(waiting_for_news.wait, 5)
        return {'type': 'http.disconnect'}

    try:
        asyncio.run(response({'type': 'http', 'asgi': {'spec_version': '2.0'}}, receive, send))
        assert waiting_for_news.is_set()
        assert shutdown.wait(timeout=5)
        assert not news_finished.is_set()
        assert not module._analysis_stream_cache
    finally:
        release.set()
        assert news_finished.wait(timeout=5)
