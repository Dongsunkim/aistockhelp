from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from monitor_evidence import evaluate_analysis_evidence, build_evidence_for_stock

NOW = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)


def evidence_bundle():
    return {
        'symbol': '005930', 'market': 'KOSPI',
        'analysis': {
            'date': '2026-11-09', 'red_flags': [],
            'levels': {'entry_low': 100, 'entry_high': 105, 'stop': 90, 'target_resistance': 140},
            'conditions': [{'id': key, 'pass': True} for key in ('trend', 'rsi', 'volatility', 'volume')]
                          + [{'id': 'pullback', 'pass': False}, {'id': 'rr', 'pass': False}],
            'evidence_checks': [{'name': key, 'pass': True} for key in
                                ('최근 72시간 내 뉴스', '위험 키워드 기사 없음', '최근 120일 내 공식 공시')],
        },
        'data_quality': {'price_levels_eligible': True, 'financials_eligible': True},
        'financials': [{'name': key, 'value': 100, 'unit': 'KRW', 'period': '2026.Q3', 'basis': '연결',
                        'source': '네이버증권 기업실적분석', 'basis_verified': True, 'is_estimate': False}
                       for key in ('매출액', '영업이익', '순이익')],
        'news': [{'title': '사업 소식', 'age_hours': 1}],
        'filings': [{'title': '분기보고서', 'age_days': 1, 'source': '금융감독원 DART'}],
        'macro': {'eligible': True, 'risk_flags': [], 'checked_at': NOW.isoformat(),
                  'metrics': {key: {'value': 1, 'change_pp': 0, 'change_pct': 0, 'asof': '2026-11-06',
                                    'source': 'FRED', 'unit': '%', 'url': 'https://example.org/data'}
                              for key in ('rate', 'usdkrw', 'inflation', 'sp500', 'nasdaq', 'sox', 'nvda', 'aapl', 'msft')}},
    }


def test_live_entry_can_be_checked_after_daily_close_was_outside_zone():
    result = evaluate_analysis_evidence(evidence_bundle(), now=NOW)
    assert result['eligible'] is True
    assert result['levels'] == {'entry_low': 100, 'entry_high': 105, 'stop': 90, 'resistance': 140}


@pytest.mark.parametrize('failure', ['macro_missing', 'macro_risk', 'macro_future', 'macro_stale', 'macro_badvalue',
                                     'financial_loss', 'financial_estimate', 'financial_duplicate', 'financial_basis',
                                     'news_old', 'news_risk', 'filings_wrongsource', 'daily_old', 'daily_future',
                                     'technical_fail', 'technical_duplicate', 'levels_inverted', 'levels_nan'])
def test_missing_or_unsafe_evidence_blocks_buy(failure):
    data = evidence_bundle()
    if failure == 'macro_missing': del data['macro']['metrics']['nvda']
    elif failure == 'macro_risk': data['macro']['risk_flags'] = ['시장 변동성']
    elif failure == 'macro_future': data['macro']['checked_at'] = (NOW + timedelta(seconds=1)).isoformat()
    elif failure == 'macro_stale': data['macro']['checked_at'] = (NOW - timedelta(minutes=16)).isoformat()
    elif failure == 'macro_badvalue': data['macro']['metrics']['rate']['value'] = float('inf')
    elif failure == 'financial_loss': data['financials'][1]['value'] = -1
    elif failure == 'financial_estimate': data['financials'][1]['is_estimate'] = True
    elif failure == 'financial_duplicate': data['financials'].append(deepcopy(data['financials'][0]))
    elif failure == 'financial_basis': data['financials'][0]['basis'] = '별도'
    elif failure == 'news_old': data['news'][0]['age_hours'] = 73
    elif failure == 'news_risk': data['analysis']['red_flags'] = ['횡령']
    elif failure == 'filings_wrongsource': data['filings'][0]['source'] = '기사'
    elif failure == 'daily_old': data['analysis']['date'] = '2026-11-06'
    elif failure == 'daily_future': data['analysis']['date'] = '2026-11-10'
    elif failure == 'technical_fail': data['analysis']['conditions'][0]['pass'] = False
    elif failure == 'technical_duplicate': data['analysis']['conditions'].append({'id': 'trend', 'pass': True})
    elif failure == 'levels_inverted': data['analysis']['levels']['stop'] = 101
    elif failure == 'levels_nan': data['analysis']['levels']['entry_low'] = float('nan')
    result = evaluate_analysis_evidence(data, now=NOW)
    assert result['eligible'] is False
    assert result['blocked_reasons']


def test_worker_evidence_cache_keeps_original_timestamp_and_is_copied(monkeypatch):
    import app
    import monitor_evidence as module
    module._cache.clear()
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    calls = []
    monkeypatch.setattr(app, 'run_analysis', lambda *args, **kwargs: calls.append(args) or evidence_bundle())
    monkeypatch.setattr(module, 'evaluate_analysis_evidence', lambda bundle: evaluate_analysis_evidence(bundle, now=NOW))
    first = build_evidence_for_stock('005930', 'KOSPI', '삼성전자')
    first['levels']['entry_low'] = 999
    clock[0] += 100
    second = build_evidence_for_stock('005930', 'KOSPI', '삼성전자')
    assert second['checked_at'] == first['checked_at']
    assert second['levels']['entry_low'] == 100
    assert len(calls) == 1
    clock[0] += 301
    build_evidence_for_stock('005930', 'KOSPI', '삼성전자')
    assert len(calls) == 2
    module._cache.clear()


def test_blocked_evidence_retries_soon_instead_of_waiting_five_minutes(monkeypatch):
    import app
    import monitor_evidence as module
    module._cache.clear()
    clock = [100.]
    calls = []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    data = evidence_bundle()
    data['macro']['risk_flags'] = ['변동성 주의']
    monkeypatch.setattr(app, 'run_analysis', lambda *args, **kwargs: calls.append(args) or data)
    monkeypatch.setattr(module, 'evaluate_analysis_evidence', lambda bundle: evaluate_analysis_evidence(bundle, now=NOW))
    assert build_evidence_for_stock('005930', 'KOSPI')['eligible'] is False
    clock[0] += 31
    data['macro']['risk_flags'] = []
    assert build_evidence_for_stock('005930', 'KOSPI')['eligible'] is True
    assert len(calls) == 2
    module._cache.clear()
