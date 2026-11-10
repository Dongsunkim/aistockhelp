"""Identity safety is checked before stock evidence or AI calls."""
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import stock_lookup


@pytest.fixture
def identity_api(monkeypatch, tmp_path):
    import app as module
    monkeypatch.setattr(module,'resolve_stock',stock_lookup.resolve_stock)
    monkeypatch.setattr(module,'ON_VERCEL',False)
    monkeypatch.setattr(module,'DB',tmp_path/'identity.sqlite3')
    module.init_db()
    monkeypatch.setattr(stock_lookup,'_get_session',lambda:pytest.fail('unexpected live lookup'))
    module._analysis_contexts.clear()
    yield module,TestClient(module.app)
    module._analysis_contexts.clear()


def test_catalog_search_and_market_crossing_are_available_offline(identity_api):
    _,client=identity_api
    catalog=client.get('/api/stocks/catalog').json()['items']
    assert len(catalog)==18
    assert 'Samsung Electronics' in next(s for s in catalog if s['symbol']=='005930')['aliases']
    result=client.get('/api/stocks/search',params={'q':'삼성전자'}).json()['items']
    assert result[0]['symbol']=='005930'
    assert {s['symbol'] for s in result}=={'005930','005935'}
    result=client.get('/api/stocks/search',params={'q':'애플'}).json()['items']
    assert result[0]['market']=='US'


@pytest.mark.parametrize('params',[{'q':''},{'q':'x'*81},{'q':'삼성','market':'bad'},{'q':'삼성','limit':21}])
def test_search_validates_request(identity_api,params):
    assert identity_api[1].get('/api/stocks/search',params=params).status_code==422


@pytest.mark.parametrize('route',['/api/analyze','/api/analyze/stream','/api/ask','/api/watchlist'])
def test_mismatched_name_rejected_before_providers(identity_api,monkeypatch,route):
    module,client=identity_api
    analyze=Mock(side_effect=AssertionError('provider must not start'))
    monkeypatch.setattr(module,'run_analysis',analyze)
    monkeypatch.setattr(module,'iter_analysis_events',analyze)
    response=client.post(route,json={'symbol':'000660','market':'KOSPI','name':'삼성전자','question':'언제 매수?'} )
    assert response.status_code==422
    assert '005930' in response.json()['detail']
    analyze.assert_not_called()


def test_wrong_market_rejected(identity_api):
    response=identity_api[1].post('/api/analyze',json={'symbol':'086520','market':'KOSPI','name':'에코프로'})
    assert response.status_code==422
    assert 'KOSDAQ' in response.json()['detail']


def test_json_analysis_canonicalizes_unknown_label(identity_api,monkeypatch):
    module,client=identity_api
    provider=Mock(return_value={'symbol':'005930','company_name':'삼성전자'})
    monkeypatch.setattr(module,'run_analysis',provider)
    response=client.post('/api/analyze',json={'symbol':'005930','market':'KOSPI','name':'old label'})
    assert response.status_code==200
    provider.assert_called_once_with('005930','삼성전자','KOSPI',False)


def test_stream_analysis_canonicalizes_alias(identity_api,monkeypatch):
    module,client=identity_api
    def events(symbol,name,market,*args,**kwargs):
        yield {'stage':'complete','result':{'symbol':symbol,'company_name':name,'market':market},'pending_sources':[]}
    monkeypatch.setattr(module,'iter_analysis_events',events)
    response=client.post('/api/analyze/stream',json={'symbol':'AAPL','market':'US','name':'애플'})
    assert response.status_code==200
    assert json.loads(response.text)['result']=={'symbol':'AAPL','company_name':'Apple','market':'US'}


def test_qa_alias_uses_canonical_cached_context(identity_api,monkeypatch):
    module,client=identity_api
    packet={'symbol':'005930','market':'KOSPI','company_name':'삼성전자'}
    module._remember_context(packet)
    refresh=Mock(side_effect=AssertionError('canonical cache should be used'))
    answer=Mock(return_value={'answer':'cached'})
    monkeypatch.setattr(module,'run_analysis',refresh)
    monkeypatch.setattr(module,'answer_question',answer)
    response=client.post('/api/ask',json={'symbol':'005930','market':'KOSPI','name':'Samsung Electronics','question':'매수 가격은?'})
    assert response.status_code==200
    refresh.assert_not_called()
    answer.assert_called_once_with('매수 가격은?',packet)


def test_watchlist_never_stores_arbitrary_label(identity_api):
    _,client=identity_api
    response=client.post('/api/watchlist',json={'symbol':'005930','market':'KOSPI','name':'old label'})
    assert response.status_code==200
    assert client.get('/api/watchlist').json()[0]['name']=='삼성전자'
