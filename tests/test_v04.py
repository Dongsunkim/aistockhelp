"""Offline tests: no keys, no network, no trading instructions."""
import importlib
from fastapi.testclient import TestClient
from evidence import build_evidence_pack
from tests.test_engine import compute, sample_bars


def test_all_headlines_are_still_unverified():
    r=compute()
    assert not r['confirmed']
    assert '원문 확인 필요' in r['status']


def test_evidence_indexes_primary_news_and_research():
    analysis=compute()
    result=build_evidence_pack({
        'market':'KOSPI','symbol':'005930','analysis':analysis,
        'meta':{'provider':'test provider'},
        'filings':[{'title':'분기 보고서','source':'DART','date':'2026-10-29','link':'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=1'}],
        'news':[{'title':'실적 뉴스','source':'기자','published_at':'2026-11-04T01:00:00Z','link':'https://news.example.net'}],
        'research':[{'title':'애널리스트 리포트 보도','link':'https://news.example.net/r'}],
        'financials':[{'name':'매출액','period':'2026 반기','link':'https://dart.fss.or.kr/','source':'OpenDART'}],
    })
    assert result['source_counts']=={'price':1,'primary':2,'news':1,'research_mentions':1}
    assert result['methodology_url']=='/static/evidence-guide.html'
    assert all(d['url'].startswith('https://') for d in result['documents'])
    assert any('원문 미확인' in d['level'] for d in result['documents'])


def test_login_free_health_and_guide():
    import app as module
    client=TestClient(module.app)
    health=client.get('/api/health')
    assert health.status_code==200
    assert health.json()['requires_password'] is False
    guide=client.get('/static/evidence-guide.html')
    assert guide.status_code==200
    assert '매수 검토 구간' in guide.text
    assert 'OpenDART' in guide.text


def test_public_vercel_does_not_call_paid_ai(monkeypatch):
    import app as module
    bars=sample_bars()
    bars['open']=bars['close']
    monkeypatch.setattr(module,'ON_VERCEL',True)
    monkeypatch.setattr(module,'fetch_daily',lambda *a,**k:(bars,{'provider':'test','fetched_at':'2026-11-04T01:00:00+00:00'}))
    monkeypatch.setattr(module,'fetch_news',lambda *a,**k:[])
    monkeypatch.setattr(module,'fetch_dart_filings',lambda *a,**k:[])
    monkeypatch.setattr(module,'fetch_research_mentions',lambda *a,**k:[])
    monkeypatch.setattr(module,'fetch_financial_facts',lambda *a,**k:[])
    monkeypatch.setattr(module,'kis_configured',lambda:False)
    monkeypatch.setattr(module,'fetch_naver_quote',lambda *a,**k:{'provider':'네이버증권 비공식 시세','price':100,'verified_live':False})
    def fail_if_called(*a,**k):
        raise AssertionError('Paid AI must never be called for the public site')
    monkeypatch.setattr(module,'summarize_with_openai',fail_if_called)
    client=TestClient(module.app)
    result=client.post('/api/analyze',json={'symbol':'005930','market':'KOSPI','name':'삼성전자','with_ai':True})
    assert result.status_code==200
    data=result.json()
    assert not data['analysis']['confirmed']
    assert data['evidence_pack']['documents'][0]['id']=='P1'
    assert '비활성화' in data['ai_error']
