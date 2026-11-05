import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import pytest
from analysis_engine import analyze_bars
from sources import yahoo_symbol

NOW=datetime(2026,11,4,15,0,tzinfo=ZoneInfo('Asia/Seoul'))


def sample_bars():
    v=np.linspace(80,120,220)
    v[-25:]=117+0.35*np.sin(np.arange(25)*1.17)
    h=v+0.6
    # Multiple historic higher wicks, not a single anomalous spike
    h[-45:-35]=136
    return pd.DataFrame({'date':pd.bdate_range(end='2026-11-03',periods=len(v)).date.astype(str),
                         'close':v, 'high':h, 'low':v-0.6, 'volume':np.ones(len(v))*1000})


def compute(bars=None,news=None,filings=None,news_error=None,filings_error=None,asof=NOW):
    return analyze_bars(bars if bars is not None else sample_bars(), market='KOSPI',
        news=[{'title':'중립적인 사업 뉴스','age_hours':4}] if news is None else news,
        filings=[{'title':'사업보고서','age_days':20}] if filings is None else filings,
        news_error=news_error,filings_error=filings_error,asof=asof)


def test_conservative_signal_requires_all_gates():
    s=compute()
    assert all(x['pass'] for x in s['conditions'])
    assert s['status']=='기술적 매수 관찰 · 원문 확인 필요'
    assert not s['confirmed']
    assert s['levels']['entry_low'] <= s['close'] <= s['levels']['entry_high']
    assert s['levels']['rr']>=2


def test_recent_negative_headline_blocks_review():
    s=compute(news=[{'title':'기업 회계부정 조사 보도','age_hours':2}])
    assert s['status']=='근거 확인 전 보류'
    assert not s['confirmed']
    assert s['red_flags']


def test_missing_or_stale_news_blocks_review():
    assert compute(news=[])['status']=='근거 확인 전 보류'
    assert compute(news=[{'title':'일반뉴스','age_hours':95}])['status']=='근거 확인 전 보류'
    assert compute(news_error='수집 실패')['status']=='근거 확인 전 보류'


def test_missing_filings_blocks_review():
    assert compute(filings=[])['status']=='근거 확인 전 보류'
    assert compute(filings_error='API 키 없음')['status']=='근거 확인 전 보류'


def test_stale_data_never_triggers():
    s=compute(asof=datetime(2026,11,15,13,0,tzinfo=ZoneInfo('Asia/Seoul')))
    assert s['status']=='데이터 확인 필요'
    assert not s['confirmed']


def test_too_few_bars_fails_safely():
    with pytest.raises(ValueError): compute(bars=sample_bars().tail(40))


def test_market_symbol_validation():
    assert yahoo_symbol('005930','KOSPI')=='005930.KS'
    assert yahoo_symbol('035720','KOSDAQ')=='035720.KQ'
    assert yahoo_symbol('aapl','US')=='AAPL'
    with pytest.raises(ValueError):yahoo_symbol('00593','KOSPI')
    with pytest.raises(ValueError):yahoo_symbol('AAPL','KOSDAQ')
    with pytest.raises(ValueError):yahoo_symbol('AAPL','??')
