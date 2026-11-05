import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np
import pytest
from backtest import backtest_technical
from quotes import domestic_timestamp_valid
from tests.test_engine import sample_bars,compute


def test_risk_filing_title_blocks_buy():
    result=compute(filings=[{'title':'회사 파산 관련 공시', 'age_days':1}])
    assert not result['confirmed']
    assert result['status']=='근거 확인 전 보류'


def test_kis_quote_cannot_be_called_live_without_dated_trade():
    now=datetime(2026,11,4,11,0,tzinfo=ZoneInfo('Asia/Seoul'))
    assert domestic_timestamp_valid({'stck_cntg_hour':'105900'},now)==(None,False)
    stamp,fresh=domestic_timestamp_valid({'stck_bsop_date':'20261104','stck_cntg_hour':'105900'},now)
    assert stamp=='2026-11-04T10:59:00+09:00'
    assert fresh
    _,old=domestic_timestamp_valid({'stck_bsop_date':'20261104','stck_cntg_hour':'103000'},now)
    assert not old


def test_backtest_does_not_allow_signal_day_close_entry():
    bars=sample_bars()
    extra=[]
    start=pd.to_datetime(bars.iloc[-1]['date'])
    for j in range(1,40):
        close=117+0.35*np.sin((25+j)*1.17)
        extra.append({'date':(start+pd.offsets.BDay(j)).strftime('%Y-%m-%d'),
                      'open':close,'close':close,'high':close+.6,'low':close-.6,'volume':1000})
    bars['open']=bars['close']
    df=pd.concat([bars,pd.DataFrame(extra)],ignore_index=True)
    result=backtest_technical(df,market='KOSPI',horizon=10)
    assert result['total_trades']>0
    assert all(t['entry_date']>t['signal_date'] for t in result['trades'])
    assert all(t['exit_date']>=t['entry_date'] for t in result['trades'])
    assert result['round_trip_fees_bps']>0


def test_invalid_backtest_horizon_rejected():
    bars=sample_bars()
    bars['open']=bars['close']
    with pytest.raises(ValueError):backtest_technical(bars,market='KOSPI',horizon=8)


def test_full_analysis_response_offline(monkeypatch):
    import app as api_module
    from fastapi.testclient import TestClient
    b=sample_bars()
    b['open']=b['close']
    monkeypatch.setattr(api_module,'fetch_daily',lambda symbol,market,**kwargs:(b,{
        'provider':'테스트용 데이터','fetched_at':'2026-11-04T06:00:00+00:00'}))
    monkeypatch.setattr(api_module,'fetch_news',lambda *args,**kwargs:[
        {'title':'중립적인 사업 뉴스','age_hours':4,'published_at':'2026-11-04T06:00:00+00:00',
         'source':'테스트뉴스','link':'https://example.org/test'}])
    monkeypatch.setattr(api_module,'fetch_research_mentions',lambda *args,**kwargs:[
        {'title':'리포트 관련 보도','source':'테스트','published_at':'2026-11-04T06:00:00+00:00',
         'link':'https://example.org/research'}])
    monkeypatch.setattr(api_module,'fetch_dart_filings',lambda *args,**kwargs:[
        {'title':'분기보고서','age_days':30,'source':'DART','link':'https://example.org/filing'}])
    monkeypatch.setattr(api_module,'fetch_financial_facts',lambda *args,**kwargs:[
        {'name':'매출액','value':1000,'unit':'KRW','period':'2026년 반기',
         'reported_at':None,'source':'OpenDART','basis':'연결','link':'https://dart.fss.or.kr'}])
    monkeypatch.setattr(api_module,'kis_configured',lambda:False)
    monkeypatch.setattr(api_module,'fetch_naver_quote',lambda *a,**k:{'provider':'네이버증권 비공식 시세','price':100,'verified_live':False})
    client=TestClient(api_module.app)
    payload=client.post('/api/analyze',json={'symbol':'005930','market':'KOSPI','name':'테스트','with_ai':False})
    assert payload.status_code==200
    data=payload.json()
    assert data['research'][0]['title']=='리포트 관련 보도'
    assert data['financials'][0]['value']==1000
    assert data['quote']['provider']=='네이버증권 비공식 시세'
    assert data['analysis']['levels']['entry_low']>0
    back=client.post('/api/backtest',json={'symbol':'005930','market':'KOSPI','horizon':5})
    assert back.status_code==200
    assert back.json()['backtest']['horizon_sessions']==5
    assert client.get('/').status_code==200
