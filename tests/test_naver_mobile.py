"""Mobile fallback pagination regressions; no external market requests."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from naver_history import fetch_naver_daily


def mobile_history(count):
    yesterday=datetime.now(ZoneInfo("Asia/Seoul")).date()-timedelta(days=1)
    dates=pd.bdate_range(end=yesterday,periods=count)
    return [dict(localTradedAt=day.strftime("%Y-%m-%d"),openPrice="100",
                 highPrice="110",lowPrice="90",closePrice="105",
                 accumulatedTradingVolume="1,000") for day in reversed(dates)]


class MobileResponse:
    def __init__(self,payload):
        self.payload=payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class MobileSession:
    def __init__(self,rows):
        self.rows=rows
        self.pages=[]

    def get(self,url,*,params,headers,timeout):
        if "fchart.stock.naver.com" in url:
            raise requests.HTTPError("FCHART unavailable")
        assert url=="https://m.stock.naver.com/api/stock/005930/price"
        assert params["pageSize"]==20
        self.pages.append(dict(params))
        start=(params["page"]-1)*params["pageSize"]
        return MobileResponse(self.rows[start:start+params["pageSize"]])


@pytest.mark.parametrize("history,target,pages",[("1y",330,17),("3y",1000,50)])
def test_mobile_fallback_preserves_full_history_with_small_pages(history,target,pages):
    session=MobileSession(mobile_history(target+50))
    bars,meta=fetch_naver_daily("005930","KOSPI",history=history,session=session)

    assert len(bars)==target
    assert bars["date"].is_monotonic_increasing
    assert meta["source"]=="NAVER"
    assert meta["endpoint"]=="NAVER_MOBILE"
    assert session.pages==[{"page":page,"pageSize":20} for page in range(1,pages+1)]


def test_mobile_fallback_rejects_short_three_year_history():
    session=MobileSession(mobile_history(640))
    with pytest.raises(ValueError,match="확정 일봉 부족\\(640/650\\)"):
        fetch_naver_daily("005930","KOSPI",history="3y",session=session)
    assert len(session.pages)==33


@pytest.mark.parametrize("payload",[{"error":"bad request"},[None],[]])
def test_mobile_fallback_rejects_invalid_or_empty_payload(payload):
    class InvalidMobileSession(MobileSession):
        def get(self,url,**kwargs):
            if "fchart.stock.naver.com" in url:
                raise requests.HTTPError("FCHART unavailable")
            return MobileResponse(payload)

    with pytest.raises(ValueError,match="네이버 일봉 검증 실패"):
        fetch_naver_daily("005930","KOSPI",session=InvalidMobileSession([]))


def test_mobile_fallback_still_rejects_duplicate_trading_days():
    rows=mobile_history(330)
    rows[20]=dict(rows[19])
    with pytest.raises(ValueError,match="중복 거래일"):
        fetch_naver_daily("005930","KOSPI",session=MobileSession(rows))


def test_mobile_fallback_rejects_oversized_page():
    class OversizedMobileSession(MobileSession):
        def get(self,url,**kwargs):
            if "fchart.stock.naver.com" in url:
                raise requests.HTTPError("FCHART unavailable")
            return MobileResponse(self.rows[:21])

    with pytest.raises(ValueError,match="모바일 응답 페이지 크기 오류"):
        fetch_naver_daily("005930","KOSPI",session=OversizedMobileSession(mobile_history(330)))


def test_mobile_requires_650_completed_bars_for_three_year_history(monkeypatch):
    import naver_history

    class BeforeClose(datetime):
        @classmethod
        def now(cls,tz=None):
            return cls(2026,11,5,10,tzinfo=ZoneInfo("Asia/Seoul"))

    rows=mobile_history(650)
    for row,day in zip(rows,reversed(pd.bdate_range(end="2026-11-05",periods=650))):
        row["localTradedAt"]=day.strftime("%Y-%m-%d")
    monkeypatch.setattr(naver_history,"datetime",BeforeClose)
    with pytest.raises(ValueError,match="확정 일봉 부족\\(649/650\\)"):
        fetch_naver_daily("005930","KOSPI",history="3y",session=MobileSession(rows))
