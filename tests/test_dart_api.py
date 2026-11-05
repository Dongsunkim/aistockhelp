"""API integration: validate input, protect credentials, retain full DART output."""
import pytest
import requests
from fastapi.testclient import TestClient
import app as module


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(module, 'ON_VERCEL', False)
    monkeypatch.delenv('DART_API', raising=False)
    monkeypatch.delenv('DART_API_KEY', raising=False)
    return TestClient(module.app)


def test_dart_key_is_required_on_server(client):
    for endpoint, payload in (
        ('financials', {'symbol':'005930','year':2025}),
        ('disclosures', {'symbol':'005930','begin':'2025-01-01','end':'2025-12-31'}),
    ):
        r=client.post('/api/dart/'+endpoint, json=payload)
        assert r.status_code==503
        assert 'DART_API_KEY' in r.json()['detail']


@pytest.mark.parametrize('payload', [
    {'symbol':'AAPL','year':2025},
    {'symbol':'005930','year':2014},
    {'symbol':'005930','year':2025,'report_code':'oops'},
    {'symbol':'005930','year':2025,'fs_div':'IS'},
])
def test_financial_input_rejected_before_fetch(client,payload):
    assert client.post('/api/dart/financials',json=payload).status_code==422


def test_full_financial_response_is_preserved(client,monkeypatch):
    monkeypatch.setenv('DART_API_KEY','test-server-key')
    full={'statements':[{'fs_div':'CFS','accounts':[
        {'sj_div':'BS','account_nm':'자산총계','thstrm_amount':'9007199254740993123'},
        {'sj_div':'CF','account_nm':'영업현금흐름','thstrm_amount':'-123456789'},
    ]}],'complete':True}
    def fetch(symbol,key,**kwargs):
        assert symbol=='005930' and key=='test-server-key'
        assert kwargs=={'year':2025,'report_code':'11012','fs_div':'BOTH'}
        return full
    monkeypatch.setattr(module,'fetch_dart_financials',fetch)
    r=client.post('/api/dart/financials',json={'symbol':'005930','year':2025,'report_code':'11012'})
    assert r.status_code==200 and r.json()==full
    assert 'test-server-key' not in r.text


def test_disclosure_page_and_legacy_key_forwarding(client,monkeypatch):
    monkeypatch.setenv('DART_API','legacy-test-key')
    def fetch(symbol,key,**kwargs):
        assert symbol=='005930' and key=='legacy-test-key'
        assert kwargs=={'begin':'2025-01-01','end':'2025-12-31','page':2}
        return {'page':2,'total_count':101,'total_pages':2,'items':[{'rcept_no':'20251114000001'}],
                'has_more':False,'complete':True}
    monkeypatch.setattr(module,'fetch_dart_disclosures',fetch)
    r=client.post('/api/dart/disclosures',json={
        'symbol':'005930','begin':'2025-01-01','end':'2025-12-31','page':2})
    assert r.status_code==200 and r.json()['page']==2


@pytest.mark.parametrize('begin,end,page', [
    ('2025-12-31','2025-01-01',1), ('2020-01-01','2025-12-31',1),
    ('not-a-date','2025-12-31',1), ('2025-01-01','2025-12-31',0),
])
def test_disclosure_validation(client,begin,end,page):
    r=client.post('/api/dart/disclosures',json={'symbol':'005930','begin':begin,'end':end,'page':page})
    assert r.status_code==422


@pytest.mark.parametrize('endpoint,payload,adapter', [
    ('financials',{'symbol':'005930','year':2025},'fetch_dart_financials'),
    ('disclosures',{'symbol':'005930','begin':'2025-01-01','end':'2025-12-31'},'fetch_dart_disclosures'),
])
def test_credential_urls_are_not_exposed(client,monkeypatch,endpoint,payload,adapter):
    monkeypatch.setenv('DART_API_KEY','secret-test-key')
    def fail(*args,**kwargs):
        raise requests.HTTPError('https://opendart.fss.or.kr/api/list.json?crtfc_key=secret-test-key')
    monkeypatch.setattr(module,adapter,fail)
    r=client.post('/api/dart/'+endpoint,json=payload)
    assert r.status_code==502 and 'secret-test-key' not in r.text


def test_public_dart_uses_existing_request_throttle(client,monkeypatch):
    monkeypatch.setattr(module,'ON_VERCEL',True)
    monkeypatch.setenv('DART_API_KEY','test-server-key')
    monkeypatch.setattr(module,'fetch_dart_financials',lambda *a,**k:{'complete':True})
    monkeypatch.setattr(module,'_limiter',{})
    payload={'symbol':'005930','year':2025}
    for _ in range(24):
        assert client.post('/api/dart/financials',json=payload).status_code==200
    assert client.post('/api/dart/financials',json=payload).status_code==429


def test_existing_analysis_cannot_expose_dart_query_key(client,monkeypatch):
    from tests.test_engine import sample_bars
    bars=sample_bars()
    bars['open']=bars['close']
    monkeypatch.setenv('DART_API_KEY','analysis-secret-key')
    monkeypatch.setattr(module,'fetch_daily',lambda *a,**k:(bars,{'source':'NAVER'}))
    monkeypatch.setattr(module,'fetch_news',lambda *a,**k:[])
    monkeypatch.setattr(module,'fetch_research_mentions',lambda *a,**k:[])
    monkeypatch.setattr(module,'fetch_naver_quote',lambda *a,**k:{'price':100})
    def fail(*args,**kwargs):
        raise requests.HTTPError('https://opendart.fss.or.kr/api/list.json?crtfc_key=analysis-secret-key')
    monkeypatch.setattr(module,'fetch_dart_filings',fail)
    monkeypatch.setattr(module,'fetch_financial_facts',fail)
    r=client.post('/api/analyze',json={'symbol':'005930','market':'KOSPI'})
    assert r.status_code==200
    assert 'analysis-secret-key' not in r.text and 'crtfc_key=' not in r.text
    assert r.json()['filings_error'] and r.json()['financials_error']
