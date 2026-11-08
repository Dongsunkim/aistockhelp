"""Slow evidence must end in an honest final result before a serverless deadline."""
import json
from collections import Counter
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor
from threading import Event, Lock

import pytest
import requests

from tests.test_macro_api import install_offline_sources


@pytest.fixture(autouse=True)
def clear_analysis_reuse():
    import app as module
    with module._analysis_stream_cache_lock:
        module._analysis_stream_cache.clear()
    with module._analysis_context_lock:
        module._analysis_contexts.clear()
    yield
    with module._analysis_stream_cache_lock:
        module._analysis_stream_cache.clear()
    with module._analysis_context_lock:
        module._analysis_contexts.clear()


def install_deadline_sources(monkeypatch, *, blocked=('macro', 'research'), elapsed_before_wait=0):
    """Advance virtual time only after fast sources have really returned their futures."""
    import app as module

    client, snapshot, started = install_offline_sources(monkeypatch)
    release = Event()
    waiting = {kind: Event() for kind in blocked}
    finished = {kind: Event() for kind in blocked}
    counts = Counter()
    lock = Lock()
    clock = [1000.0]
    waits = []
    providers = {
        'daily': 'fetch_daily', 'news': 'fetch_news', 'research': 'fetch_research_mentions',
        'filings': 'fetch_dart_filings', 'financials': 'fetch_financial_facts',
        'quote': 'fetch_naver_quote', 'macro': 'fetch_macro_context',
    }
    for kind, attr in providers.items():
        original = getattr(module, attr)

        def fetch(*args, _kind=kind, _original=original, **kwargs):
            with lock:
                counts[_kind] += 1
            try:
                value = _original(*args, **kwargs)
                if _kind in waiting:
                    waiting[_kind].set()
                    assert release.wait(timeout=10), 'test did not release the blocked provider'
                return value
            finally:
                if _kind in finished:
                    finished[_kind].set()

        monkeypatch.setattr(module, attr, fetch)

    original_start = module._start_analysis_jobs
    real_wait = module.wait

    def start(*args, **kwargs):
        pool, jobs = original_start(*args, **kwargs)
        fast = [future for kind, future in jobs.items() if future is not None and kind not in blocked]
        _, pending = real_wait(fast, timeout=5, return_when=ALL_COMPLETED)
        assert not pending, 'offline fast providers did not finish'
        assert all(event.wait(timeout=5) for event in waiting.values())
        clock[0] += elapsed_before_wait
        return pool, jobs

    def budget_wait(jobs, timeout=None, return_when=None):
        done, pending = real_wait(jobs, timeout=0, return_when=return_when)
        if done:
            return done, pending
        waits.append(timeout)
        # A bounded implementation always supplies a wait timeout. Advancing a
        # full budget for legacy unbounded waits lets this regression fail safely.
        clock[0] += 12 if timeout is None else max(0, timeout)
        return real_wait(jobs, timeout=0, return_when=return_when)

    monkeypatch.setattr(module, 'ANALYSIS_TIMEOUT_SECONDS', 12)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, '_start_analysis_jobs', start)
    monkeypatch.setattr(module, 'wait', budget_wait)
    return client, snapshot, started, release, finished, counts, clock, waits


def assert_sources_still_waiting(finished):
    assert not any(event.is_set() for event in finished.values())


def release_sources(release, finished):
    release.set()
    assert all(event.wait(timeout=5) for event in finished.values())


def test_stream_emits_terminal_partial_result_before_slow_macro_and_research_finish(monkeypatch):
    import app as module

    _, _, started, release, finished, counts, _, _ = install_deadline_sources(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            events = caller.submit(
                lambda: list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))
            ).result(timeout=5)
            assert_sources_still_waiting(finished)
            assert events[0]['stage'] == 'partial'
            final = events[-1]
            assert final['stage'] == 'complete'
            assert final['pending_sources'] == []
            result = final['result']
            completion = result['completion']
            assert completion['status'] == 'partial'
            assert set(completion['timed_out_sources']) == {'macro', 'research'}
            assert {'macro', 'research'} <= set(completion['unavailable_sources'])
            assert completion['message']
            assert '조회 시간 초과' in result['research_error']
            assert '조회 시간 초과' in result['macro_error']
            assert result['macro']['eligible'] is False
            assert any('조회 시간 초과' in warning for warning in result['macro']['warnings'])
            assert result['news'][0]['source'] == '네이버증권 뉴스'
            assert len(result['financials']) == 3
            assert result['data_quality']['financials_eligible'] is True
            assert result['quote']['price'] == 117
            assert result['analysis']['levels']['entry_low'] > 0
            assert result['analysis']['confirmed'] is False
            assert result['data_quality']['verified_for_trade'] is False
            assert result['cache']['stored_at'] is None
            assert not module._analysis_stream_cache
            assert started == {'daily', 'news', 'research', 'filings', 'financials', 'quote', 'macro'}
            assert counts == Counter({kind: 1 for kind in started})
            remembered = module._recent_context('005930', 'KOSPI', '삼성전자')
            assert remembered['completion'] == completion
        finally:
            release_sources(release, finished)


def test_legacy_json_analysis_obeys_the_same_total_deadline(monkeypatch):
    import app as module

    _, _, _, release, finished, _, _, _ = install_deadline_sources(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            result = caller.submit(module.run_analysis, '005930', '삼성전자', 'KOSPI').result(timeout=5)
            assert_sources_still_waiting(finished)
            assert result['completion']['status'] == 'partial'
            assert set(result['completion']['timed_out_sources']) == {'macro', 'research'}
            assert result['news'][0]['source'] == '네이버증권 뉴스'
            assert result['data_quality']['financials_eligible'] is True
            assert result['macro']['eligible'] is False
            assert result['analysis']['confirmed'] is False
        finally:
            release_sources(release, finished)


@pytest.mark.parametrize('mode', ['stream', 'json'])
def test_daily_timeout_never_invents_a_price_or_waits_for_the_provider(monkeypatch, mode):
    import app as module

    _, _, _, release, finished, _, _, _ = install_deadline_sources(monkeypatch, blocked=('daily',))
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            if mode == 'stream':
                events = caller.submit(
                    lambda: list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))
                ).result(timeout=5)
                assert len(events) == 1
                assert events[0]['stage'] == 'error'
                assert events[0]['result'] is None
                assert events[0]['pending_sources'] == []
                assert '조회 시간 초과' in events[0]['detail']
                assert 'http' not in json.dumps(events[0])
            else:
                future = caller.submit(module.run_analysis, '005930', '삼성전자', 'KOSPI')
                with pytest.raises(requests.Timeout, match='조회 시간 초과'):
                    future.result(timeout=5)
            assert_sources_still_waiting(finished)
            assert not module._analysis_stream_cache
            assert module._recent_context('005930', 'KOSPI', '삼성전자') is None
        finally:
            release_sources(release, finished)


def test_daily_elapsed_time_consumes_the_shared_budget(monkeypatch):
    import app as module

    _, _, _, release, finished, _, clock, waits = install_deadline_sources(
        monkeypatch, blocked=('macro',), elapsed_before_wait=9
    )
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            events = caller.submit(
                lambda: list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))
            ).result(timeout=5)
            assert events[-1]['result']['completion']['timed_out_sources'] == ['macro']
            assert clock[0] == pytest.approx(1012)
            assert waits and all(timeout is not None and timeout <= 3 for timeout in waits)
            assert_sources_still_waiting(finished)
        finally:
            release_sources(release, finished)


def test_successful_final_result_has_complete_metadata_and_remains_cacheable(monkeypatch):
    import app as module

    _, snapshot, _ = install_offline_sources(monkeypatch)
    events = list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))
    result = events[-1]['result']
    assert events[-1]['stage'] == 'complete'
    assert result['completion']['status'] == 'complete'
    assert result['completion']['timed_out_sources'] == []
    assert result['completion']['unavailable_sources'] == []
    assert result['macro'] == snapshot
    assert result['cache']['stored_at']
    cached = list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))
    assert len(cached) == 1
    assert cached[0]['result']['cache']['hit'] is True
    assert cached[0]['result']['completion'] == result['completion']


def test_provider_failures_finish_with_explicit_unavailable_evidence(monkeypatch):
    import app as module

    _, _, _ = install_offline_sources(monkeypatch)

    def fail(*args, **kwargs):
        raise ValueError('https://secret.invalid/?api_key=DO_NOT_EXPOSE')

    monkeypatch.setattr(module, 'fetch_financial_facts', fail)
    monkeypatch.setattr(module, 'fetch_macro_context', fail)
    events = list(module.iter_analysis_events('005930', '삼성전자', 'KOSPI'))
    assert events[-1]['stage'] == 'complete'
    result = events[-1]['result']
    assert result['completion']['status'] == 'partial'
    assert result['completion']['timed_out_sources'] == []
    assert {'financials', 'macro'} <= set(result['completion']['unavailable_sources'])
    assert result['financials_error']
    assert result['macro']['eligible'] is False
    assert result['analysis']['confirmed'] is False
    assert result['data_quality']['financials_eligible'] is False
    assert 'DO_NOT_EXPOSE' not in json.dumps(events)
    assert not module._analysis_stream_cache
