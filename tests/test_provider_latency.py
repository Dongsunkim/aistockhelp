"""Offline checks for bounded provider work and safe reuse of validated data."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock, get_ident

import pandas as pd
import pytest
import requests

import macro_data as macro
import naver_history as naver


def history_rows(count):
    end=datetime.now(naver.KST).date()-timedelta(days=1)
    dates=pd.bdate_range(end=end,periods=count)
    return [dict(localTradedAt=day.strftime("%Y-%m-%d"),openPrice="100",
                 highPrice="110",lowPrice="90",closePrice="105",
                 accumulatedTradingVolume="1000") for day in reversed(dates)]


class Response:
    def __init__(self,payload=None,content=None):
        self.payload=payload
        self.content=content
        self.text=payload if isinstance(payload,str) else ""

    def raise_for_status(self):pass

    def json(self):return deepcopy(self.payload)


class MobileFactory:
    """One fake transport per production session, with overlapping page calls."""
    def __init__(self,rows,*,overlap=False,invalid_after=None):
        self.rows=rows
        self.lock=Lock()
        self.rendezvous=Barrier(4) if overlap else None
        self.invalid_after=invalid_after
        self.pages=[]
        self.clients=[]
        self.active=0
        self.maximum=0

    def __call__(self):
        factory=self
        class Client:
            owner=None
            closed=False

            def get(self,url,*,params,headers,timeout):
                # Neither cache lock nor a shared requests.Session protects an
                # outstanding network call; each worker owns its connection.
                assert naver._daily_cache_lock.acquire(blocking=False)
                naver._daily_cache_lock.release()
                if self.owner is None:self.owner=get_ident()
                assert self.owner==get_ident()
                assert headers==naver.HEADERS
                assert timeout==8
                if "fchart.stock.naver.com" in url:
                    raise requests.HTTPError("FCHART unavailable")
                page=params["page"]
                assert params["pageSize"]==20
                with factory.lock:
                    factory.pages.append(page)
                    factory.active+=1
                    factory.maximum=max(factory.maximum,factory.active)
                try:
                    if factory.rendezvous is not None and 2<=page<=5:
                        factory.rendezvous.wait(timeout=5)
                    if factory.invalid_after is not None and page>factory.invalid_after:
                        return Response({"error":"beyond first short page"})
                    start=(page-1)*20
                    return Response(factory.rows[start:start+20])
                finally:
                    with factory.lock:factory.active-=1

            def close(self):self.closed=True
        client=Client()
        with self.lock:self.clients.append(client)
        return client


@pytest.fixture(autouse=True)
def empty_daily_cache(monkeypatch):
    monkeypatch.setattr(naver,"_daily_cache",OrderedDict())


@pytest.mark.parametrize("history,target,pages",[("1y",330,17),("3y",1000,50)])
def test_default_mobile_pages_overlap_with_four_worker_owned_sessions(monkeypatch,history,target,pages):
    factory=MobileFactory(history_rows(target+30),overlap=True)
    monkeypatch.setattr(naver.requests,"Session",factory)
    bars,metadata=naver.fetch_naver_daily("005930","KOSPI",history=history)

    assert len(bars)==target
    assert bars["date"].is_monotonic_increasing
    assert sorted(factory.pages)==list(range(1,pages+1))
    assert factory.maximum==4
    assert len(factory.clients)==5  # Main FCHART/page-1 session plus four workers.
    assert all(client.closed for client in factory.clients)
    assert metadata["endpoint"]=="NAVER_MOBILE"


def test_default_mobile_stops_at_first_short_page_and_ignores_speculative_pages(monkeypatch):
    # Page 15 has ten rows. Pages 16/17 may already be in the bounded batch,
    # but their malformed payloads must not be consumed or extend the history.
    factory=MobileFactory(history_rows(290),invalid_after=15)
    monkeypatch.setattr(naver.requests,"Session",factory)
    bars,_=naver.fetch_naver_daily("005930","KOSPI")
    assert len(bars)==290
    assert sorted(factory.pages)==list(range(1,18))
    assert max(factory.pages)-15<naver.MOBILE_WORKERS


def test_default_mobile_still_rejects_insufficient_or_duplicate_completed_bars(monkeypatch):
    factory=MobileFactory(history_rows(640))
    monkeypatch.setattr(naver.requests,"Session",factory)
    with pytest.raises(ValueError,match="확정 일봉 부족\\(640/650\\)"):
        naver.fetch_naver_daily("005930","KOSPI",history="3y")
    assert sorted(factory.pages)==list(range(1,34))
    assert not naver._daily_cache

    rows=history_rows(330)
    rows[20]=dict(rows[19])
    factory=MobileFactory(rows)
    monkeypatch.setattr(naver.requests,"Session",factory)
    with pytest.raises(ValueError,match="중복 거래일"):
        naver.fetch_naver_daily("005930","KOSPI")
    assert not naver._daily_cache


def test_default_daily_cache_avoids_network_and_preserves_original_timestamp_and_copies(monkeypatch):
    factory=MobileFactory(history_rows(360))
    monkeypatch.setattr(naver.requests,"Session",factory)
    clock=[100.0]
    monkeypatch.setattr(naver.time,"monotonic",lambda:clock[0])
    first,metadata=naver.fetch_naver_daily("005930","KOSPI")
    fetched_at=metadata["fetched_at"]
    first.loc[0,"close"]=999
    metadata["source"]="caller mutation"
    requests_after_first=len(factory.pages)
    clients_after_first=len(factory.clients)

    clock[0]+=5
    second,cached=naver.fetch_naver_daily("005930","KOSPI")
    assert len(factory.pages)==requests_after_first
    assert len(factory.clients)==clients_after_first
    assert second.loc[0,"close"]==105
    assert cached["source"]=="NAVER"
    assert cached["fetched_at"]==cached["cached_at"]==fetched_at
    assert cached["cache_age_seconds"]==5
    second.loc[0,"close"]=999
    cached["source"]="second mutation"
    third,metadata=naver.fetch_naver_daily("005930","KOSPI")
    assert third.loc[0,"close"]==105
    assert metadata["source"]=="NAVER"

    clock[0]+=naver.DAILY_CACHE_SECONDS
    naver.fetch_naver_daily("005930","KOSPI")
    assert len(factory.pages)==2*requests_after_first


def test_injected_session_bypasses_default_daily_cache_and_is_not_closed(monkeypatch):
    factory=MobileFactory(history_rows(360))
    monkeypatch.setattr(naver.requests,"Session",factory)
    naver.fetch_naver_daily("005930","KOSPI")
    original_requests=len(factory.pages)
    client=factory()
    naver.fetch_naver_daily("005930","KOSPI",session=client)
    assert len(factory.pages)==2*original_requests
    assert not client.closed
    assert factory.pages[original_requests:]==list(range(1,18))


def test_daily_cache_rechecks_completed_bar_freshness(monkeypatch):
    factory=MobileFactory(history_rows(360))
    monkeypatch.setattr(naver.requests,"Session",factory)
    naver.fetch_naver_daily("005930","KOSPI")
    original_requests=len(factory.pages)
    actual_validate=naver.validated
    future=datetime.now(naver.KST)+timedelta(days=8)
    monkeypatch.setattr(naver,"validated",lambda rows,history:actual_validate(rows,history,now=future))
    with pytest.raises(ValueError,match="확정 일봉.*일 경과"):
        naver.fetch_naver_daily("005930","KOSPI")
    assert len(factory.pages)>original_requests
    assert not naver._daily_cache


def test_fchart_cache_keys_include_market_symbol_and_history_and_stay_bounded(monkeypatch):
    rows=history_rows(1000)
    calls=[]
    class Session:
        def get(self,url,*,params,headers,timeout):
            assert url=="https://fchart.stock.naver.com/sise.nhn"
            calls.append(dict(params))
            items="".join('<item data="'+row["localTradedAt"].replace("-","")+
                          '|100|110|90|105|1000"/>' for row in rows[:params["count"]])
            return Response(content=("<protocol>"+items+"</protocol>").encode())

        def close(self):pass

    monkeypatch.setattr(naver.requests,"Session",Session)
    short,metadata=naver.fetch_naver_daily("005930","KOSPI")
    long,_=naver.fetch_naver_daily("005930","KOSPI",history="3y")
    naver.fetch_naver_daily("005930","KOSDAQ")
    naver.fetch_naver_daily("000660","KOSPI")
    assert metadata["endpoint"]=="NAVER_FCHART"
    assert len(short)==330 and len(long)==1000
    assert len(calls)==4
    naver.fetch_naver_daily("005930","KOSPI")
    assert len(calls)==4
    for number in range(naver.DAILY_CACHE_SIZE):
        naver.fetch_naver_daily(f"{number:06}","KOSPI")
    assert len(naver._daily_cache)==naver.DAILY_CACHE_SIZE
    assert ("KOSPI","005930","1y") not in naver._daily_cache


@pytest.mark.parametrize("key,days",[("rate",30),("usdkrw",30),("inflation",800)])
def test_fred_requests_only_required_date_window_and_keeps_exact_cpi_comparisons(key,days):
    # UTC is already the next day; the range must follow the US market date.
    now=datetime(2026,11,6,1,tzinfo=timezone.utc)
    today=now.astimezone(macro.NY).date()
    calls=[]
    class Session:
        def get(self,url,*,params,timeout):
            assert url=="https://fred.stlouisfed.org/graph/graph.csv"
            assert timeout==8
            calls.append(dict(params))
            start=params["cosd"]
            if key=="inflation":
                rows=[(day.strftime("%Y-%m-%d"),100 if day.year==2025 else 102)
                      for day in pd.date_range("2025-09-01","2026-10-01",freq="MS")]
            else:
                rows=[("2026-11-03",4 if key=="rate" else 1000),
                      ("2026-11-04",4.25 if key=="rate" else 1015)]
            text="DATE,"+macro.FRED[key]+"\n"+"\n".join(
                f"{day},{value}" for day,value in rows if day>=start)
            return Response(text)

    metric=macro._fetch_metric(key,now,Session())
    assert calls==[{"id":macro.FRED[key],"cosd":(today-timedelta(days=days)).isoformat()}]
    if key=="inflation":
        assert metric["value"]==metric["previous"]==2
        assert metric["asof"]=="2026-10"
        assert metric["previous_asof"]=="2026-09"
    else:
        assert metric["asof"]=="2026-11-04"
        assert metric["previous_asof"]=="2026-11-03"
