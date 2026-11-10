"""Build monitoring gates from server-collected evidence, never browser claims.

Daily trend indicators remain daily observations. Price entry and risk/reward
are checked again against a fresh quote by alert_engine at trigger time.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import threading
import time
from copy import deepcopy
from zoneinfo import ZoneInfo

_CACHE_SECONDS = 300
_cache: dict = {}
_lock = threading.Lock()
_MACRO_IDS = ('rate', 'usdkrw', 'inflation', 'sp500', 'nasdaq', 'sox', 'nvda', 'aapl', 'msft')


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _recent_stamp(value, now, seconds=900):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.utcoffset() is not None and 0 <= (now - stamp).total_seconds() <= seconds
    except (AttributeError, TypeError, ValueError):
        return False


def evaluate_analysis_evidence(bundle: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('검증 시각에는 시간대가 필요합니다.')
    analysis = bundle.get('analysis') or {}
    quality = bundle.get('data_quality') or {}
    market = bundle.get('market')
    levels = analysis.get('levels') or {}
    conditions = analysis.get('conditions') or []
    checks = analysis.get('evidence_checks') or []
    passed = lambda name: any(c.get('name') == name and c.get('pass') is True for c in checks)

    # During a domestic trading session the latest completed daily bar should
    # be the preceding weekday. Unmodelled exchange holidays fail closed.
    previous = now.astimezone(ZoneInfo('Asia/Seoul')).date() - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    try:
        latest = date.fromisoformat(str(analysis.get('date'))[:10])
        daily_ready = market in ('KOSPI', 'KOSDAQ') and latest == previous
    except ValueError:
        daily_ready = False
    required = ('trend', 'rsi', 'volatility', 'volume')
    technical = daily_ready and quality.get('price_levels_eligible') is True and all(
        len(matches := [c for c in conditions if c.get('id') == key]) == 1
        and matches[0].get('pass') is True for key in required)
    prices = [levels.get(key) for key in ('entry_low', 'entry_high', 'stop', 'target_resistance')]
    levels_ready = all(_finite(v) for v in prices)
    if levels_ready:
        low, high, stop, resistance = prices
        levels_ready = 0 < stop < low <= high < resistance
    technical = technical and levels_ready

    facts = bundle.get('financials') or []
    selected = [[f for f in facts if f.get('name') == key] for key in ('매출액', '영업이익', '순이익')]
    financial = quality.get('financials_eligible') is True and not bundle.get('financials_error') and all(len(f) == 1 for f in selected)
    if financial:
        accounts = [f[0] for f in selected]
        financial = all(_finite(f.get('value')) and f['value'] >= 0 and f.get('unit') == 'KRW'
                        and f.get('period') and f.get('basis') and (
                            f.get('source') == 'OpenDART 주요 재무계정' or (
                                f.get('source') == '네이버증권 기업실적분석'
                                and f.get('basis_verified') is True and f.get('is_estimate') is False))
                        for f in accounts)
        financial = financial and len({f.get('period') for f in accounts}) == 1 and len({f.get('basis') for f in accounts}) == 1

    news = not bundle.get('news_error') and passed('최근 72시간 내 뉴스') and passed('위험 키워드 기사 없음') and not analysis.get('red_flags') and any(
        n.get('title') and _finite(n.get('age_hours')) and 0 <= n['age_hours'] <= 72
        for n in bundle.get('news') or [])
    filings = not bundle.get('filings_error') and passed('최근 120일 내 공식 공시') and any(
        f.get('title') and f.get('source') == '금융감독원 DART' and _finite(f.get('age_days'))
        and 0 <= f['age_days'] <= 120 for f in bundle.get('filings') or [])

    macro_data = bundle.get('macro') or {}
    metrics = macro_data.get('metrics') or {}
    macro = macro_data.get('eligible') is True and macro_data.get('risk_flags') == [] and not bundle.get('macro_error') and _recent_stamp(macro_data.get('checked_at'), now)
    for key in _MACRO_IDS:
        metric = metrics.get(key) or {}
        change = 'change_pp' if key in ('rate', 'inflation') else 'change_pct'
        macro = macro and _finite(metric.get('value')) and _finite(metric.get(change)) and all(
            isinstance(metric.get(field), str) and metric[field].strip() for field in ('asof', 'source', 'unit', 'url')) and metric['url'].startswith('https://')
    gates = {'technical': bool(technical), 'financial': bool(financial), 'news': bool(news), 'filings': bool(filings), 'macro': bool(macro)}
    reasons = {'technical': '최근 확정 일봉·추세·과열·변동성·거래량 조건 미충족',
               'financial': '검증된 동일 기간 실적 미확인 또는 적자', 'news': '최근 기사 제목 확인 부족 또는 위험 문구 발견',
               'filings': '최근 DART 공식 공시 확인 부족', 'macro': '미국 금리·환율·물가·주요 주식 확인 부족 또는 주의 신호'}
    return {**gates, 'eligible': all(gates.values()), 'checked_at': now.isoformat(),
            'symbol': bundle.get('symbol'), 'market': market,
            'levels': {'entry_low': levels.get('entry_low'), 'entry_high': levels.get('entry_high'),
                       'stop': levels.get('stop'), 'resistance': levels.get('target_resistance')},
            'blocked_reasons': [reasons[k] for k, ok in gates.items() if not ok],
            'summary': '일봉·실적·기사/공시 제목·미국 경제 기준의 관찰 조건입니다. 기사·공시 원문 및 기업가치 검증은 별도입니다.'}


def build_evidence_for_stock(symbol: str, market: str, name: str = '') -> dict:
    """Reuse evidence briefly without refreshing its original check timestamp."""
    from stock_lookup import resolve_stock
    identity = resolve_stock(symbol, market, name)
    key = (identity['market'], identity['symbol'])
    with _lock:
        item = _cache.get(key)
        ttl = _CACHE_SECONDS if item and item[1].get('eligible') is True else 30
        if item and time.monotonic() - item[0] < ttl:
            return deepcopy(item[1])
    # Lazy import keeps the public HTTP app and the monitoring worker independent.
    from app import run_analysis
    bundle = run_analysis(identity['symbol'], identity['name'], identity['market'], with_ai=False)
    result = evaluate_analysis_evidence(bundle)
    with _lock:
        if len(_cache) >= 64:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic(), deepcopy(result))
    return result
